"""
Tests for production-readiness features: health probes, KDS lifecycle
transitions, Redis/Postgres configuration plumbing, and SQL translation.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_order(client: AsyncClient, counter: str = "P1") -> dict:
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
    response = await client.post("/api/v1/orders", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def _latest_kds_ticket(client: AsyncClient) -> dict:
    resp = await client.get("/api/v1/kds/orders?limit=1")
    assert resp.status_code == 200
    tickets = resp.json()
    assert len(tickets) > 0
    return tickets[0]


# ---------------------------------------------------------------------------
# Health probes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthz(client: AsyncClient):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_readyz_sqlite_backend(client: AsyncClient):
    resp = await client.get("/readyz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["backend"] == "sqlite"
    assert data["database"] == "ok"
    assert data["redis"] == "disabled"


# ---------------------------------------------------------------------------
# KDS lifecycle transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kds_status_transition_flow(client: AsyncClient):
    await _create_order(client, counter="P2")
    ticket = await _latest_kds_ticket(client)
    kds_id = ticket["kds_id"]

    for status in ("preparing", "ready", "served"):
        resp = await client.patch(
            f"/api/v1/kds/{kds_id}",
            json={"status": status},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"kds_id": kds_id, "order_id": ticket["order_id"], "status": status}

        # Persisted — filterable via the REST fallback
        resp = await client.get(f"/api/v1/kds/orders?status={status}&limit=50")
        assert resp.status_code == 200
        matching = [t for t in resp.json() if t["kds_id"] == kds_id]
        assert len(matching) == 1


@pytest.mark.asyncio
async def test_kds_patch_unknown_ticket(client: AsyncClient):
    resp = await client.patch(
        "/api/v1/kds/kds-nonexistent",
        json={"status": "ready"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_kds_patch_invalid_status(client: AsyncClient):
    resp = await client.patch(
        "/api/v1/kds/kds-whatever",
        json={"status": "teleported"},
    )
    assert resp.status_code == 422


def test_ws_kds_status_changed_event(patched_db):
    """PATCHing a ticket pushes a kds.status_changed event over WebSocket."""
    from fastapi.testclient import TestClient
    from cafe_os.main import app

    with TestClient(app) as tc:
        with tc.websocket_connect("/ws/kds") as ws:
            resp = tc.post(
                "/api/v1/orders",
                json={
                    "counter_number": "P3",
                    "items": [
                        {
                            "menu_item_id": "muffin-001",
                            "quantity": 1,
                            "modifiers": [],
                            "special_instructions": "",
                        }
                    ],
                },
            )
            assert resp.status_code == 200, resp.text
            order_id = resp.json()["order_id"]

            dispatched = ws.receive_json()
            assert dispatched["type"] == "kds.dispatched"

            resp = tc.patch(
                f"/api/v1/kds/{dispatched['kds_id']}",
                json={"status": "preparing"},
            )
            assert resp.status_code == 200, resp.text

            event = ws.receive_json()
            assert event["type"] == "kds.status_changed"
            assert event["kds_id"] == dispatched["kds_id"]
            assert event["order_id"] == order_id
            assert event["status"] == "preparing"


# ---------------------------------------------------------------------------
# SQLite -> PostgreSQL SQL translation (unit tests, pure functions)
# ---------------------------------------------------------------------------


def test_translate_placeholders():
    from cafe_os.db import _translate_sql

    sql = "INSERT INTO orders (id, total) VALUES (?, ?)"
    assert _translate_sql(sql) == "INSERT INTO orders (id, total) VALUES ($1, $2)"


def test_translate_skips_string_literals():
    from cafe_os.db import _translate_sql

    sql = "SELECT * FROM t WHERE keyword = 'a?b' AND id = ?"
    translated = _translate_sql(sql)
    assert "'a?b'" in translated
    assert "= $1" in translated
    assert "? AND" not in translated.replace("'a?b'", "")


def test_translate_date_call():
    from cafe_os.db import _translate_sql

    sql = "SELECT * FROM orders WHERE date(created_at) = ?"
    translated = _translate_sql(sql)
    assert "(created_at::timestamptz)::date = $1" in translated


def test_translate_datetime_now_offset():
    from cafe_os.db import _translate_sql

    sql = "WHERE o.created_at >= datetime('now', '-30 days')"
    translated = _translate_sql(sql)
    assert "(now() + interval '-30 days')" in translated


def test_translate_insert_or_ignore():
    from cafe_os.db import _translate_sql

    sql = "INSERT OR IGNORE INTO menu_items (id, name) VALUES (?, ?)"
    translated = _translate_sql(sql)
    assert "INSERT INTO menu_items" in translated
    assert translated.endswith("ON CONFLICT DO NOTHING")
    assert "$1" in translated and "$2" in translated


def test_is_postgres_default_false(patched_db):
    from cafe_os.db import is_postgres

    assert is_postgres() is False


def test_tax_rate_env_override(monkeypatch):
    """TAX_RATE env var feeds calculate_totals when no explicit rate given."""
    import cafe_os.tools as tools_module
    from decimal import Decimal

    monkeypatch.setattr(tools_module, "TAX_RATE", Decimal("0.20"))
    result = __import__("asyncio").run(
        tools_module.calculate_totals(subtotal=Decimal("100"))
    )
    assert result["tax"] == pytest.approx(20.0)
    assert result["total"] == pytest.approx(120.0)
