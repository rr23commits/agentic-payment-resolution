const $ = (id) => document.getElementById(id);
let mandate;
let products = [];
let selected = [];
let cart;
let transactions = [];
let poll;
let requestedQuantities = {};

const money = (paise) => paise == null ? "—" : `₹${(paise / 100).toFixed(2)}`;
const requestId = () => crypto.randomUUID();
const activePayment = new Set(["CREATED", "PENDING", "AMBIGUOUS"]);
const finalPayment = new Set(["CAPTURED", "FAILED", "REVERSED", "REFUNDED"]);
const catalogueCategoryAliases = {tshirt: "tshirts", tshirts: "tshirts", pant: "pants", pants: "pants", book: "books", books: "books"};

function extractRequestedQuantities(request) {
  const normalized = request.replace(/\bt[\s-]?shirts?\b/gi, "tshirts");
  const quantities = {};
  for (const match of normalized.matchAll(/\b(\d+)\s+([a-z]+)\b/gi)) {
    const category = catalogueCategoryAliases[match[2].toLowerCase()];
    if (category) quantities[category] = Number(match[1]);
  }
  return quantities;
}

function nav(view) {
  const actualView = view === "dashboard" ? "shop" : view;
  document.querySelectorAll(".customer-subnav button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".customer-view").forEach((section) => section.classList.toggle("active-view", section.id === `${actualView}-view`));
  if (actualView === "cart") renderCart();
  if (actualView === "transactions") loadTransactions();
}

function renderMandate() {
  if (!mandate?.id) return;
  $("max-limit").value = mandate.max_amount_paise / 100;
  renderCategoryPicker(mandate.allowed_categories_json);
  $("header-mandate").textContent = money(mandate.max_amount_paise);
  $("chat-submit").disabled = false;
}

function renderCategoryPicker(selectedCategories = []) {
  const categories = [...new Set(["books", "tshirts", "pants", ...selectedCategories])];
  const options = $("category-options"); options.replaceChildren(...categories.map((category) => {
    const label = document.createElement("label"); const input = document.createElement("input"); input.type = "checkbox"; input.value = category; input.checked = selectedCategories.includes(category); input.onchange = updateCategorySummary; label.append(input, document.createTextNode(category)); return label;
  })); updateCategorySummary();
}

function updateCategorySummary() {
  const selectedCategories = [...document.querySelectorAll("#category-options input:checked")].map((input) => input.value);
  const summary = $("category-summary"); summary.replaceChildren(document.createTextNode("Things I Want"), ...selectedCategories.map((category) => { const chip = document.createElement("span"); chip.className = "category-chip"; chip.textContent = category; return chip; }));
}

async function loadMandate() {
  const response = await fetch("/api/customer/mandate");
  if (response.ok) { mandate = await response.json(); renderMandate(); }
}

async function saveMandate(event) {
  event.preventDefault();
  const categories = [...document.querySelectorAll("#category-options input:checked")].map((input) => input.value);
  const response = await fetch("/api/customer/mandate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
    request_id: requestId(), max_amount_paise: Math.round(Number($("max-limit").value) * 100), allowed_categories: categories,
    expires_at: new Date(Date.now() + 30 * 86400000).toISOString(),
  })});
  $("mandate-status").textContent = response.ok ? "Mandate saved." : "Could not save mandate.";
  if (response.ok) { mandate = await response.json(); renderMandate(); }
}

function productCard(product) {
  const card = document.createElement("label"); card.className = "product-card";
  const top = document.createElement("span"); top.className = "product-card-top";
  const name = document.createElement("strong"); name.textContent = product.name;
  const price = document.createElement("b"); price.textContent = money(product.price_paise); top.append(name, price);
  const description = document.createElement("small"); description.className = "product-card-description"; description.textContent = product.description || product.category;
  const bottom = document.createElement("span"); bottom.className = "product-card-bottom";
  const input = document.createElement("input"); input.type = "checkbox"; input.checked = selected.some(({id}) => id === product.id);
  input.onchange = () => { selected = products.filter((item) => item.id === product.id ? input.checked : selected.some(({id}) => id === item.id)); renderProducts(); };
  const quantity = document.createElement("input"); quantity.type = "number"; quantity.min = "1"; quantity.max = "1000"; quantity.value = selected.find(({id}) => id === product.id)?.quantity || requestedQuantities[product.category] || 1; quantity.setAttribute("aria-label", `Quantity of ${product.name}`);
  const total = document.createElement("span"); total.className = "product-card-total"; total.textContent = money(product.price_paise * Number(quantity.value));
  quantity.onchange = () => { const item = selected.find(({id}) => id === product.id); if (item) item.quantity = Math.max(1, Number(quantity.value) || 1); total.textContent = money(product.price_paise * Number(quantity.value)); };
  bottom.append(input, quantity, total); card.append(top, description, bottom); return card;
}

function renderProducts() {
  const box = $("recommendations"); box.replaceChildren();
  const groups = new Map();
  products.forEach((product) => groups.set(product.category, [...(groups.get(product.category) || []), product]));
  if (!products.length) { box.className = "recommendations empty-state"; box.textContent = "Ask the agent to see products."; }
  else {
    box.className = "recommendations";
    groups.forEach((items, category) => { const section = document.createElement("section"); const heading = document.createElement("h3"); heading.textContent = category === "tshirts" ? "T-Shirts" : category; section.append(heading, ...items.map(productCard)); box.append(section); });
  }
  $("select-products").classList.toggle("hidden", !selected.length);
}

async function askAgent(event) {
  event.preventDefault(); $("chat-status").textContent = "Searching…";
  const response = await fetch("/api/customer/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({request: $("chat-request").value})});
  const result = await response.json();
  if (!response.ok) { $("chat-status").textContent = result.error || "The agent could not respond."; return; }
  const catalogueResults = result.history.flatMap((entry) => entry.tool === "search_catalogue" && Array.isArray(entry.result) ? entry.result : []);
  $("chat-status").textContent = catalogueResults.length ? (result.message || "I found these options for you.").split(/\n\s*\n/)[0] : (result.message || "The agent could not find matching products.");
  requestedQuantities = extractRequestedQuantities($("chat-request").value);
  products = [...new Map(catalogueResults.filter((product) => product && product.id).map((product) => [product.id, product])).values()];
  selected = []; renderProducts();
  if (result.checkout?.intent_id) {
    const persisted = await loadIntent(result.checkout.intent_id);
    if (result.checkout.allowed !== false && persisted?.status === "PENDING" && !persisted.payment_id && persisted.checkout?.order_id) {
      window.openRazorpayCheckout(persisted.checkout);
    }
  }
}

async function reviewCart() {
  if (!selected.length || !mandate) return;
  const response = await fetch("/api/customer/cart", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
    mandate_id: mandate.id, items: selected.map(({id, quantity = 1}) => ({product_id: id, quantity})),
  })});
  cart = await response.json();
  if (!response.ok) { $("chat-status").textContent = cart.error || "The requested quantity cannot be fulfilled."; return; }
  nav("cart"); renderCart();
}

function renderCart() {
  const hasCart = Boolean(cart?.cart_id);
  $("cart-empty").classList.toggle("hidden", hasCart);
  $("cart-items").replaceChildren(...(hasCart ? selected.map((product) => { const row = document.createElement("div"); row.className = "cart-row"; const name = document.createElement("span"); name.textContent = `${product.name} × ${product.quantity || 1}`; const price = document.createElement("strong"); price.textContent = money(product.price_paise * (product.quantity || 1)); row.append(name, price); return row; }) : []));
  const count = hasCart ? selected.reduce((total, product) => total + (product.quantity || 1), 0) : 0; $("cart-count").textContent = hasCart ? `(${count})` : ""; $("cart-count-label").textContent = hasCart ? `${count} item${count === 1 ? "" : "s"}` : ""; $("shop-cart-summary").textContent = hasCart ? `${count} · ${money(cart.cart_total_paise)}` : "";
  if (!hasCart) return;
  $("cart-total").textContent = money(cart.cart_total_paise); $("cart-mandate").textContent = money(cart.mandate_cap_paise);
  $("cart-status").textContent = cart.allowed ? "Within your mandate. Checkout is ready." : (cart.reasons || []).join(" ");
  const increase = $("increase-mandate");
  if (!cart.allowed && cart.cart_total_paise > cart.mandate_cap_paise) {
    const suggested = Math.ceil(cart.cart_total_paise / 10000) * 10000; increase.textContent = `Increase mandate to ${money(suggested)}`; increase.hidden = false; increase.classList.remove("hidden"); increase.onclick = increaseMandate;
  } else { increase.hidden = true; increase.classList.add("hidden"); }
  $("launch").hidden = !cart.allowed; $("launch").classList.toggle("hidden", !cart.allowed); $("launch").onclick = createCheckout;
}

async function increaseMandate() {
  const response = await fetch("/api/customer/mandate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "increase", mandate_id: mandate.id, cart_id: cart.cart_id, request_id: requestId()})});
  if (response.ok) { mandate = await response.json(); renderMandate(); await reviewCart(); }
}

async function createCheckout() {
  const response = await fetch("/api/customer/checkout", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({cart_id: cart.cart_id, mandate_id: mandate.id, request_id: requestId()})});
  const result = await response.json();
  if (response.ok && result.intent_id) { await loadTransactions(null, false); showCheckoutReady(result); if (result.order_id) window.openRazorpayCheckout(result); }
  else $("cart-status").textContent = (result.reasons || [result.message || "Checkout blocked."]).join(" ");
}

function showCheckoutReady(result) {
  $("intent-id").value = result.intent_id; $("payment-panel").classList.remove("hidden"); $("payment-title").textContent = "Checkout ready"; $("payment-copy").textContent = "Razorpay will open to complete your payment."; $("payment-alert").className = "alert pending"; $("checkout-step").textContent = "✓ Checkout"; $("submitted-step").textContent = "○ Payment submitted"; $("confirm-step").textContent = "○ Confirmation pending"; $("order-id").textContent = result.order_id || "—"; $("payment-id").textContent = "—";
}

function renderPayment(result) {
  const submitted = Boolean(result.payment_id);
  if (finalPayment.has(result.status)) { clearInterval(poll); poll = undefined; }
  $("payment-panel").classList.remove("hidden"); $("payment-title").textContent = result.status === "CAPTURED" ? "Payment confirmed" : submitted || result.status === "AMBIGUOUS" ? result.message || "Payment is being confirmed." : "Checkout ready";
  $("payment-copy").textContent = result.status === "CAPTURED" ? "Your order is confirmed." : submitted || result.status === "AMBIGUOUS" ? "Your payment is being verified. Do not retry." : "Razorpay will open to complete your payment.";
  $("payment-alert").className = `alert ${result.status === "CAPTURED" ? "success" : finalPayment.has(result.status) ? "error" : "pending"}`;
  $("checkout-step").textContent = "✓ Checkout"; $("submitted-step").textContent = result.payment_id ? "✓ Payment submitted" : "○ Payment submitted"; $("confirm-step").textContent = result.status === "CAPTURED" ? "✓ Payment confirmed" : "○ Confirmation pending";
  $("order-id").textContent = result.order_id || "—"; $("payment-id").textContent = result.payment_id || "—";
  $("view-transaction").classList.toggle("hidden", result.status !== "CAPTURED");
  $("view-transaction").onclick = () => { nav("transactions"); loadIntent(result.intent_id); };
  if (result.status === "PENDING" || result.status === "AMBIGUOUS") { clearInterval(poll); poll = setInterval(() => loadIntent(result.intent_id), 2000); }
}

async function loadIntent(intentId) {
  $("intent-id").value = intentId; const response = await fetch(`/api/customer/intent?intent_id=${encodeURIComponent(intentId)}`); if (!response.ok) return;
  const result = await response.json(); if (result.found) { renderPayment(result); renderTransaction(result); } return result;
}

function renderTransaction(result) {
  $("transaction-detail").classList.remove("hidden"); $("confirmation-title").textContent = result.status === "CAPTURED" ? "Payment confirmed" : result.message || "Payment status"; $("confirmation-copy").textContent = result.status === "CAPTURED" ? "Your order is confirmed." : "This status is backed by the payment record.";
  $("transaction-order").textContent = result.order_id || "—"; $("transaction-payment").textContent = result.payment_id || "—";
  $("processing-timeline").replaceChildren(...(result.timeline || []).map((event) => { const row = document.createElement("div"); const title = document.createElement("strong"); title.textContent = event.type; const detail = document.createElement("small"); detail.textContent = event.detail?.status || event.evidence_source || "Recorded"; row.append(title, detail); return row; }));
}

function renderTransactionList() {
  const list = $("transaction-list"); list.replaceChildren(...transactions.map((transaction) => { const button = document.createElement("button"); button.className = "transaction-choice"; button.type = "button"; const name = document.createElement("strong"); name.textContent = transaction.product; const summary = document.createElement("span"); summary.textContent = `${money(transaction.amount_paise)} · ${transaction.status}`; button.append(name, summary); button.onclick = () => loadIntent(transaction.intent_id); return button; }));
  $("transaction-empty").classList.toggle("hidden", transactions.length > 0);
}

async function loadTransactions(preferred, select = true) {
  const response = await fetch("/api/customer/transactions"); if (!response.ok) return; transactions = (await response.json()).transactions || []; renderTransactionList();
  if (!select) return;
  const selectedId = preferred || transactions.find(({status}) => activePayment.has(status))?.intent_id || localStorage.getItem("customer.intentId") || transactions[0]?.intent_id;
  if (selectedId) { localStorage.setItem("customer.intentId", selectedId); await loadIntent(selectedId); }
}

window.onRazorpayClientPayment = (_checkout, payment) => { renderPayment({intent_id: $("intent-id").value, status: "PENDING", payment_id: payment.razorpay_payment_id, order_id: payment.razorpay_order_id, message: "Payment submitted. Confirmation pending."}); loadIntent($("intent-id").value); };
window.onRazorpayDismiss = (checkout) => { fetch("/checkout/client-cancel", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({intent_id: checkout.intent_id})}).then((response) => { $("cart-status").textContent = response.ok ? "Payment is still being confirmed. Do not retry." : "Payment is being confirmed. Do not retry."; }).catch(() => { $("cart-status").textContent = "Payment is being confirmed. Do not retry."; }); };
$("mandate-form").onsubmit = saveMandate; $("chat-form").onsubmit = askAgent; $("select-products").onclick = reviewCart; $("open-cart").onclick = () => nav("cart"); $("edit-mandate").onclick = () => { nav("dashboard"); $("max-limit").focus(); };
document.querySelectorAll(".customer-subnav button").forEach((button) => button.onclick = () => nav(button.dataset.view));
document.addEventListener("click", (event) => { const picker = $("category-picker"); if (picker?.open && !picker.contains(event.target)) picker.open = false; });
renderCategoryPicker(); loadMandate(); loadTransactions(); renderProducts();
