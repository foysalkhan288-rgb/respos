"""
Integration tests for KDS WebSocket push, KDS REST fallback, payments,
and real sales-volume-based menu engineering.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_order(client: AsyncClient, counter: str = "C9", **overrides) -> dict:
    payload = {
        "counter_number": counter,
        "items": [
            {
                "menu_item_id": "espresso-001",
                "quantity": 1,
                "modifiers": [],
                "special_instructions": "",
            }
        ],
    }
    payload.update(overrides)
    response = await client.post("/api/v1/orders", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pay_order_cash_success(client: AsyncClient):
    data = await _create_order(client)
    order_id = data["order_id"]
    assert data["payment_status"] == "pending"

    resp = await client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"payment_method": "cash"},
    )
    assert resp.status_code == 200, resp.text
    receipt = resp.json()
    assert receipt["order_id"] == order_id
    assert receipt["payment_status"] == "paid"
    assert receipt["payment_method"] == "cash"
    assert receipt["provider"] == "cash_drawer"
    assert receipt["transaction_id"].startswith("cash-")
    assert receipt["amount"] == pytest.approx(data["total"], abs=0.01)

    # Order now reflects paid status
    resp = await client.get(f"/api/v1/orders/{order_id}")
    assert resp.status_code == 200
    assert resp.json()["payment_status"] == "paid"


@pytest.mark.asyncio
async def test_pay_order_card_mock_gateway(client: AsyncClient):
    data = await _create_order(client, counter="C10")
    resp = await client.post(
        f"/api/v1/orders/{data['order_id']}/pay",
        json={"payment_method": "card"},
    )
    assert resp.status_code == 200, resp.text
    receipt = resp.json()
    assert receipt["provider"] == "mock"
    assert receipt["transaction_id"].startswith("txn-")
    assert receipt["payment_status"] == "paid"


@pytest.mark.asyncio
async def test_pay_order_explicit_amount_must_match_total(client: AsyncClient):
    data = await _create_order(client, counter="C11")
    order_id = data["order_id"]

    resp = await client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"payment_method": "cash", "amount": 0.01},
    )
    assert resp.status_code == 400

    # Matching amount succeeds
    resp = await client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"payment_method": "cash", "amount": data["total"]},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_pay_order_already_paid_conflict(client: AsyncClient):
    data = await _create_order(client, counter="C12", payment_method="mobile")
    order_id = data["order_id"]

    resp = await client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"payment_method": "mobile"},
    )
    assert resp.status_code == 200

    resp = await client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"payment_method": "mobile"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_pay_nonexistent_order(client: AsyncClient):
    resp = await client.post(
        "/api/v1/orders/ord-nonexistent/pay",
        json={"payment_method": "cash"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pay_unsupported_payment_method(client: AsyncClient):
    resp = await client.post(
        "/api/v1/orders/ord-whatever/pay",
        json={"payment_method": "bitcoin"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_order_persists_payment_method(client: AsyncClient):
    """payment_method supplied at order creation lands in the orders table."""
    import aiosqlite
    import cafe_os.db as db_module

    data = await _create_order(client, counter="C13", payment_method="mobile")

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT payment_method FROM orders WHERE id = ?", (data["order_id"],)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row["payment_method"] == "mobile"


# ---------------------------------------------------------------------------
# Kitchen Display System
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kds_orders_rest_fallback(client: AsyncClient):
    data = await _create_order(client, counter="C14")
    resp = await client.get("/api/v1/kds/orders?limit=5")
    assert resp.status_code == 200
    tickets = resp.json()
    assert len(tickets) > 0
    ticket_ids = {t["order_id"] for t in tickets}
    assert data["order_id"] in ticket_ids
    ticket = next(t for t in tickets if t["order_id"] == data["order_id"])
    assert ticket["status"] == "dispatched"
    assert isinstance(ticket["items"], list)
    assert ticket["items"][0]["menu_item_id"] == "espresso-001"


def test_ws_kds_push(patched_db):
    """Connecting a KDS websocket then dispatching an order pushes the ticket."""
    from fastapi.testclient import TestClient
    from cafe_os.main import app

    with TestClient(app) as tc:
        with tc.websocket_connect("/ws/kds") as ws:
            ws.send_text("ping")
            pong = ws.receive_json()
            assert pong == {"type": "pong"}

            resp = tc.post(
                "/api/v1/orders",
                json={
                    "counter_number": "C15",
                    "items": [
                        {
                            "menu_item_id": "latte-001",
                            "quantity": 2,
                            "modifiers": ["oat milk"],
                            "special_instructions": "",
                        }
                    ],
                    "payment_method": "card",
                },
            )
            assert resp.status_code == 200, resp.text
            order_id = resp.json()["order_id"]

            event = ws.receive_json()
            assert event["type"] == "kds.dispatched"
            assert event["order_id"] == order_id
            assert event["status"] == "dispatched"
            assert event["counter_number"] == "C15"
            assert len(event["items"]) == 1
            assert event["items"][0]["menu_item_id"] == "latte-001"
            assert event["items"][0]["quantity"] == 2
            assert event["items"][0]["modifiers"] == ["oat milk"]


# ---------------------------------------------------------------------------
# Menu engineering with real sales volumes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_menu_engineering_uses_real_volumes(client: AsyncClient):
    """Volumes come from actual order_items, not hardcoded defaults."""
    payload = {
        "counter_number": "C16",
        "items": [
            {
                "menu_item_id": "latte-001",
                "quantity": 3,
                "modifiers": [],
                "special_instructions": "",
            }
        ],
    }
    resp = await client.post("/api/v1/orders", json=payload)
    assert resp.status_code == 200, resp.text

    resp = await client.get("/api/v1/reports/menu-engineering")
    assert resp.status_code == 200
    data = resp.json()

    all_items = (
        data["matrix"]["stars"]
        + data["matrix"]["puzzles"]
        + data["matrix"]["plowhorses"]
        + data["matrix"]["dogs"]
    )
    assert len(all_items) == 4
    for item in all_items:
        assert "volume" in item
        assert "order_count" in item

    latte = next(i for i in all_items if i["menu_item_id"] == "latte-001")
    assert latte["volume"] >= 3
    assert latte["order_count"] >= 1

    assert "avg_volume" in data["summary"]
    assert "period_days" in data["summary"]
