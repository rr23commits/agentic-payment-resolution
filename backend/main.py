"""Minimal same-origin browser checkout server."""

import json
import hmac
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent.loop import run_agent
from agent.gemini import gemini_first_model
from backend.browser_checkout import record_client_payment_reference, record_client_timeout
from backend.resolver import reconcile_status
from backend.views import customer_intent, customer_transactions, operator_intent
from backend.webhooks import ingest_webhook


FRONTEND = Path(__file__).parents[1] / "frontend"
DEMO_CUSTOMER_ID = "customer_demo"
_CUSTOMER_CHAT_STATE: dict[str, dict] = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/customer/intent":
            self._json(HTTPStatus.OK, customer_intent(_query(parsed.query, "intent_id")))
            return
        if parsed.path == "/api/customer/transactions":
            self._json(HTTPStatus.OK, customer_transactions(DEMO_CUSTOMER_ID))
            return
        if parsed.path == "/api/operator/intent":
            if not self._operator_authorized():
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            self._json(HTTPStatus.OK, operator_intent(_query(parsed.query, "intent_id")))
            return
        filename = {
            "/": "index.html", "/operator": "operator.html", "/checkout.js": "checkout.js",
            "/app.js": "app.js", "/styles.css": "styles.css",
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
        if self.path == "/api/customer/chat":
            self._handle_customer_chat()
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
                body["customer_id"],
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
            result = record_client_timeout(body["intent_id"], body["customer_id"], body.get("event", "timeout"))
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
        if demo_delay:
            try:
                delay = float(demo_delay)
                if not 0 <= delay <= 60:
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
        except RuntimeError:
            self._json(HTTPStatus.BAD_GATEWAY, {"accepted": False, "message": "Reconciliation unavailable"})
            return
        self._json(HTTPStatus.OK, result)

    def _operator_authorized(self) -> bool:
        token = os.environ.get("OPERATOR_VIEW_TOKEN")
        supplied = self.headers.get("X-Operator-Token", "")
        return bool(token) and hmac.compare_digest(supplied, token)

    def _json(self, status: HTTPStatus, result: dict) -> None:
        encoded = json.dumps(result, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


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
