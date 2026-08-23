"""Payment processing for Cafe OS.

Provider-agnostic gateway abstraction. Uses Stripe (via REST API, no SDK
dependency) when STRIPE_API_KEY is configured; falls back to a deterministic
mock gateway otherwise. Cash payments bypass the gateway entirely and settle
through the cash drawer.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from cafe_os.db import get_connection


class PaymentError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


SUPPORTED_METHODS = {"cash", "card", "mobile"}

ACCEPTED_GATEWAY_STATUSES = {"succeeded", "requires_capture"}


class PaymentGateway:
    name = "base"

    async def charge(self, order_id: str, amount: Decimal, payment_method: str) -> Dict[str, Any]:
        raise NotImplementedError


class MockPaymentGateway(PaymentGateway):
    """Always approves with a generated transaction id (MVP default)."""

    name = "mock"

    async def charge(self, order_id: str, amount: Decimal, payment_method: str) -> Dict[str, Any]:
        return {
            "provider": "mock",
            "status": "succeeded",
            "transaction_id": f"txn-{uuid.uuid4().hex[:12]}",
            "amount": float(amount),
        }


class StripePaymentGateway(PaymentGateway):
    """Creates Stripe PaymentIntents over the REST API.

    For the MVP terminal flow a PaymentIntent in `requires_capture` is treated
    as accepted and the order is marked paid.
    """

    name = "stripe"
    API_BASE = "https://api.stripe.com/v1"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def charge(self, order_id: str, amount: Decimal, payment_method: str) -> Dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(base_url=self.API_BASE, timeout=15.0) as client:
            response = await client.post(
                "/payment_intents",
                auth=(self.api_key, ""),
                data={
                    "amount": int((amount * 100).quantize(Decimal("1"))),
                    "currency": "usd",
                    "metadata[order_id]": order_id,
                    "metadata[payment_method]": payment_method,
                },
            )
        if response.status_code >= 400:
            raise PaymentError(f"stripe_error: {response.text}", status_code=502)
        body = response.json()
        return {
            "provider": "stripe",
            "status": body.get("status", "requires_capture"),
            "transaction_id": body.get("id"),
            "client_secret": body.get("client_secret"),
            "amount": float(amount),
        }


def get_gateway() -> PaymentGateway:
    api_key = os.environ.get("STRIPE_API_KEY", "")
    if api_key:
        return StripePaymentGateway(api_key)
    return MockPaymentGateway()


async def pay_order(order_id: str, payment_method: str, amount: Optional[float] = None) -> Dict[str, Any]:
    """Charge an order and persist payment state on the orders row."""
    if payment_method not in SUPPORTED_METHODS:
        raise PaymentError(f"Unsupported payment method: {payment_method}", status_code=400)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, total, payment_status FROM orders WHERE id = ?",
            (order_id,),
        )
        order = await cursor.fetchone()

    if not order:
        raise PaymentError(f"Order not found: {order_id}", status_code=404)
    if order["payment_status"] == "paid":
        raise PaymentError(f"Order {order_id} is already paid", status_code=409)

    order_total = Decimal(str(order["total"] or 0))
    if amount is None:
        charge_amount = order_total
    else:
        charge_amount = Decimal(str(amount))
        if abs(charge_amount - order_total) > Decimal("0.01"):
            raise PaymentError(
                f"Amount {float(charge_amount)} does not match order total {float(order_total)}",
                status_code=400,
            )

    if payment_method == "cash":
        result = {
            "provider": "cash_drawer",
            "status": "succeeded",
            "transaction_id": f"cash-{uuid.uuid4().hex[:12]}",
            "amount": float(charge_amount),
        }
    else:
        result = await get_gateway().charge(order_id, charge_amount, payment_method)

    if result.get("status") not in ACCEPTED_GATEWAY_STATUSES:
        raise PaymentError(f"Payment failed: {result.get('status')}", status_code=402)

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE orders SET payment_status = ?, payment_method = ? WHERE id = ?",
            ("paid", payment_method, order_id),
        )
        await conn.commit()

    receipt: Dict[str, Any] = {
        "order_id": order_id,
        "payment_status": "paid",
        "payment_method": payment_method,
        "amount": float(charge_amount),
        "provider": result["provider"],
        "transaction_id": result["transaction_id"],
    }
    if result.get("client_secret"):
        receipt["client_secret"] = result["client_secret"]
    return receipt
