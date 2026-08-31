"""Small local HTTP harness for delayed or replayed signed Razorpay events."""

import argparse
import hashlib
import hmac
import json
import os
from urllib.request import Request, urlopen
from uuid import uuid4


def replay_webhook(order_id: str, payment_id: str, status: str, delay: float, url: str) -> None:
    body = json.dumps({
        "event": f"payment.{status}",
        "payload": {"payment": {"entity": {"id": payment_id, "order_id": order_id}}},
    }, separators=(",", ":")).encode()
    secret = os.environ["RAZORPAY_WEBHOOK_SECRET"].encode()
    request = Request(
        f"{url.rstrip('/')}/webhooks/razorpay", data=body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": hmac.new(secret, body, hashlib.sha256).hexdigest(),
            "X-Razorpay-Event-Id": f"demo_{uuid4().hex}",
            **({"X-Demo-Webhook-Delay": str(delay)} if os.environ.get("DEMO_MODE") == "1" else {}),
        }, method="POST",
    )
    with urlopen(request) as response:
        print(response.read().decode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--payment-id", default="pay_demo")
    parser.add_argument("--status", choices=("captured", "failed", "reversed", "refunded"), default="captured")
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    replay_webhook(args.order_id, args.payment_id, args.status, args.delay, args.url)
