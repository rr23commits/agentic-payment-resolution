"""Minimal Razorpay Test Mode order adapter."""

import base64
import json
import os
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RazorpayError(RuntimeError):
    """Raised when Razorpay cannot create a Test Mode order."""


def create_order(amount_paise: int, receipt: str) -> str:
    """Create one Razorpay Test Mode INR order and return its provider ID."""
    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id.startswith("rzp_test_") or not key_secret:
        raise RazorpayError("Razorpay Test Mode credentials are required")

    authorization = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    request = Request(
        "https://api.razorpay.com/v1/orders",
        data=json.dumps(
            {"amount": amount_paise, "currency": "INR", "receipt": receipt}
        ).encode(),
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            order_id = json.load(response).get("id")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, AttributeError, TypeError) as error:
        raise RazorpayError("Razorpay order creation failed") from error
    if not isinstance(order_id, str) or not order_id:
        raise RazorpayError("Razorpay returned no order ID")
    return order_id


def fetch_order_payments(order_id: str) -> list[dict]:
    """Fetch Razorpay's authoritative payment records for one existing order."""
    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id.startswith("rzp_test_") or not key_secret:
        raise RazorpayError("Razorpay Test Mode credentials are required")
    authorization = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    request = Request(
        f"https://api.razorpay.com/v1/orders/{quote(order_id, safe='')}/payments",
        headers={"Authorization": f"Basic {authorization}"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError) as error:
        raise RazorpayError("Razorpay reconciliation failed") from error
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RazorpayError("Razorpay returned invalid payment data")
    return items
