const byId = (id) => document.getElementById(id);
let customerPoll;

async function readCustomer() {
  clearInterval(customerPoll);
  const intentId = byId("intent-id").value;
  const response = await fetch(`/api/customer/intent?intent_id=${encodeURIComponent(intentId)}`);
  const result = await response.json();
  byId("payment-status").textContent = result.found ? result.message : "Checkout not found.";
  byId("mandate-outcome").textContent = result.found
    ? `Cart: ₹${result.cart_total_paise / 100}; mandate cap: ₹${result.mandate_cap_paise / 100}.`
    : "";
  byId("launch").hidden = !result.checkout;
  byId("demo-timeout").hidden = !result.checkout;
  byId("launch").onclick = () => window.openRazorpayCheckout(result.checkout);
  if (result.found && ["PENDING", "AMBIGUOUS"].includes(result.status)) {
    customerPoll = setInterval(readCustomer, 2000);
  }
}

async function runChat(event) {
  event.preventDefault();
  const status = byId("chat-status");
  status.textContent = "Asking the agent…";
  byId("chat-history").replaceChildren();
  const response = await fetch("/api/customer/chat", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({request: byId("chat-request").value}),
  });
  const result = await response.json();
  if (!response.ok) {
    status.textContent = result.error || "The agent could not respond.";
    return;
  }
  status.textContent = result.message;
  byId("chat-history").replaceChildren(...(result.history || []).map((entry) => {
    const item = document.createElement("li");
    item.textContent = entry.tool
      ? `${entry.tool}: ${JSON.stringify(entry.result || entry.error || {})}`
      : entry.error || "Model response received";
    return item;
  }));
  if (result.checkout) {
    byId("intent-id").value = result.checkout.intent_id;
    await readCustomer();
  }
}

async function readOperator() {
  const intentId = byId("operator-intent").value;
  const token = byId("operator-token").value;
  const response = await fetch(`/api/operator/intent?intent_id=${encodeURIComponent(intentId)}`, {headers: {"X-Operator-Token": token}});
  if (!response.ok) {
    byId("operator-status").textContent = "Operator authorization failed or intent was not found.";
    return;
  }
  const result = await response.json();
  byId("operator-status").textContent = result.found ? `Attempt ${result.attempt_id}: ${result.status}` : "Intent not found.";
  byId("reconcile").hidden = !result.found;
  byId("reconcile").onclick = () => reconcile(intentId, token);
  byId("timeline").replaceChildren(...result.timeline.map((event) => {
    const item = document.createElement("li");
    item.textContent = `${event.sequence}. ${event.type} (${event.evidence_source})`;
    return item;
  }));
}

async function reconcile(intentId, token) {
  const response = await fetch("/api/operator/reconcile", {
    method: "POST", headers: {"Content-Type": "application/json", "X-Operator-Token": token},
    body: JSON.stringify({intent_id: intentId}),
  });
  byId("operator-status").textContent = response.ok ? "Reconciliation completed; reload the timeline." : "Reconciliation unavailable.";
}

if (byId("load")) byId("load").onclick = readCustomer;
if (byId("chat-form")) byId("chat-form").onsubmit = runChat;
if (byId("operator-load")) byId("operator-load").onclick = readOperator;
