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
