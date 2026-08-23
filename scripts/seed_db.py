"""Seed the Cafe OS database with sample data.

Usage:
    python scripts/seed_db.py [--db PATH]
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cafe_os.db as db_module


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Cafe OS SQLite database")
    parser.add_argument("--db", default=None, help="Path to SQLite database file")
    args = parser.parse_args()

    if args.db:
        db_module.DB_PATH = args.db

    await db_module.init_db()
    await db_module.seed_sample_data()
    print(f"Seeded database at {db_module.DB_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
