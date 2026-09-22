"""KDS endpoints (REST + WebSocket), checkpointer persistence, seed script."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient


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

    await db_module.init_db()
    await db_module.seed_sample_data()

    yield temp_db_path


@pytest_asyncio.fixture
async def client(patched_db):
    from cafe_os.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_menu_item_rolls_back_order(client: AsyncClient):
    """Bad menu_item_id → 400, and no poisoned order row is left behind."""
    resp = await client.post("/api/v1/orders", json={
        "counter_number": "BAD1",
        "items": [{"menu_item_id": "does-not-exist", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    assert resp.status_code == 400, resp.text
    assert "does-not-exist" in resp.json()["detail"]

    import cafe_os.db as db_module

    async with aiosqlite.connect(db_module.DB_PATH) as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM orders WHERE counter_number = 'BAD1'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0


@pytest.mark.asyncio
async def test_list_kds_orders_rest(client: AsyncClient):
    """POST /orders dispatches to KDS; GET /kds/orders lists the ticket."""
    resp = await client.post("/api/v1/orders", json={
        "counter_number": "KDS1",
        "items": [{"menu_item_id": "muffin-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
    })
    assert resp.status_code == 200, resp.text
    order_id = resp.json()["order_id"]

    resp = await client.get("/api/v1/kds/orders")
    assert resp.status_code == 200
    tickets = resp.json()
    ticket = next(t for t in tickets if t["order_id"] == order_id)
    assert ticket["status"] == "dispatched"
    assert ticket["items"][0]["name"] == "Blueberry Muffin"


# ---------------------------------------------------------------------------
# WebSocket (run on the TestClient's portal loop in a worker thread)
# ---------------------------------------------------------------------------


def _ws_flow():
    from cafe_os.main import app

    with TestClient(app) as client:
        with client.websocket_connect("/ws/kds") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert all(o["status"] != "completed" for o in snapshot["orders"])

            resp = client.post("/api/v1/orders", json={
                "counter_number": "WS1",
                "items": [{"menu_item_id": "espresso-001", "quantity": 1, "modifiers": [], "special_instructions": ""}],
            })
            assert resp.status_code == 200, resp.text
            order_id = resp.json()["order_id"]

            event = ws.receive_json()
            assert event["type"] == "order_dispatched"
            assert event["order_id"] == order_id
            assert event["items"][0]["menu_item_id"] == "espresso-001"
            kds_id = event["kds_id"]

            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"

            ws.send_json({"type": "status_update", "kds_id": kds_id, "status": "bogus"})
            error = ws.receive_json()
            assert error["type"] == "error"

            ws.send_json({"type": "status_update", "kds_id": kds_id, "status": "completed"})
            update = ws.receive_json()
            assert update == {"type": "status_changed", "kds_id": kds_id, "status": "completed"}

        resp = client.get("/api/v1/kds/orders")
        assert all(o["kds_id"] != kds_id for o in resp.json())
        resp = client.get("/api/v1/kds/orders", params={"include_completed": "true"})
        assert any(o["kds_id"] == kds_id and o["status"] == "completed" for o in resp.json())

    return order_id, kds_id


@pytest.mark.asyncio
async def test_kds_websocket_flow(patched_db):
    """WS /ws/kds: snapshot on connect, live dispatch push, status_update round-trip."""
    order_id, kds_id = await asyncio.to_thread(_ws_flow)

    import cafe_os.db as db_module

    async with aiosqlite.connect(db_module.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT status FROM kds_orders WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "completed"

        # The app's lifespan installs an AsyncSqliteSaver — order runs leave checkpoints.
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM checkpoints")
        row = await cursor.fetchone()
        assert row["n"] > 0


# ---------------------------------------------------------------------------
# Seed script
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_script(tmp_path):
    """scripts/seed_db.py creates a fresh DB and loads demo data."""
    db_file = tmp_path / "seeded.db"
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        os.path.join("scripts", "seed_db.py"),
        "--db",
        str(db_file),
        cwd=repo_root,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, stderr.decode()
    assert b"menu_items" in stdout

    async with aiosqlite.connect(str(db_file)) as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM menu_items")
        row = await cursor.fetchone()
        assert row[0] >= 4
        cursor = await conn.execute("SELECT COUNT(*) FROM modifiers")
        row = await cursor.fetchone()
        assert row[0] >= 4
