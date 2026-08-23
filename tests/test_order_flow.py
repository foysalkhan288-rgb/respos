"""
Integration tests for Cafe OS Intelligence Agent — end-to-end order flow.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient

# Fixtures (temp_db_path, patched_db, client) live in tests/conftest.py


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_order_with_modifiers(client: AsyncClient):
    """End-to-end: POST order → validate schema → assert inventory deducted → assert KDS dispatched."""
    payload = {
        "counter_number": "C1",
        "table_number": None,
        "customer_id": None,
        "items": [
            {
                "menu_item_id": "latte-001",
                "quantity": 2,
                "modifiers": ["oat milk", "extra shot"],
                "special_instructions": "no whip",
            }
        ],
        "payment_method": "cash",
    }

    # Verify inventory deduction via DB (delta-based — other tests may share the DB)
    import cafe_os.db as db_module
    import aiosqlite

    async def _stock(ingredient_id: str) -> float:
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT current_stock FROM inventory WHERE id = ?", (ingredient_id,)
            )
            row = await cursor.fetchone()
        assert row is not None
        return row["current_stock"]

    before_milk = await _stock("ing-001")
    before_oat = await _stock("ing-002")
    before_shots = await _stock("ing-003")
    before_whip = await _stock("ing-006")

    response = await client.post("/api/v1/orders", json=payload)
    assert response.status_code == 200, response.text

    data = response.json()
    assert data["order_id"].startswith("ord-")
    assert data["counter_number"] == "C1"
    assert data["payment_status"] == "pending"
    assert data["kds_status"] == "dispatched"
    assert isinstance(data["subtotal"], float)
    assert isinstance(data["tax"], float)
    assert isinstance(data["total"], float)
    assert data["total"] == round(data["subtotal"] + data["tax"], 2)
    assert len(data["items"]) == 1

    # Whole milk: 250ml * 2 = 500ml deducted
    assert await _stock("ing-001") == pytest.approx(before_milk - 500.0, abs=0.01)

    # Oat milk: 250ml * 2 = 500ml deducted
    assert await _stock("ing-002") == pytest.approx(before_oat - 500.0, abs=0.01)

    # Espresso shot: 1 * 2 (base) + 1 * 2 (extra shot modifier) = 4 shots
    assert await _stock("ing-003") == pytest.approx(before_shots - 4.0, abs=0.01)

    # Whip cream: -30ml * 2 = -60ml deduction (i.e. +60ml stock back)
    assert await _stock("ing-006") == pytest.approx(before_whip + 60.0, abs=0.01)

    # Verify KDS row exists
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM kds_orders WHERE order_id = ?", (data["order_id"],)
        )
        kds_row = await cursor.fetchone()
        assert kds_row is not None
        assert kds_row["status"] == "dispatched"
        items = json.loads(kds_row["items_json"])
        assert len(items) == 1
        assert items[0]["quantity"] == 2


@pytest.mark.asyncio
async def test_get_order(client: AsyncClient):
    """GET /api/v1/orders/{id} returns correct data."""
    # First create an order
    payload = {
        "counter_number": "C2",
        "items": [
            {
                "menu_item_id": "espresso-001",
                "quantity": 1,
                "modifiers": [],
                "special_instructions": "",
            }
        ],
    }
    create_resp = await client.post("/api/v1/orders", json=payload)
    assert create_resp.status_code == 200
    order_id = create_resp.json()["order_id"]

    # Fetch it
    resp = await client.get(f"/api/v1/orders/{order_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["order_id"] == order_id
    assert data["counter_number"] == "C2"
    assert data["payment_status"] == "pending"


@pytest.mark.asyncio
async def test_get_nonexistent_order(client: AsyncClient):
    resp = await client.get("/api/v1/orders/ord-nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_menu(client: AsyncClient):
    resp = await client.get("/api/v1/menu")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 4
    names = {item["name"] for item in data}
    assert "Latte" in names


@pytest.mark.asyncio
async def test_low_stock_empty_initially(client: AsyncClient):
    resp = await client.get("/api/v1/inventory/low-stock")
    assert resp.status_code == 200
    data = resp.json()
    # All stocks are above threshold with initial seed data
    assert len(data) == 0


@pytest.mark.asyncio
async def test_customer_lookup_endpoint(client: AsyncClient):
    resp = await client.get("/api/v1/customers/cust-001")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "cust-001"
    assert data["name"] == "Alice Johnson"
    assert data["reward_points"] == 150


@pytest.mark.asyncio
async def test_customer_lookup_not_found(client: AsyncClient):
    resp = await client.get("/api/v1/customers/cust-999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_loyalty_discount_applied(client: AsyncClient):
    payload = {
        "counter_number": "C3",
        "customer_id": "cust-001",
        "items": [
            {
                "menu_item_id": "latte-001",
                "quantity": 1,
                "modifiers": [],
                "special_instructions": "",
            }
        ],
        "payment_method": "cash",
    }
    response = await client.post("/api/v1/orders", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["customer"] is not None
    assert data["customer"]["reward_points"] == 150
    assert data["loyalty_discount"] is not None
    assert data["loyalty_discount"] > 0
    expected_total = round(data["subtotal"] + data["tax"] - data["loyalty_discount"], 2)
    assert data["total"] == expected_total


@pytest.mark.asyncio
async def test_loyalty_discount_persisted(client: AsyncClient):
    """Discount and adjusted total are persisted back to the orders table."""
    import aiosqlite
    import cafe_os.db as db_module

    payload = {
        "counter_number": "C17",
        "customer_id": "cust-001",
        "items": [
            {
                "menu_item_id": "latte-001",
                "quantity": 1,
                "modifiers": [],
                "special_instructions": "",
            }
        ],
    }
    response = await client.post("/api/v1/orders", json=payload)
    assert response.status_code == 200
    data = response.json()

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT discount, total FROM orders WHERE id = ?", (data["order_id"],)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row["discount"] == pytest.approx(data["loyalty_discount"], abs=0.01)
    assert row["total"] == pytest.approx(data["total"], abs=0.01)


@pytest.mark.asyncio
async def test_get_order_returns_discount(client: AsyncClient):
    """GET /orders/{id} surfaces the stored loyalty discount."""
    payload = {
        "counter_number": "C18",
        "customer_id": "cust-001",
        "items": [
            {
                "menu_item_id": "cappuccino-001",
                "quantity": 1,
                "modifiers": [],
                "special_instructions": "",
            }
        ],
    }
    created = await client.post("/api/v1/orders", json=payload)
    assert created.status_code == 200
    order_id = created.json()["order_id"]

    resp = await client.get(f"/api/v1/orders/{order_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["loyalty_discount"] is not None
    assert data["loyalty_discount"] > 0


@pytest.mark.asyncio
async def test_order_with_raw_text_field(client: AsyncClient):
    """raw_order_text is passed through to graph state (LLM parsing requires API key)."""
    payload = {
        "counter_number": "C4",
        "items": [
            {
                "menu_item_id": "espresso-001",
                "quantity": 1,
                "modifiers": [],
                "special_instructions": "",
            }
        ],
        "raw_order_text": "quick espresso please",
        "payment_method": "card",
    }
    response = await client.post("/api/v1/orders", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["order_id"].startswith("ord-")
    assert data["payment_status"] == "pending"
    assert data["kds_status"] == "dispatched"


@pytest.mark.asyncio
async def test_reconcile_shift_success(client: AsyncClient):
    # Create a new open shift for this test
    import cafe_os.db as db_module
    import aiosqlite
    import uuid

    shift_id = f"shift-{uuid.uuid4().hex[:8]}"
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute(
            "INSERT INTO shifts (id, cashier_id, started_at, expected_cash, status) VALUES (?, ?, ?, ?, ?)",
            (shift_id, 'cashier-test', '2026-08-05T08:00:00Z', 100.00, 'open'),
        )
        await db.commit()

    resp = await client.post(
        f"/api/v1/shifts/{shift_id}/reconcile",
        json={"actual_cash": 105.00},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["shift_id"] == shift_id
    assert data["status"] == "closed"
    assert data["expected_cash"] == 100.0
    assert data["actual_cash"] == 105.00
    assert data["cash_difference"] == 5.00
    assert data["flagged_for_review"] is False


@pytest.mark.asyncio
async def test_reconcile_shift_flagged(client: AsyncClient):
    # Create a new open shift for this test
    import cafe_os.db as db_module
    import aiosqlite
    import uuid

    shift_id = f"shift-{uuid.uuid4().hex[:8]}"
    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute(
            "INSERT INTO shifts (id, cashier_id, started_at, expected_cash, status) VALUES (?, ?, ?, ?, ?)",
            (shift_id, 'cashier-test', '2026-08-05T08:00:00Z', 100.00, 'open'),
        )
        await db.commit()

    resp = await client.post(
        f"/api/v1/shifts/{shift_id}/reconcile",
        json={"actual_cash": 500.00},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["shift_id"] == shift_id
    assert data["status"] == "closed"
    assert data["cash_difference"] == 400.00
    assert data["flagged_for_review"] is True


@pytest.mark.asyncio
async def test_reconcile_shift_invalid(client: AsyncClient):
    resp = await client.post(
        "/api/v1/shifts/nonexistent-shift/reconcile",
        json={"actual_cash": 100.00},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_daily_sales_report(client: AsyncClient):
    resp = await client.get("/api/v1/reports/daily-sales/2026-08-04")
    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == "2026-08-04"
    assert data["total_orders"] == 50
    assert data["gross_revenue"] == 450.00
    assert data["top_item_id"] == "latte-001"


@pytest.mark.asyncio
async def test_daily_sales_report_empty(client: AsyncClient):
    resp = await client.get("/api/v1/reports/daily-sales/2099-01-01")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_orders"] == 0
    assert data["gross_revenue"] == 0.0


@pytest.mark.asyncio
async def test_menu_engineering(client: AsyncClient):
    resp = await client.get("/api/v1/reports/menu-engineering")
    assert resp.status_code == 200
    data = resp.json()
    assert "matrix" in data
    assert "summary" in data
    assert "stars" in data["matrix"]
    assert "puzzles" in data["matrix"]
    assert "plowhorses" in data["matrix"]
    assert "dogs" in data["matrix"]
    assert isinstance(data["summary"]["total_items"], int)
    assert data["summary"]["total_items"] == 4


@pytest.mark.asyncio
async def test_record_waste(client: AsyncClient):
    resp = await client.post(
        "/api/v1/inventory/waste",
        json={
            "ingredient_id": "ing-001",
            "quantity": 500.0,
            "unit": "ml",
            "reason": "Spoiled",
            "recorded_by": "manager-001",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ingredient_id"] == "ing-001"
    assert data["quantity"] == 500.0
    assert data["unit"] == "ml"


@pytest.mark.asyncio
async def test_waste_analytics(client: AsyncClient):
    resp = await client.get("/api/v1/reports/waste")
    assert resp.status_code == 200
    data = resp.json()
    assert "waste_by_ingredient" in data
    assert "variance" in data
    assert "high_variance_items" in data


@pytest.mark.asyncio
async def test_list_branches(client: AsyncClient):
    resp = await client.get("/api/v1/branches")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    names = {b["name"] for b in data}
    assert "Downtown Cafe" in names


@pytest.mark.asyncio
async def test_branch_sales(client: AsyncClient):
    resp = await client.get("/api/v1/branches/branch-001/sales?days=2")
    assert resp.status_code == 200
    data = resp.json()
    assert data["branch_id"] == "branch-001"
    assert len(data["sales"]) == 2


@pytest.mark.asyncio
async def test_branch_comparison(client: AsyncClient):
    resp = await client.get("/api/v1/branches/compare")
    assert resp.status_code == 200
    data = resp.json()
    assert "comparison" in data
    assert len(data["comparison"]) == 2


@pytest.mark.asyncio
async def test_forecast(client: AsyncClient):
    resp = await client.get("/api/v1/reports/forecast?days=3")
    assert resp.status_code == 200
    data = resp.json()
    assert "forecast" in data
    assert len(data["forecast"]) == 3
    assert data["method"] == "rolling_avg_30d"
    assert data["avg_daily_revenue"] > 0
