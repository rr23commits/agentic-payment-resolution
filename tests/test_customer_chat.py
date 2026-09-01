import json
import subprocess
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from http.server import ThreadingHTTPServer

from backend.main import Handler, _CUSTOMER_CHAT_STATE
import backend.main as main_module
from agent.gemini import gemini_first_model


class CustomerChatEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        _CUSTOMER_CHAT_STATE.clear()
        main_module._RATE_LIMIT.clear()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        _CUSTOMER_CHAT_STATE.clear()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    @patch("backend.main.run_agent")
    def test_chat_runs_customer_bound_agent_and_surfaces_checkout(self, run_agent) -> None:
        run_agent.return_value = {
            "message": "Checkout is ready.",
            "history": [{"tool": "start_checkout", "result": {"intent_id": "intent_chat", "order_id": "order_chat", "status": "PENDING"}}],
        }
        payload = json.dumps({"request": "Buy a book"})
        connection = HTTPConnection(*self.server.server_address)
        connection.request("POST", "/api/customer/chat", payload, {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(result["checkout"]["intent_id"], "intent_chat")
        run_agent.assert_called_once()
        self.assertEqual(run_agent.call_args.kwargs["customer_id"], "customer_demo")
        self.assertIs(run_agent.call_args.kwargs["model"], gemini_first_model)

    @patch("backend.main.run_agent")
    def test_follow_up_reuses_selected_product(self, run_agent) -> None:
        run_agent.side_effect = [
            {
                "message": "Would you like me to buy it?",
                "history": [{"tool": "search_catalogue", "result": [{"id": "product_demo_book", "name": "The Demo Book"}]}],
            },
            {
                "message": "Checkout is ready.",
                "history": [
                    {"tool": "create_cart", "arguments": {"items": [{"product_id": "product_demo_book", "quantity": 1}]}},
                    {"tool": "get_mandate", "arguments": {}},
                    {"tool": "validate_purchase", "result": {"allowed": True}},
                    {"tool": "start_checkout", "result": {"intent_id": "intent_chat", "status": "PENDING"}},
                ],
            },
        ]
        for request in ("Buy me the book", "Yes, buy it"):
            connection = HTTPConnection(*self.server.server_address)
            connection.request("POST", "/api/customer/chat", json.dumps({"request": request}), {"Content-Type": "application/json"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 200)

        self.assertEqual(run_agent.call_count, 2)
        self.assertEqual(run_agent.call_args_list[1].args[0].split("\n", 1)[0], "Yes, buy it")
        self.assertIn("product_demo_book", run_agent.call_args_list[1].args[0])
        self.assertEqual(run_agent.call_args_list[1].kwargs["customer_id"], "customer_demo")

    @patch("backend.main.run_agent")
    def test_follow_up_without_selected_product_does_not_invent_context(self, run_agent) -> None:
        run_agent.return_value = {"message": "Please choose a product.", "history": []}
        connection = HTTPConnection(*self.server.server_address)
        connection.request("POST", "/api/customer/chat", json.dumps({"request": "Yes, buy it"}), {"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(run_agent.call_args.args[0], "Yes, buy it")

    def test_chat_rejects_missing_request(self) -> None:
        payload = json.dumps({})
        connection = HTTPConnection(*self.server.server_address)
        connection.request("POST", "/api/customer/chat", payload, {"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        connection.close()

    @patch("backend.main.validate_purchase", return_value={
        "allowed": True, "cart_total_paise": 169800, "mandate_cap_paise": 200000,
        "reasons": [], "cart_id": "cart_customer", "mandate_id": "mandate_customer",
    })
    @patch("backend.main.create_cart", return_value={
        "cart_id": "cart_customer", "customer_id": "customer_demo", "total_paise": 169800,
    })
    def test_customer_cart_response_keeps_authoritative_validation_fields(self, _create_cart, _validate) -> None:
        connection = HTTPConnection(*self.server.server_address)
        connection.request(
            "POST", "/api/customer/cart",
            json.dumps({"items": [{"product_id": "product_pants", "quantity": 2}], "mandate_id": "mandate_customer"}),
            {"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(result["cart_total_paise"], 169800)
        self.assertEqual(result["mandate_cap_paise"], 200000)

    def test_customer_renderer_uses_validation_projection_and_waits_for_client_payment(self) -> None:
        with open("frontend/customer.js", encoding="utf-8") as file:
            source = file.read()
        self.assertIn("money(cart.cart_total_paise)", source)
        self.assertIn("money(cart.mandate_cap_paise)", source)
        self.assertIn("increase.hidden = false", source)
        self.assertIn("increase.onclick = increaseMandate", source)
        self.assertIn("await reviewCart();", source)
        self.assertIn('$("launch").hidden = !cart.allowed', source)
        self.assertIn("window.openRazorpayCheckout(result)", source)
        self.assertIn('result.status === "AMBIGUOUS" ? result.message', source)
        self.assertIn(' : "Checkout ready"', source)
        self.assertIn("if (result.checkout?.intent_id)", source)
        self.assertIn("const persisted = await loadIntent(result.checkout.intent_id)", source)
        self.assertIn('persisted?.status === "PENDING" && !persisted.payment_id', source)
        self.assertIn("window.openRazorpayCheckout(persisted.checkout)", source)
        self.assertIn("extractRequestedQuantities", source)
        self.assertIn('t[\\s-]?shirts?', source)
        self.assertIn('catalogueCategoryAliases', source)
        with open("frontend/app.js", encoding="utf-8") as file:
            operator_source = file.read()
        self.assertIn('const evidence = result.evidence || {}', operator_source)
        self.assertIn("Webhook Signature Verified", operator_source)
        self.assertIn("Resolution Reason", operator_source)

        with open("frontend/index.html", encoding="utf-8") as file:
            markup = file.read()
        self.assertIn("Payment protection — An unresolved payment is never automatically retried.", markup)
        self.assertIn("Controlled demo · single customer session", markup)
        self.assertIn(">Customer</span>", markup)
        self.assertNotIn('aria-label="Help"', markup)
        self.assertNotIn('aria-label="Settings"', markup)

        with open("frontend/checkout.js", encoding="utf-8") as file:
            checkout_source = file.read()
        self.assertIn('window.onRazorpayDismiss(checkout)', checkout_source)
        self.assertIn('fetch("/checkout/client-cancel"', source)

    def test_customer_quantity_parser_handles_supported_phrases(self) -> None:
        with open("frontend/customer.js", encoding="utf-8") as file:
            source = file.read()
        start = source.index("function extractRequestedQuantities")
        end = source.index("\n}\n\nfunction nav", start) + 2
        aliases = source[source.index("const catalogueCategoryAliases"):source.index(";", source.index("const catalogueCategoryAliases")) + 1]
        script = aliases + "\n" + source[start:end] + "\nconsole.log(JSON.stringify(extractRequestedQuantities('2 T-Shirts and 3 pants')));"
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"tshirts": 2, "pants": 3})

    @patch("backend.main.reconcile_status", return_value={"status": "CAPTURED"})
    def test_operator_reconcile_uses_attempt_id(self, reconcile) -> None:
        with patch.dict("os.environ", {"OPERATOR_VIEW_TOKEN": "operator-secret"}):
            connection = HTTPConnection(*self.server.server_address)
            connection.request(
                "POST", "/api/operator/reconcile", json.dumps({"attempt_id": "attempt_1"}),
                {"Content-Type": "application/json", "X-Operator-Token": "operator-secret"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
        self.assertEqual(response.status, 200)
        reconcile.assert_called_once_with("attempt_1")

    @patch("backend.main.time.sleep")
    @patch("backend.main.ingest_webhook", return_value={"accepted": True})
    def test_demo_webhook_delay_happens_inside_processing_boundary(self, ingest, sleep) -> None:
        with patch.dict("os.environ", {"DEMO_MODE": "1"}, clear=True):
            connection = HTTPConnection(*self.server.server_address)
            connection.request(
                "POST", "/webhooks/razorpay", "{}",
                {"Content-Type": "application/json", "X-Demo-Webhook-Delay": "0.25"},
            )
            response = connection.getresponse()
            response.read()
            connection.close()
        self.assertEqual(response.status, 200)
        sleep.assert_called_once_with(0.25)
        ingest.assert_called_once()

    @patch("backend.main.time.sleep")
    @patch("backend.main.ingest_webhook", return_value={"accepted": True})
    def test_webhook_delay_header_is_ignored_outside_demo_mode(self, ingest, sleep) -> None:
        with patch.dict("os.environ", {}, clear=True):
            connection = HTTPConnection(*self.server.server_address)
            connection.request("POST", "/webhooks/razorpay", "{}", {"X-Demo-Webhook-Delay": "10"})
            response = connection.getresponse()
            response.read()
            connection.close()
        self.assertEqual(response.status, 200)
        sleep.assert_not_called()

    def test_payment_responses_include_security_headers(self) -> None:
        connection = HTTPConnection(*self.server.server_address)
        connection.request("GET", "/api/customer/transactions")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertIn("script-src 'self' https://checkout.razorpay.com", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    @patch("backend.main.reconcile_status", side_effect=RuntimeError("database bug"))
    def test_unexpected_reconcile_error_returns_internal_server_error(self, _reconcile) -> None:
        with patch.dict("os.environ", {"OPERATOR_VIEW_TOKEN": "operator-secret"}):
            connection = HTTPConnection(*self.server.server_address)
            connection.request("POST", "/api/operator/reconcile", json.dumps({"attempt_id": "attempt_1"}), {"X-Operator-Token": "operator-secret"})
            response = connection.getresponse()
            response.read()
            connection.close()
        self.assertEqual(response.status, 500)

    def test_post_requests_are_rate_limited_per_client_and_path(self) -> None:
        with patch.object(main_module, "_RATE_LIMIT_MAX", 1):
            connection = HTTPConnection(*self.server.server_address)
            connection.request("POST", "/api/customer/chat", json.dumps({}))
            first = connection.getresponse(); first.read()
            connection.close()
            connection = HTTPConnection(*self.server.server_address)
            connection.request("POST", "/api/customer/chat", json.dumps({}))
            second = connection.getresponse(); second.read()
            connection.close()
        self.assertEqual(first.status, 400)
        self.assertEqual(second.status, 429)
