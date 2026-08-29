import unittest
from unittest.mock import Mock, patch

from agent.loop import run_agent


class AgentArgumentSafetyTests(unittest.TestCase):
    @patch("agent.loop.tools_for")
    def test_malformed_create_cart_items_fail_in_loop(self, tools_for) -> None:
        tools = Mock()
        tools.respond_to_customer.return_value = {"message": "I could not create that cart safely."}
        tools_for.return_value = tools

        def model(context: dict) -> dict:
            if not context["history"]:
                return {"tool": "create_cart", "arguments": {"items": "product_1"}}
            return {"tool": "respond_to_customer", "arguments": {"message": "I could not create that cart safely."}}

        result = run_agent("Buy it", customer_id="customer_demo", model=model)
        self.assertEqual(result["message"], "I could not create that cart safely.")
        self.assertIn("create_cart.items", result["history"][0]["result"]["error"])
        tools.create_cart.assert_not_called()

    @patch("agent.loop.tools_for")
    def test_respond_to_customer_terminates_the_run(self, tools_for) -> None:
        tools = Mock()
        tools.respond_to_customer.return_value = {"message": "Done."}
        tools_for.return_value = tools

        result = run_agent(
            "Tell me when done", customer_id="customer_demo",
            model=lambda _context: {"tool": "respond_to_customer", "arguments": {"message": "Done."}},
        )

        self.assertEqual(result["message"], "Done.")
        self.assertEqual(result["history"][0]["tool"], "respond_to_customer")
        tools.respond_to_customer.assert_called_once_with(message="Done.")

    @patch("agent.loop.tools_for")
    def test_ordinary_final_is_rejected_as_a_model_turn(self, tools_for) -> None:
        tools = Mock()
        tools.respond_to_customer.return_value = {"message": "Recovered."}
        tools_for.return_value = tools
        actions = iter(({"final": "Not a tool call."}, {"tool": "respond_to_customer", "arguments": {"message": "Recovered."}}))

        result = run_agent("Finish", customer_id="customer_demo", model=lambda _context: next(actions))

        self.assertEqual(result["message"], "Recovered.")
        self.assertEqual(result["history"][0]["error"], "Exactly one available tool call is required")

    @patch("agent.loop.tools_for")
    def test_unresolved_checkout_keeps_only_observation_and_terminal_tools(self, tools_for) -> None:
        tools = Mock()
        tools.respond_to_customer.return_value = {"message": "Waiting."}
        tools_for.return_value = tools
        for status in ("PENDING", "AMBIGUOUS"):
            tools.start_checkout.return_value = {"status": status, "allowed": True}
            seen = []

            def model(context: dict) -> dict:
                seen.append({tool["name"] for tool in context["tools"]})
                if not context["history"]:
                    return {"tool": "start_checkout", "arguments": {}}
                return {"tool": "respond_to_customer", "arguments": {"message": "Waiting."}}

            with self.subTest(status=status):
                run_agent("Pay", customer_id="customer_demo", model=model)
                self.assertEqual(seen[1], {"get_payment_status", "get_audit_timeline", "respond_to_customer"})
