import uuid
import datetime
import json
from decimal import Decimal
from typing import Dict, List, Any, Optional
from cafe_os.db import get_connection


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def create_order(counter_number: str, table_number: Optional[str] = None, customer_id: Optional[str] = None) -> Dict[str, Any]:
    order_id = f"ord-{_now_iso().replace(':','').replace('.','').replace('+','')}-{uuid.uuid4().hex[:8]}"
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO orders (id, counter_number, table_number, customer_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (order_id, counter_number, table_number, customer_id, _now_iso()),
        )
        await conn.commit()
    return {"order_id": order_id}


async def add_order_item(order_id: str, menu_item_id: str, quantity: int, unit_price: Decimal, modifiers: List[str] = None, special_instructions: str = "") -> Dict[str, Any]:
    item_id = f"item-{uuid.uuid4().hex[:8]}"
    modifiers = modifiers if modifiers is not None else []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO order_items (id, order_id, menu_item_id, quantity, unit_price, modifiers_json, special_instructions) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, order_id, menu_item_id, quantity, float(unit_price), json.dumps(modifiers), special_instructions),
        )
        await conn.commit()
    return {"item_id": item_id}


async def lookup_modifiers(keywords: List[str]) -> Dict[str, Any]:
    if not keywords:
        return {"matched": [], "unmatched": []}
    matched = []
    unmatched = []
    async with get_connection() as conn:
        for keyword in keywords:
            cursor = await conn.execute(
                "SELECT id, keyword, ingredient_id, unit_qty, unit, price_delta FROM modifiers WHERE lower(keyword) = lower(?)",
                (keyword,),
            )
            row = await cursor.fetchone()
            if row:
                matched.append({
                    "id": row["id"],
                    "keyword": row["keyword"],
                    "ingredient_id": row["ingredient_id"],
                    "unit_qty": Decimal(str(row["unit_qty"])),
                    "unit": row["unit"],
                    "price_delta": Decimal(str(row["price_delta"])),
                })
            else:
                unmatched.append(keyword)
    return {"matched": matched, "unmatched": unmatched}


async def calculate_totals(subtotal: Decimal, tax_rate: Decimal = Decimal("0.10"), discount: Decimal = Decimal("0")) -> Dict[str, Any]:
    tax = (subtotal * tax_rate).quantize(Decimal("0.01"))
    total = (subtotal + tax - discount).quantize(Decimal("0.01"))
    return {
        "subtotal": float(subtotal),
        "tax": float(tax),
        "total": float(total),
    }


async def dispatch_kds(order_id: str) -> Dict[str, Any]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, menu_item_id, quantity, modifiers_json, special_instructions FROM order_items WHERE order_id = ?",
            (order_id,),
        )
        order_items = await cursor.fetchall()
    items_json = []
    for item in order_items:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT name FROM menu_items WHERE id = ?",
                (item["menu_item_id"],),
            )
            menu_row = await cursor.fetchone()
        if not menu_row:
            raise ValueError(f"Menu item not found: {item['menu_item_id']}")
        items_json.append({
            "menu_item_id": item["menu_item_id"],
            "name": menu_row["name"],
            "quantity": item["quantity"],
            "modifiers": json.loads(item["modifiers_json"] or "[]"),
            "special_instructions": item["special_instructions"],
        })
    kds_id = f"kds-{uuid.uuid4().hex[:8]}"
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO kds_orders (id, order_id, items_json, dispatched_at) VALUES (?, ?, ?, ?)",
            (kds_id, order_id, json.dumps(items_json), _now_iso()),
        )
        await conn.commit()
    return {"kds_id": kds_id, "status": "dispatched"}


async def deduct_inventory(order_id: str) -> Dict[str, Any]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, menu_item_id, quantity, modifiers_json FROM order_items WHERE order_id = ?",
            (order_id,),
        )
        order_items = await cursor.fetchall()
    if not order_items:
        raise ValueError(f"No order items found for order_id: {order_id}")
    ingredient_deltas: Dict[str, Decimal] = {}
    for item in order_items:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT ingredient_id, unit_qty FROM recipe_bom WHERE menu_item_id = ?",
                (item["menu_item_id"],),
            )
            bom_rows = await cursor.fetchall()
        for bom in bom_rows:
            ing_id = bom["ingredient_id"]
            ingredient_deltas[ing_id] = ingredient_deltas.get(ing_id, Decimal("0")) + Decimal(str(bom["unit_qty"])) * Decimal(str(item["quantity"]))
        modifiers_list = json.loads(item["modifiers_json"] or "[]")
        if modifiers_list:
            modifier_results = await lookup_modifiers(modifiers_list)
            for modifier in modifier_results["matched"]:
                ing_id = modifier["ingredient_id"]
                ingredient_deltas[ing_id] = ingredient_deltas.get(ing_id, Decimal("0")) + modifier["unit_qty"] * Decimal(str(item["quantity"]))
    alerts = []
    for ing_id, delta in ingredient_deltas.items():
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT current_stock, reorder_threshold FROM inventory WHERE id = ?",
                (ing_id,),
            )
            inv_row = await cursor.fetchone()
        if not inv_row:
            raise ValueError(f"Inventory item not found: {ing_id}")
        new_stock = Decimal(str(inv_row["current_stock"])) - delta
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE inventory SET current_stock = ? WHERE id = ?",
                (float(new_stock), ing_id),
            )
            await conn.commit()
        if new_stock < Decimal(str(inv_row["reorder_threshold"])):
            alerts.append(f"Low stock: {ing_id}")
    return {
        "deltas": {k: float(v) for k, v in ingredient_deltas.items()},
        "alerts": alerts,
    }


async def get_menu_item(menu_item_id: str) -> Dict[str, Any]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, category, selling_price, packaging_cost, active FROM menu_items WHERE id = ?",
            (menu_item_id,),
        )
        menu_row = await cursor.fetchone()
    if not menu_row:
        raise ValueError(f"Menu item not found: {menu_item_id}")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, menu_item_id, ingredient_id, unit_qty, unit FROM recipe_bom WHERE menu_item_id = ?",
            (menu_item_id,),
        )
        bom_rows = await cursor.fetchall()
    return {
        "menu_item": dict(menu_row),
        "bom": [dict(bom) for bom in bom_rows],
    }
