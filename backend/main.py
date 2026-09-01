"""Minimal same-origin browser checkout server."""

import json
import hmac
import os
import time
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent.loop import run_agent
from agent.gemini import gemini_first_model
from backend.browser_checkout import record_client_cancellation, record_client_payment_reference, record_client_timeout
from backend.catalogue import create_cart, get_mandate, increase_mandate, update_mandate, validate_purchase
from backend.checkout import start_checkout
from backend.resolver import reconcile_status
from backend.razorpay import RazorpayError
from backend.views import customer_intent, customer_transactions, operator_intent, operator_transactions
from backend.webhooks import ingest_webhook


FRONTEND = Path(__file__).parents[1] / "frontend"
DEMO_CUSTOMER_ID = "customer_demo"
_CUSTOMER_CHAT_STATE: dict[str, dict] = {}
_RATE_LIMIT: dict[tuple[str, str], list[float]] = {}
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_MAX = 60
_RATE_LIMIT_WINDOW = 60.0
_CSP = "default-src 'self'; script-src 'self' https://checkout.razorpay.com; connect-src 'self' https://api.razorpay.com https://checkout.razorpay.com; style-src 'self'; img-src 'self' data:; frame-src https://checkout.razorpay.com https://api.razorpay.com; object-src 'none'; base-uri 'self'; form-action 'self'"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") and self._rate_limited(parsed.path):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many requests"})
            return
        if parsed.path == "/api/customer/intent":
            self._json(HTTPStatus.OK, customer_intent(_query(parsed.query, "intent_id"), DEMO_CUSTOMER_ID))
            return
        if parsed.path == "/api/customer/transactions":
            self._json(HTTPStatus.OK, customer_transactions(DEMO_CUSTOMER_ID))
            return
        if parsed.path == "/api/customer/mandate":
            self._json(HTTPStatus.OK, get_mandate(DEMO_CUSTOMER_ID) or {"found": False})
            return
        if parsed.path == "/api/operator/intent":
            if not self._operator_authorized():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self._json(HTTPStatus.OK, operator_intent(_query(parsed.query, "intent_id")))
            return
        if parsed.path == "/api/operator/transactions":
            if not self._operator_authorized():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self._json(HTTPStatus.OK, operator_transactions(_query(parsed.query, "q")))
            return
        filename = {
            "/": "index.html", "/operator": "operator.html", "/checkout.js": "checkout.js",
            "/app.js": "app.js", "/customer.js": "customer.js", "/styles.css": "styles.css",
        }.get(parsed.path)
        if not filename:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = (FRONTEND / filename).read_bytes()
        self.send_response(HTTPStatus.OK)
        content_type = "text/css" if filename.endswith(".css") else "text/javascript" if filename.endswith(".js") else "text/html"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        if self._rate_limited(urlparse(self.path).path):
            self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many requests"})
            return
        if self.path == "/api/customer/chat":
            self._handle_customer_chat()
            return
        if self.path == "/api/customer/mandate":
            self._handle_mandate()
            return
        if self.path == "/api/customer/cart":
            self._handle_cart()
            return
        if self.path == "/api/customer/checkout":
            self._handle_checkout()
            return
        if self.path == "/api/operator/reconcile":
            self._handle_reconcile()
            return
        if self.path == "/webhooks/razorpay":
            self._handle_razorpay_webhook()
            return
        if self.path == "/checkout/client-timeout":
            self._handle_client_timeout()
            return
        if self.path == "/checkout/client-cancel":
            self._handle_client_cancel()
            return
        if self.path != "/checkout/client-report":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 16_384:
                raise ValueError
            body = json.loads(self.rfile.read(content_length))
            result = record_client_payment_reference(
                body["intent_id"],
                DEMO_CUSTOMER_ID,
                body["razorpay_order_id"],
                body["razorpay_payment_id"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        self._json(HTTPStatus.ACCEPTED if result["accepted"] else HTTPStatus.BAD_REQUEST, result)

    def _handle_client_timeout(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 16_384:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(content_length))
            result = record_client_timeout(body["intent_id"], DEMO_CUSTOMER_ID, body.get("event", "timeout"))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        self._json(HTTPStatus.ACCEPTED if result["accepted"] else HTTPStatus.BAD_REQUEST, result)

    def _handle_client_cancel(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 16_384:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(content_length))
            result = record_client_cancellation(body["intent_id"], DEMO_CUSTOMER_ID)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        self._json(HTTPStatus.ACCEPTED if result["accepted"] else HTTPStatus.BAD_REQUEST, result)

    def _handle_customer_chat(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 16_384:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(content_length))
            request = body["request"]
            if not isinstance(request, str) or not request.strip():
                raise ValueError("request is required")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        customer_request = _continue_customer_request(DEMO_CUSTOMER_ID, request.strip())
        had_context = DEMO_CUSTOMER_ID in _CUSTOMER_CHAT_STATE
        try:
            result = run_agent(customer_request, customer_id=DEMO_CUSTOMER_ID, model=gemini_first_model)
        except (RuntimeError, ValueError):
            self._json(HTTPStatus.BAD_GATEWAY, {"error": "Agent service unavailable"})
            return
        _update_customer_chat_state(DEMO_CUSTOMER_ID, result, had_context)
        checkout = next(
            (entry.get("result") for entry in result.get("history", [])
             if entry.get("tool") == "start_checkout" and isinstance(entry.get("result"), dict)
             and entry["result"].get("intent_id")),
            None,
        )
        self._json(HTTPStatus.OK, {**result, "checkout": checkout})

    def _read_json(self, maximum: int = 16_384) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if not 0 < content_length <= maximum:
            raise ValueError("invalid request size")
        body = json.loads(self.rfile.read(content_length))
        if not isinstance(body, dict):
            raise ValueError("object required")
        return body

    def _handle_mandate(self) -> None:
        try:
            body = self._read_json()
            if body.get("action") == "increase":
                result = increase_mandate(DEMO_CUSTOMER_ID, body["mandate_id"], body["cart_id"], request_id=body["request_id"])
                self._json(HTTPStatus.OK, result)
                return
            from datetime import datetime
            expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
            result = update_mandate(DEMO_CUSTOMER_ID, body["max_amount_paise"], body["allowed_categories"], expires_at, request_id=body["request_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        self._json(HTTPStatus.OK, result)

    def _handle_cart(self) -> None:
        try:
            body = self._read_json()
            cart = create_cart(body["items"], customer_id=DEMO_CUSTOMER_ID)
            result = validate_purchase(cart["cart_id"], body["mandate_id"], customer_id=DEMO_CUSTOMER_ID)
            self._json(HTTPStatus.OK, {**cart, **result})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def _handle_checkout(self) -> None:
        try:
            body = self._read_json()
            result = start_checkout(body["cart_id"], body["mandate_id"], body["request_id"], customer_id=DEMO_CUSTOMER_ID)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._json(HTTPStatus.OK if result.get("allowed") else HTTPStatus.BAD_REQUEST, result)

    def _handle_razorpay_webhook(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 1_048_576:
                raise ValueError
            body = self.rfile.read(content_length)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        demo_delay = self.headers.get("X-Demo-Webhook-Delay", "")
        if demo_delay and os.environ.get("DEMO_MODE") == "1":
            try:
                delay = float(demo_delay)
                if not 0 <= delay <= 10:
                    raise ValueError
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            time.sleep(delay)
        result = ingest_webhook(
            body,
            self.headers.get("X-Razorpay-Signature"),
            self.headers.get("X-Razorpay-Event-Id"),
        )
        self._json(HTTPStatus.OK if result["accepted"] else HTTPStatus.BAD_REQUEST, result)

    def _handle_reconcile(self) -> None:
        if not self._operator_authorized():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 0 < content_length <= 16_384:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(content_length))
            result = reconcile_status(body["attempt_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        except RazorpayError:
            self._json(HTTPStatus.BAD_GATEWAY, {"accepted": False, "message": "Reconciliation unavailable"})
            return
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"accepted": False, "message": "Internal server error"})
            return
        self._json(HTTPStatus.OK, result)

    def _operator_authorized(self) -> bool:
        token = os.environ.get("OPERATOR_VIEW_TOKEN")
        supplied = self.headers.get("X-Operator-Token", "")
        return bool(token) and hmac.compare_digest(supplied, token)

    def _rate_limited(self, path: str) -> bool:
        now = time.monotonic()
        key = (self.client_address[0], path)
        with _RATE_LIMIT_LOCK:
            timestamps = [stamp for stamp in _RATE_LIMIT.get(key, []) if now - stamp < _RATE_LIMIT_WINDOW]
            limited = len(timestamps) >= _RATE_LIMIT_MAX
            if not limited:
                timestamps.append(now)
            _RATE_LIMIT[key] = timestamps
            return limited

    def _json(self, status: HTTPStatus, result: dict) -> None:
        encoded = json.dumps(result, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.path.startswith(("/api/", "/checkout/", "/webhooks/")) or self.path.split("?", 1)[0] == "/customer.js":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _query(query: str, name: str) -> str:
    return parse_qs(query).get(name, [""])[0]


def _continue_customer_request(customer_id: str, request: str) -> str:
    state = _CUSTOMER_CHAT_STATE.get(customer_id)
    product = state.get("product") if state else None
    if not product:
        return request
    return f"{request}\n\nThe immediately preceding turn selected product {product['id']} ({product['name']}). Resolve 'it' as this product and continue the purchase flow."


def _update_customer_chat_state(customer_id: str, result: dict, had_context: bool) -> None:
    history = result.get("history", []) if isinstance(result, dict) else []
    checkout_exists = any(
        entry.get("tool") == "start_checkout"
        and isinstance(entry.get("result"), dict)
        and entry["result"].get("intent_id")
        for entry in history
    )
    if checkout_exists or had_context:
        _CUSTOMER_CHAT_STATE.pop(customer_id, None)
        return
    for entry in reversed(history):
        if entry.get("tool") != "search_catalogue" or not isinstance(entry.get("result"), list):
            continue
        products = [product for product in entry["result"] if isinstance(product, dict) and product.get("id") and product.get("name")]
        if len(products) == 1:
            _CUSTOMER_CHAT_STATE[customer_id] = {"product": {"id": products[0]["id"], "name": products[0]["name"]}}
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    print("Serving checkout at http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    main()
