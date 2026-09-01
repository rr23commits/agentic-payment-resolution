window.openRazorpayCheckout = function (checkout) {
  const status = document.getElementById("payment-status");
  const waiting = "Payment is being confirmed";
  let submitted = false;
  if (status) status.textContent = "Razorpay checkout opened";
  const demoTimeout = document.getElementById("demo-timeout");
  if (demoTimeout) demoTimeout.hidden = false;
  if (demoTimeout) demoTimeout.onclick = () => fetch("/checkout/client-timeout", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({intent_id: checkout.intent_id, event: "timeout"}),
  }).then((response) => {
    if (status) status.textContent = response.ok
      ? "Payment is still being confirmed. Do not retry."
      : waiting;
  }).catch(() => { if (status) status.textContent = waiting; });

  new window.Razorpay({
    key: checkout.razorpay_key_id,
    order_id: checkout.order_id,
    handler(response) {
      submitted = true;
      fetch("/checkout/client-report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          intent_id: checkout.intent_id,
          razorpay_order_id: response.razorpay_order_id,
          razorpay_payment_id: response.razorpay_payment_id,
        }),
      }).catch(() => {});
      if (status) status.textContent = waiting;
      if (typeof window.onRazorpayClientPayment === "function") window.onRazorpayClientPayment(checkout, response);
    },
    modal: {ondismiss() { if (!submitted && typeof window.onRazorpayDismiss === "function") window.onRazorpayDismiss(checkout); }},
  }).open();
};
