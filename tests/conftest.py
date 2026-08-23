"""
Shared fixtures for Cafe OS integration tests.
"""

from __future__ import annotations

import os
import tempfile

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


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
    import cafe_os.payments as payments_module
    import importlib

    importlib.reload(db_module)
    importlib.reload(tools_module)
    importlib.reload(graph_module)
    importlib.reload(payments_module)

    db_module.DB_PATH = temp_db_path

    # Initialize schema + seed data
    await db_module.init_db()
    await db_module.seed_sample_data()

    yield temp_db_path


@pytest_asyncio.fixture
async def client(patched_db):
    from cafe_os.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
