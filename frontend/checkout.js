window.openRazorpayCheckout = function (checkout) {
  const status = document.getElementById("payment-status");
  const waiting = "Payment is being confirmed";
  status.textContent = waiting;
  const demoTimeout = document.getElementById("demo-timeout");
  demoTimeout.hidden = false;
  demoTimeout.onclick = () => fetch("/checkout/client-timeout", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({intent_id: checkout.intent_id, event: "timeout"}),
  }).then((response) => {
    status.textContent = response.ok
      ? "Payment is still being confirmed. Do not retry."
      : waiting;
  }).catch(() => { status.textContent = waiting; });

  new window.Razorpay({
    key: checkout.razorpay_key_id,
    order_id: checkout.order_id,
    handler(response) {
      fetch("/checkout/client-report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          intent_id: checkout.intent_id,
          razorpay_order_id: response.razorpay_order_id,
          razorpay_payment_id: response.razorpay_payment_id,
        }),
      }).catch(() => {});
      status.textContent = waiting;
    },
  }).open();
};
