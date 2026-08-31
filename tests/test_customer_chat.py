import json
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

from http.server import ThreadingHTTPServer

from backend.main import Handler, _CUSTOMER_CHAT_STATE
from agent.gemini import gemini_first_model


class CustomerChatEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        _CUSTOMER_CHAT_STATE.clear()
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
        with patch.dict("os.environ", {}, clear=True):
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
