import aiosqlite
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import json

DB_PATH = "cafe_os.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS menu_items (
    id TEXT PRIMARY KEY,
    name TEXT,
    category TEXT,
    selling_price REAL,
    packaging_cost REAL DEFAULT 0,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recipe_bom (
    id TEXT PRIMARY KEY,
    menu_item_id TEXT,
    ingredient_id TEXT,
    unit_qty REAL,
    unit TEXT
);

CREATE TABLE IF NOT EXISTS inventory (
    id TEXT PRIMARY KEY,
    name TEXT,
    unit TEXT,
    current_stock REAL,
    reorder_threshold REAL DEFAULT 0,
    cost_per_unit REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS modifiers (
    id TEXT PRIMARY KEY,
    keyword TEXT,
    ingredient_id TEXT,
    unit_qty REAL,
    unit TEXT,
    price_delta REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    counter_number TEXT,
    table_number TEXT,
    customer_id TEXT,
    subtotal REAL,
    tax REAL,
    discount REAL DEFAULT 0,
    total REAL,
    payment_status TEXT DEFAULT 'pending',
    payment_method TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    menu_item_id TEXT,
    quantity INTEGER,
    unit_price REAL,
    modifiers_json TEXT DEFAULT '[]',
    special_instructions TEXT
);

CREATE TABLE IF NOT EXISTS kds_orders (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    items_json TEXT,
    status TEXT DEFAULT 'pending',
    dispatched_at TEXT
);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT,
    phone TEXT,
    reward_points INTEGER DEFAULT 0,
    preferences TEXT
);

CREATE TABLE IF NOT EXISTS shifts (
    id TEXT PRIMARY KEY,
    cashier_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    expected_cash REAL DEFAULT 0,
    actual_cash REAL,
    cash_difference REAL,
    payment_method TEXT DEFAULT 'cash',
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS daily_sales (
    id TEXT PRIMARY KEY,
    date TEXT,
    gross_revenue REAL DEFAULT 0,
    net_revenue REAL DEFAULT 0,
    tax_collected REAL DEFAULT 0,
    total_orders INTEGER DEFAULT 0,
    avg_basket_size REAL DEFAULT 0,
    cash_revenue REAL DEFAULT 0,
    card_revenue REAL DEFAULT 0,
    mobile_revenue REAL DEFAULT 0,
    top_item_id TEXT,
    top_item_quantity INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS waste_logs (
    id TEXT PRIMARY KEY,
    ingredient_id TEXT,
    quantity REAL,
    unit TEXT,
    reason TEXT,
    recorded_by TEXT,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY,
    name TEXT,
    location TEXT,
    manager_id TEXT,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS historical_sales (
    id TEXT PRIMARY KEY,
    branch_id TEXT,
    date TEXT,
    gross_revenue REAL,
    net_revenue REAL,
    tax_collected REAL,
    total_orders INTEGER,
    avg_basket_size REAL,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items (order_id);
CREATE INDEX IF NOT EXISTS idx_kds_orders_order_id ON kds_orders (order_id);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders (created_at);
CREATE INDEX IF NOT EXISTS idx_orders_payment_method ON orders (payment_method);
CREATE INDEX IF NOT EXISTS idx_waste_logs_ingredient_id ON waste_logs (ingredient_id);
CREATE INDEX IF NOT EXISTS idx_historical_sales_branch_date ON historical_sales (branch_id, date);
"""


async def init_db() -> None:
    conn = await aiosqlite.connect(DB_PATH)
    try:
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
    finally:
        await conn.close()


@asynccontextmanager
async def get_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


async def seed_sample_data() -> None:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) FROM menu_items")
        row = await cursor.fetchone()
        if row and row[0] > 0:
            return

        await conn.executemany(
            "INSERT OR IGNORE INTO menu_items (id, name, category, selling_price, packaging_cost) VALUES (?, ?, ?, ?, ?)",
            [
                ('latte-001', 'Latte', 'coffee', 4.50, 0.10),
                ('cappuccino-001', 'Cappuccino', 'coffee', 4.00, 0.10),
                ('espresso-001', 'Espresso', 'coffee', 3.00, 0.05),
                ('muffin-001', 'Blueberry Muffin', 'food', 3.50, 0.15),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO inventory (id, name, unit, current_stock, reorder_threshold, cost_per_unit) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ('ing-001', 'Whole Milk', 'ml', 10000.0, 2000.0, 0.002),
                ('ing-002', 'Oat Milk', 'ml', 5000.0, 1000.0, 0.004),
                ('ing-003', 'Espresso Shot', 'shot', 500.0, 100.0, 0.30),
                ('ing-004', 'Syrup', 'ml', 3000.0, 500.0, 0.005),
                ('ing-005', 'Muffin Base', 'pcs', 100.0, 20.0, 0.80),
                ('ing-006', 'Whip Cream', 'ml', 2000.0, 400.0, 0.003),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO recipe_bom (id, menu_item_id, ingredient_id, unit_qty, unit) VALUES (?, ?, ?, ?, ?)",
            [
                ('bom-001', 'latte-001', 'ing-001', 250.0, 'ml'),
                ('bom-002', 'latte-001', 'ing-003', 1.0, 'shot'),
                ('bom-003', 'cappuccino-001', 'ing-001', 200.0, 'ml'),
                ('bom-004', 'cappuccino-001', 'ing-003', 1.0, 'shot'),
                ('bom-005', 'espresso-001', 'ing-003', 1.0, 'shot'),
                ('bom-006', 'muffin-001', 'ing-005', 1.0, 'pcs'),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO modifiers (id, keyword, ingredient_id, unit_qty, unit, price_delta) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ('mod-001', 'oat milk', 'ing-002', 250.0, 'ml', 0.50),
                ('mod-002', 'extra shot', 'ing-003', 1.0, 'shot', 0.75),
                ('mod-003', 'no whip', 'ing-006', -30.0, 'ml', 0.0),
                ('mod-004', '50% sugar', 'ing-004', 15.0, 'ml', 0.0),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO customers (id, name, phone, reward_points, preferences) VALUES (?, ?, ?, ?, ?)",
            [
                ('cust-001', 'Alice Johnson', '+15551234567', 150, 'Prefers oat milk, extra shot'),
                ('cust-002', 'Bob Smith', '+15559876543', 50, 'No sugar'),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO shifts (id, cashier_id, started_at, status) VALUES (?, ?, ?, ?)",
            [
                ('shift-001', 'cashier-001', '2026-08-04T08:00:00Z', 'open'),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO daily_sales (id, date, gross_revenue, net_revenue, tax_collected, total_orders, avg_basket_size, cash_revenue, card_revenue, mobile_revenue, top_item_id, top_item_quantity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ('sales-001', '2026-08-04', 450.00, 405.00, 45.00, 50, 9.00, 200.00, 200.00, 50.00, 'latte-001', 20, '2026-08-04T23:59:59Z'),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO branches (id, name, location, manager_id, status) VALUES (?, ?, ?, ?, ?)",
            [
                ('branch-001', 'Downtown Cafe', '123 Main St', 'mgr-001', 'active'),
                ('branch-002', 'Airport Cafe', '456 Airport Rd', 'mgr-002', 'active'),
            ],
        )

        await conn.executemany(
            "INSERT OR IGNORE INTO historical_sales (id, branch_id, date, gross_revenue, net_revenue, tax_collected, total_orders, avg_basket_size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ('hist-001', 'branch-001', '2026-08-01', 500.00, 450.00, 50.00, 55, 9.09, '2026-08-01T23:59:59Z'),
                ('hist-002', 'branch-001', '2026-08-02', 520.00, 468.00, 52.00, 58, 8.97, '2026-08-02T23:59:59Z'),
                ('hist-003', 'branch-002', '2026-08-01', 400.00, 360.00, 40.00, 40, 10.00, '2026-08-01T23:59:59Z'),
                ('hist-004', 'branch-002', '2026-08-02', 420.00, 378.00, 42.00, 42, 10.00, '2026-08-02T23:59:59Z'),
            ],
        )

        await conn.commit()


@asynccontextmanager
async def db_lifespan(app) -> AsyncGenerator[None, None]:
    await init_db()
    await seed_sample_data()
    yield
