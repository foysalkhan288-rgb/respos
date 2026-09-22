"""
Integration tests for Cafe OS Intelligence Agent — end-to-end order flow.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Test DB setup — use a temp file so tests don't pollute the repo
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.remove(path)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def patched_db(temp_db_path):
    """Monkeypatch DB_PATH before any cafe_os modules are imported."""
    import cafe_os.db as db_module
    import cafe_os.tools as tools_module
    import cafe_os.graph as graph_module
    import importlib

    importlib.reload(db_module)
    importlib.reload(tools_module)
    importlib.reload(graph_module)

    db_module.DB_PATH = temp_db_path

    # Initialize schema + seed data
    await db_module.init_db()
    await db_module.seed_sample_data()

    yield temp_db_path


# ---------------------------------------------------------------------------
# App factory — must be imported AFTER patched_db runs
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(patched_db):
    from cafe_os.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


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

    # Verify inventory deduction via DB
    import cafe_os.db as db_module
    import aiosqlite

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Whole milk: 250ml * 2 = 500ml deducted
        cursor = await db.execute(
            "SELECT current_stock FROM inventory WHERE id = 'ing-001'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["current_stock"] == pytest.approx(9500.0, abs=0.01)

        # Oat milk: 250ml * 2 = 500ml deducted
        cursor = await db.execute(
            "SELECT current_stock FROM inventory WHERE id = 'ing-002'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["current_stock"] == pytest.approx(4500.0, abs=0.01)

        # Espresso shot: 1 * 2 + 1 * 2 (base + extra shot) = 4 shots
        cursor = await db.execute(
            "SELECT current_stock FROM inventory WHERE id = 'ing-003'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["current_stock"] == pytest.approx(496.0, abs=0.01)

        # Whip cream: -30ml * 2 = -60ml (i.e. +60ml back because negative deduction)
        cursor = await db.execute(
            "SELECT current_stock FROM inventory WHERE id = 'ing-006'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["current_stock"] == pytest.approx(2060.0, abs=0.01)

    # Verify KDS row exists
    import cafe_os.db as db_module
    import aiosqlite

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


# ---------------------------------------------------------------------------
# Payments — run last: cash payment mutates the seeded open shift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pay_order_cash_banks_shift_and_accrues_points(client: AsyncClient):
    """Cash payment: marks paid, accrues loyalty points, adds to shift expected_cash."""
    resp = await client.post("/api/v1/orders", json={
        "counter_number": "P1",
        "customer_id": "cust-002",
        "items": [{"menu_item_id": "espresso-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    assert resp.status_code == 200
    order_id = resp.json()["order_id"]

    import cafe_os.db as db_module
    import aiosqlite

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT expected_cash FROM shifts WHERE id = 'shift-001'")
        cash_before = (await cursor.fetchone())["expected_cash"]
        cursor = await db.execute("SELECT reward_points FROM customers WHERE id = 'cust-002'")
        points_before = (await cursor.fetchone())["reward_points"]

    resp = await client.post(f"/api/v1/orders/{order_id}/pay", json={"payment_method": "cash"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["payment_status"] == "paid"
    assert data["payment_method"] == "cash"
    assert data["shift_id"] == "shift-001"
    assert data["points_earned"] == int(data["total"])

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT expected_cash FROM shifts WHERE id = 'shift-001'")
        cash_after = (await cursor.fetchone())["expected_cash"]
        assert cash_after == pytest.approx(cash_before + data["total"], abs=0.01)
        cursor = await db.execute("SELECT reward_points FROM customers WHERE id = 'cust-002'")
        points_after = (await cursor.fetchone())["reward_points"]
        assert points_after == points_before + data["points_earned"]

    # GET reflects the paid status
    resp = await client.get(f"/api/v1/orders/{order_id}")
    assert resp.status_code == 200
    assert resp.json()["payment_status"] == "paid"


@pytest.mark.asyncio
async def test_pay_order_card_does_not_touch_shift(client: AsyncClient):
    resp = await client.post("/api/v1/orders", json={
        "counter_number": "P2",
        "items": [{"menu_item_id": "muffin-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    order_id = resp.json()["order_id"]

    import cafe_os.db as db_module
    import aiosqlite

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT expected_cash FROM shifts WHERE id = 'shift-001'")
        cash_before = (await cursor.fetchone())["expected_cash"]

    resp = await client.post(f"/api/v1/orders/{order_id}/pay", json={"payment_method": "card"})
    assert resp.status_code == 200
    assert resp.json()["shift_id"] is None

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT expected_cash FROM shifts WHERE id = 'shift-001'")
        cash_after = (await cursor.fetchone())["expected_cash"]
        assert cash_after == pytest.approx(cash_before, abs=0.01)


@pytest.mark.asyncio
async def test_pay_order_twice_fails(client: AsyncClient):
    resp = await client.post("/api/v1/orders", json={
        "counter_number": "P3",
        "items": [{"menu_item_id": "espresso-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    order_id = resp.json()["order_id"]

    resp = await client.post(f"/api/v1/orders/{order_id}/pay", json={"payment_method": "card"})
    assert resp.status_code == 200
    resp = await client.post(f"/api/v1/orders/{order_id}/pay", json={"payment_method": "card"})
    assert resp.status_code == 400
    assert "already paid" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_pay_nonexistent_order(client: AsyncClient):
    resp = await client.post("/api/v1/orders/ord-nope/pay", json={"payment_method": "cash"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_pay_invalid_method(client: AsyncClient):
    resp = await client.post("/api/v1/orders", json={
        "counter_number": "P4",
        "items": [{"menu_item_id": "espresso-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    order_id = resp.json()["order_id"]

    resp = await client.post(f"/api/v1/orders/{order_id}/pay", json={"payment_method": "crypto"})
    assert resp.status_code == 400
    assert "Invalid payment method" in resp.json()["detail"]

    # Order stays pending and payable afterwards
    resp = await client.post(f"/api/v1/orders/{order_id}/pay", json={"payment_method": "mobile"})
    assert resp.status_code == 200
    assert resp.json()["payment_method"] == "mobile"


@pytest.mark.asyncio
async def test_open_shift_and_list(client: AsyncClient):
    resp = await client.post("/api/v1/shifts", json={"cashier_id": "cashier-9", "opening_cash": 100})
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "open"
    assert body["cashier_id"] == "cashier-9"
    assert body["expected_cash"] == pytest.approx(100)
    assert body["ended_at"] is None
    shift_id = body["id"]

    resp = await client.get("/api/v1/shifts", params={"status": "open"})
    assert resp.status_code == 200
    open_ids = [s["id"] for s in resp.json()]
    assert shift_id in open_ids
    assert "shift-001" in open_ids  # seeded shift still open

    resp = await client.get("/api/v1/shifts", params={"status": "closed"})
    assert resp.status_code == 200
    assert shift_id not in [s["id"] for s in resp.json()]


@pytest.mark.asyncio
async def test_open_shift_default_opening_cash(client: AsyncClient):
    resp = await client.post("/api/v1/shifts", json={"cashier_id": "cashier-10"})
    assert resp.status_code == 201
    assert resp.json()["expected_cash"] == 0


@pytest.mark.asyncio
async def test_cash_payment_banks_to_new_shift(client: AsyncClient):
    resp = await client.post("/api/v1/shifts", json={"cashier_id": "cashier-11", "opening_cash": 50})
    assert resp.status_code == 201
    shift_id = resp.json()["id"]

    resp = await client.post("/api/v1/orders", json={
        "counter_number": "S1",
        "items": [{"menu_item_id": "espresso-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    order_id = resp.json()["order_id"]

    resp = await client.post(
        f"/api/v1/orders/{order_id}/pay",
        json={"payment_method": "cash", "shift_id": shift_id},
    )
    assert resp.status_code == 200
    assert resp.json()["shift_id"] == shift_id
    total = resp.json()["total"]

    import cafe_os.db as db_module
    import aiosqlite

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT expected_cash FROM shifts WHERE id = ?", (shift_id,))
        assert (await cursor.fetchone())["expected_cash"] == pytest.approx(50 + total, abs=0.01)


@pytest.mark.asyncio
async def test_cash_payment_no_autopick_with_multiple_open_shifts(client: AsyncClient):
    # With shift-001 plus the shifts opened above all open, cash payment
    # without an explicit shift_id must not guess a drawer.
    resp = await client.post("/api/v1/orders", json={
        "counter_number": "S2",
        "items": [{"menu_item_id": "espresso-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    order_id = resp.json()["order_id"]

    resp = await client.post(f"/api/v1/orders/{order_id}/pay", json={"payment_method": "cash"})
    assert resp.status_code == 200
    assert resp.json()["shift_id"] is None
