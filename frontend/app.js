const byId = (id) => document.getElementById(id);
let customerPoll;
let customerData;
let transactions = [];
let mandate;
let recommendations = [];
let selectedProducts = [];
let cartState;

const money = (paise) => paise == null ? "—" : `₹${(paise / 100).toFixed(2)}`;
const shortId = (value) => value || "—";
const finalStatuses = new Set(["CAPTURED", "FAILED", "REVERSED", "REFUNDED"]);
const productRow = (item) => {
  const row = document.createElement("div"); row.className = "cart-item";
  const cover = document.createElement("span"); cover.className = "book-cover"; cover.setAttribute("aria-hidden", "true");
  const info = document.createElement("span"); const name = document.createElement("strong"); name.textContent = item.name;
  const category = document.createElement("small"); category.textContent = item.category; info.append(name, category);
  const price = document.createElement("span"); price.className = "price"; price.textContent = money(item.price_paise * item.quantity);
  row.append(cover, info, price); return row;
};

const statusCopy = {
  PENDING: ["Payment is being confirmed.", "We received your payment attempt and are waiting for authoritative confirmation. Do not retry."],
  AMBIGUOUS: ["Payment is still being confirmed.", "We received an uncertain payment signal. Do not retry."],
  CAPTURED: ["Payment confirmed.", "Your payment has been successfully confirmed and processed."],
  FAILED: ["Payment failed.", "The payment was not confirmed by the provider."],
  REVERSED: ["Payment reversed.", "The provider reversed this payment."],
  REFUNDED: ["Payment refunded.", "The provider refunded this payment."],
};

function renderStatus(result, prefix = "payment") {
  const [title, defaultCopy] = statusCopy[result.status] || ["Payment status unavailable.", ""];
  const copy = result.status === "PENDING" && result.payment_id
    ? "We received your payment attempt."
    : defaultCopy;
  const alert = byId(`${prefix}-alert`) || byId("transaction-banner");
  const titleNode = byId(prefix === "payment" ? "payment-title" : "confirmation-title");
  const copyNode = byId(prefix === "payment" ? "payment-copy" : "confirmation-copy");
  titleNode.textContent = title; copyNode.textContent = copy;
  const icon = alert.querySelector("span"); if (icon) icon.textContent = result.status === "CAPTURED" ? "✓" : "!";
  alert.classList.toggle("success", result.status === "CAPTURED");
  alert.classList.toggle("error", finalStatuses.has(result.status) && result.status !== "CAPTURED");
  alert.classList.toggle("pending", ["PENDING", "AMBIGUOUS"].includes(result.status));
}

function renderCart(result) {
  const items = result.items || [];
  byId("cart-card").classList.toggle("hidden", !items.length);
  byId("cart-items").replaceChildren(...items.map((item) => {
    const row = document.createElement("div");
    row.className = "cart-item";
    return productRow(item);
  }));
  byId("remaining-cap").textContent = result.mandate_cap_paise == null ? "—" : money(result.mandate_cap_paise - result.cart_total_paise);
  const item = items[0];
  byId("transaction-item").replaceChildren(...(item ? [productRow(item)] : []));
  byId("transaction-subtotal").textContent = money(result.cart_total_paise);
  byId("transaction-tax").textContent = money(result.tax_paise);
  byId("transaction-total").textContent = money(result.cart_total_paise + (result.tax_paise || 0));
  byId("transaction-merchant").textContent = shortId(result.merchant_id);
  byId("transaction-mandate-id").textContent = shortId(result.mandate_id);
  byId("transaction-mandate").textContent = `(Max: ${money(result.mandate_cap_paise)})`;
  byId("cap-used").textContent = `${money(result.cart_total_paise)} of ${money(result.mandate_cap_paise)}`;
  byId("cap-fill").style.width = result.mandate_cap_paise ? `${Math.min(100, result.cart_total_paise / result.mandate_cap_paise * 100)}%` : "0%";
}

function renderRecommendations(products) {
  recommendations = products;
  const box = byId("recommendations");
  box.classList.toggle("hidden", !products.length);
  box.replaceChildren(...products.map((product) => {
    const label = document.createElement("label"); label.className = "cart-item";
    const input = document.createElement("input"); input.type = "checkbox"; input.value = product.id;
    input.checked = selectedProducts.some((item) => item.id === product.id);
    input.onchange = () => { selectedProducts = recommendations.filter((item) => box.querySelector(`input[value="${CSS.escape(item.id)}"]`)?.checked); byId("select-products").classList.toggle("hidden", !selectedProducts.length); };
    const info = document.createElement("span"); const name = document.createElement("strong"); name.textContent = product.name; const category = document.createElement("small"); category.textContent = product.category; info.append(name, category);
    const price = document.createElement("span"); price.className = "price"; price.textContent = money(product.price_paise); label.append(input, info, price); return label;
  }));
  byId("select-products").classList.toggle("hidden", !products.length || !selectedProducts.length);
}

async function saveMandate(event) {
  event.preventDefault();
  const categories = [...document.querySelectorAll("input[name=category]:checked")].map((input) => input.value);
  const response = await fetch("/api/customer/mandate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
    request_id: crypto.randomUUID(), max_amount_paise: Math.round(Number(byId("max-limit").value) * 100), allowed_categories: categories,
    expires_at: new Date(Date.now() + 30 * 86400000).toISOString(),
  })});
  byId("mandate-status").textContent = response.ok ? "Mandate saved." : "Could not save mandate.";
  if (response.ok) { mandate = await response.json(); byId("chat-submit").disabled = false; }
}

async function reviewProducts() {
  if (!selectedProducts.length || !mandate) return;
  const response = await fetch("/api/customer/cart", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
    mandate_id: mandate.id, items: selectedProducts.map(({id}) => ({product_id: id, quantity: 1})),
  })});
  cartState = await response.json();
  byId("cart-card").classList.remove("hidden");
  byId("cart-status").textContent = cartState.allowed ? "Within your mandate. Checkout is ready." : (cartState.reasons || []).join(" ");
  const increase = byId("increase-mandate");
  if (!cartState.allowed && cartState.cart_total_paise > cartState.mandate_cap_paise) {
    const suggested = Math.ceil(cartState.cart_total_paise / 10000) * 10000;
    increase.textContent = `Increase mandate to ${money(suggested)}`; increase.classList.remove("hidden");
    increase.onclick = () => increaseMandate();
  } else increase.classList.add("hidden");
  byId("cart-items").replaceChildren(...selectedProducts.map((product) => productRow({...product, quantity: 1})));
  byId("remaining-cap").textContent = money((mandate.max_amount_paise || 0) - cartState.total_paise);
  byId("launch").hidden = !cartState.allowed;
  byId("launch").textContent = "Create checkout";
  byId("launch").onclick = createCheckout;
}

async function increaseMandate() {
  const response = await fetch("/api/customer/mandate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "increase", mandate_id: mandate.id, cart_id: cartState.cart_id, request_id: crypto.randomUUID()})});
  if (response.ok) { mandate = await response.json(); byId("mandate-status").textContent = "Mandate increased."; await reviewProducts(); }
}

async function createCheckout() {
  const response = await fetch("/api/customer/checkout", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({cart_id: cartState.cart_id, mandate_id: mandate.id, request_id: crypto.randomUUID()})});
  const result = await response.json();
  if (response.ok && result.intent_id) { await readTransactions(result.intent_id); }
  else byId("cart-status").textContent = (result.reasons || [result.message || "Checkout blocked."]).join(" ");
}

function renderCustomer(result) {
  customerData = result;
  const selected = transactions.find((transaction) => transaction.intent_id === result.intent_id);
  if (selected) {
    selected.status = result.status; selected.order_id = result.order_id; selected.payment_id = result.payment_id;
    renderTransactionHistory();
  }
  byId("chat-submit").disabled = !mandate || transactions.some(({status}) => ["CREATED", "PENDING", "AMBIGUOUS"].includes(status));
  byId("payment-status").textContent = result.found ? result.message : "Checkout not found.";
  if (result.found) renderStatus(result);
  byId("checkout-step").textContent = result.found ? "● Checkout" : "○ Checkout";
  byId("checkout-step").className = result.found ? "done" : "";
  byId("submitted-step").textContent = result.payment_id ? "● Payment submitted" : "○ Payment submitted";
  byId("submitted-step").className = result.payment_id ? "done" : "";
  byId("confirm-step").textContent = result.status === "CAPTURED" ? "● Confirmation complete" : "○ Confirmation pending";
  byId("confirm-step").className = result.status === "CAPTURED" ? "success" : "current";
  byId("confirm-step").classList.toggle("success", result.status === "CAPTURED");
  byId("order-id").textContent = shortId(result.order_id);
  byId("payment-id").textContent = shortId(result.payment_id);
  byId("mandate-cap").textContent = money(result.mandate_cap_paise);
  const submitted = Boolean(result.payment_id);
  const captured = result.status === "CAPTURED";
  byId("launch").hidden = !result.found || (!captured && (!result.checkout || submitted));
  byId("launch").textContent = captured ? "View Transaction →" : "Launch Checkout";
  byId("launch").onclick = captured
    ? () => showView("transactions")
    : () => window.openRazorpayCheckout(result.checkout);
  byId("demo-timeout").hidden = !result.checkout || submitted;
  renderCart(result);
  renderTransaction(result);
  if (result.found && ["PENDING", "AMBIGUOUS"].includes(result.status)) customerPoll = setInterval(readCustomer, 2000);
}

const eventLabels = {
  CHECKOUT_INTENT_CREATED: ["Order initiated", "Purchase request created."],
  RAZORPAY_ORDER_CREATED: ["Checkout ready", "Provider order created."],
  CUSTOMER_MESSAGE: ["Agent update", "The purchase assistant recorded an update."],
  CLIENT_REPORTED: ["Payment submitted", "Browser reported a payment reference. Waiting for provider confirmation."],
  CLIENT_REPORT_REJECTED: ["Client confirmation received", "Browser evidence was recorded; provider/webhook evidence remains authoritative."],
  WEBHOOK_RECEIVED: ["Provider evidence received", "Verified provider event received."],
  ATTEMPT_RESOLVED: ["Payment confirmed", "Authoritative payment state recorded."],
  RECONCILIATION_CHECKED: ["Reconciliation checked", "Existing provider state was checked."],
};

function renderTimeline(events) {
  byId("processing-timeline").replaceChildren(...(events || []).map((event) => {
    const [label, description] = eventLabels[event.type] || [event.type, "Recorded in the payment audit trail."];
    const item = document.createElement("div"); item.className = event.type === "ATTEMPT_RESOLVED" ? "complete" : "recorded";
    const icon = document.createElement("span"); icon.textContent = event.type === "ATTEMPT_RESOLVED" ? "✓" : "•";
    const details = document.createElement("div"); const title = document.createElement("strong"); title.textContent = label; const copy = document.createElement("small"); copy.textContent = description; details.append(title, copy); item.append(icon, details);
    return item;
  }));
}

function renderTransaction(result) {
  if (!result.found) return;
  renderStatus(result, "transaction");
  byId("transaction-payment").textContent = shortId(result.payment_id);
  byId("transaction-order").textContent = shortId(result.order_id);
  byId("agent-status-copy").textContent = result.status === "CAPTURED" ? "Transaction complete. No further action required." : result.message;
  renderTimeline(result.timeline);
}

async function readCustomer() {
  clearInterval(customerPoll);
  const intentId = byId("intent-id").value.trim();
  if (!intentId) return;
  const response = await fetch(`/api/customer/intent?intent_id=${encodeURIComponent(intentId)}`);
  renderCustomer(await response.json());
}

async function runChat(event) {
  event.preventDefault();
  byId("chat-status").textContent = "Asking the agent…";
  const response = await fetch("/api/customer/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({request: byId("chat-request").value})});
  const result = await response.json();
  if (!response.ok) { byId("chat-status").textContent = result.error || "The agent could not respond."; return; }
  byId("chat-status").textContent = result.message;
  renderRecommendations(result.history.flatMap((entry) => entry.tool === "search_catalogue" && Array.isArray(entry.result) ? entry.result : []));
  if (result.checkout) await readTransactions(result.checkout.intent_id);
}

async function readMandate() {
  const response = await fetch("/api/customer/mandate");
  if (!response.ok) return;
  mandate = await response.json();
  if (!mandate.id) { byId("chat-submit").disabled = true; return; }
  byId("max-limit").value = mandate.max_amount_paise / 100;
  document.querySelectorAll("input[name=category]").forEach((input) => { input.checked = mandate.allowed_categories_json.includes(input.value); });
}

function showView(view) {
  document.querySelectorAll(".subnav button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".view").forEach((section) => section.classList.toggle("active-view", section.id === `${view}-view`));
  byId("transaction-history").classList.toggle("hidden", view !== "transactions");
  if (view === "transactions" && customerData) renderCustomer(customerData);
}

function renderTransactionHistory() {
  byId("transaction-list").replaceChildren(...transactions.map((transaction) => {
    const button = document.createElement("button");
    button.className = `transaction-choice ${transaction.intent_id === byId("intent-id").value ? "selected" : ""}`;
    button.type = "button";
    const product = document.createElement("strong"); product.textContent = transaction.product;
    const summary = document.createElement("span"); summary.textContent = `${money(transaction.amount_paise)} · ${transaction.status} · ${shortId(transaction.order_id || transaction.payment_id)}`;
    const created = document.createElement("small"); created.textContent = new Date(transaction.created_at).toLocaleString(); button.append(product, summary, created);
    button.onclick = () => selectTransaction(transaction.intent_id);
    return button;
  }));
}

async function selectTransaction(intentId) {
  byId("intent-id").value = intentId;
  localStorage.setItem("customer.intentId", intentId);
  renderTransactionHistory();
  await readCustomer();
}

async function readTransactions(preferredIntentId) {
  const response = await fetch("/api/customer/transactions");
  if (!response.ok) return;
  transactions = (await response.json()).transactions || [];
  const active = transactions.find(({status}) => ["CREATED", "PENDING", "AMBIGUOUS"].includes(status));
  const stored = localStorage.getItem("customer.intentId");
  const selected = preferredIntentId || active?.intent_id ||
    (transactions.some(({intent_id}) => intent_id === stored) ? stored : transactions[0]?.intent_id);
  renderTransactionHistory();
  if (selected) await selectTransaction(selected);
}

function renderOperator(result, token) {
  byId("operator-content").classList.toggle("hidden", !result.found);
  if (!result.found) { byId("operator-status").textContent = "Intent not found."; return; }
  byId("operator-status").textContent = `Attempt ${result.attempt_id}: ${result.status}`;
  byId("attempt-id").textContent = result.attempt_id;
  byId("attempt-transition").textContent = result.status === "CAPTURED" ? "PENDING → CAPTURED" : `${result.status} (Awaiting Authority)`;
  byId("operator-amount").textContent = money(result.total_paise);
  byId("operator-state").textContent = result.status;
  byId("operator-order").textContent = shortId(result.razorpay_order_id);
  byId("operator-payment").textContent = shortId(result.razorpay_payment_id);
  byId("operator-cap").textContent = money(result.max_amount_paise);
  byId("operator-authoritative-at").textContent = result.last_authoritative_at ? new Date(result.last_authoritative_at).toLocaleString() : "Awaiting authority";
  const eventTypes = new Set((result.timeline || []).map((event) => event.type));
  const checks = [["Client payment ID received", Boolean(result.razorpay_payment_id)], ["Razorpay webhook received", eventTypes.has("WEBHOOK_RECEIVED")], ["Webhook signature verified", eventTypes.has("WEBHOOK_RECEIVED")], ["Event persisted", eventTypes.has("WEBHOOK_RECEIVED")], ["Payment matched", eventTypes.has("WEBHOOK_RECEIVED")], [`Authoritative state: ${result.status}`, eventTypes.has("ATTEMPT_RESOLVED")]];
  byId("evidence-checklist").replaceChildren(...checks.map(([label, done]) => { const row = document.createElement("div"); row.className = done ? "complete" : ""; const icon = document.createElement("span"); icon.textContent = done ? "✓" : "○"; row.append(icon, document.createTextNode(label)); return row; }));
  byId("reconcile").disabled = ["CAPTURED", "FAILED", "REVERSED", "REFUNDED"].includes(result.status);
  byId("reconcile").onclick = () => reconcile(result.attempt_id, token);
  byId("timeline").replaceChildren(...(result.timeline || []).map((event) => { const item = document.createElement("div"); const [label, description] = eventLabels[event.type] || [event.type, "Recorded in the payment audit trail."]; item.className = `audit-event ${["WEBHOOK_RECEIVED", "ATTEMPT_RESOLVED"].includes(event.type) ? "authoritative" : ""}`; const time = document.createElement("time"); time.textContent = new Date(event.created_at).toLocaleTimeString(); const title = document.createElement("strong"); title.textContent = label; const copy = document.createElement("small"); copy.textContent = event.type === "ATTEMPT_RESOLVED" ? `${description} State: ${event.detail?.status || result.status}.` : description; item.append(time, title, copy); return item; }));
}

async function readOperator() {
  const token = byId("operator-token").value;
  const response = await fetch(`/api/operator/intent?intent_id=${encodeURIComponent(byId("operator-intent").value.trim())}`, {headers: {"X-Operator-Token": token}});
  if (!response.ok) { byId("operator-status").textContent = "Operator authorization failed or intent was not found."; byId("operator-content").classList.add("hidden"); return; }
  renderOperator(await response.json(), token);
}

async function readOperatorTransactions() {
  const token = byId("operator-token").value;
  const response = await fetch(`/api/operator/transactions?q=${encodeURIComponent(byId("operator-search").value.trim())}`, {headers: {"X-Operator-Token": token}});
  if (!response.ok) { byId("operator-status").textContent = "Operator authorization failed."; return; }
  const rows = (await response.json()).transactions || [];
  byId("operator-transactions").replaceChildren(...rows.map((row) => { const item = document.createElement("div"); item.className = "transaction-choice"; const text = document.createElement("span"); text.textContent = `${row.status} · ${money(row.amount_paise)} · ${row.order_id || row.payment_id || row.intent_id}`; const view = document.createElement("button"); view.type = "button"; view.className = "secondary-button"; view.textContent = "View"; view.onclick = () => readOperator(row.intent_id); item.append(text, view); return item; }));
  byId("operator-status").textContent = `${rows.length} transaction${rows.length === 1 ? "" : "s"} found.`;
}

async function reconcile(attemptId, token) {
  const response = await fetch("/api/operator/reconcile", {method: "POST", headers: {"Content-Type": "application/json", "X-Operator-Token": token}, body: JSON.stringify({attempt_id: attemptId})});
  byId("operator-status").textContent = response.ok ? "Reconciliation completed." : "Reconciliation unavailable.";
  if (response.ok) readOperator();
}

document.querySelectorAll(".subnav button").forEach((button) => button.onclick = () => showView(button.dataset.view));
if (byId("load")) byId("load").onclick = readCustomer;
if (byId("chat-form")) byId("chat-form").onsubmit = runChat;
if (byId("mandate-form")) { byId("mandate-form").onsubmit = saveMandate; readMandate(); }
if (byId("select-products")) byId("select-products").onclick = reviewProducts;
if (byId("operator-form")) byId("operator-form").addEventListener("submit", (event) => { event.preventDefault(); readOperator(); });
if (byId("operator-discovery")) byId("operator-discovery").addEventListener("submit", (event) => { event.preventDefault(); readOperatorTransactions(); });
if (byId("transaction-history")) readTransactions();
