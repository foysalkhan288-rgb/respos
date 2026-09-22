"""Seed the Cafe OS SQLite database with demo data.

Usage:
    python scripts/seed_db.py [--db PATH]

Defaults to $DB_PATH or ./cafe_os.db. Seeding is idempotent: existing rows
are kept (INSERT OR IGNORE) and the script exits early when menu items are
already present.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from cafe_os import db

TABLES = [
    "menu_items",
    "recipe_bom",
    "inventory",
    "modifiers",
    "customers",
    "shifts",
    "daily_sales",
    "branches",
    "historical_sales",
]


async def _table_counts(path: str) -> dict:
    conn = await aiosqlite.connect(path)
    try:
        counts = {}
        for table in TABLES:
            cursor = await conn.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            counts[table] = row[0]
        return counts
    finally:
        await conn.close()


async def _run(path: str) -> None:
    db.DB_PATH = path
    await db.init_db()
    await db.seed_sample_data()
    counts = await _table_counts(path)
    print(f"Seeded database at {path}:")
    for table, count in counts.items():
        print(f"  {table}: {count} rows")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the Cafe OS database with demo data."
    )
    parser.add_argument(
        "--db",
        default=os.environ.get("DB_PATH", "cafe_os.db"),
        help="Path to the SQLite database file (default: $DB_PATH or ./cafe_os.db)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.db))


if __name__ == "__main__":
    main()
