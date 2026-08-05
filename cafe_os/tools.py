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


async def get_all_modifier_keywords() -> List[str]:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT keyword FROM modifiers")
        rows = await cursor.fetchall()
    return [row["keyword"] for row in rows]


async def extract_modifier_keywords(text: str) -> List[str]:
    keywords = await get_all_modifier_keywords()
    text_lower = text.lower()
    return [kw for kw in keywords if kw.lower() in text_lower]


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
            "INSERT INTO kds_orders (id, order_id, items_json, status, dispatched_at) VALUES (?, ?, ?, ?, ?)",
            (kds_id, order_id, json.dumps(items_json), "dispatched", _now_iso()),
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


async def reconcile_shift(shift_id: str, actual_cash: float) -> Dict[str, Any]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, cashier_id, started_at, expected_cash, status FROM shifts WHERE id = ?",
            (shift_id,),
        )
        shift_row = await cursor.fetchone()
    if not shift_row:
        raise ValueError(f"Shift not found: {shift_id}")
    if shift_row["status"] != "open":
        raise ValueError(f"Shift {shift_id} is already closed")

    expected_cash = Decimal(str(shift_row["expected_cash"] or 0))
    actual_cash_dec = Decimal(str(actual_cash))
    cash_difference = (actual_cash_dec - expected_cash).quantize(Decimal("0.01"))

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE shifts SET actual_cash = ?, cash_difference = ?, ended_at = ?, status = 'closed' WHERE id = ?",
            (float(actual_cash_dec), float(cash_difference), _now_iso(), shift_id),
        )
        await conn.commit()

    discrepancy_threshold = Decimal("10.00")
    flagged = abs(cash_difference) > discrepancy_threshold

    return {
        "shift_id": shift_id,
        "expected_cash": float(expected_cash),
        "actual_cash": float(actual_cash_dec),
        "cash_difference": float(cash_difference),
        "flagged_for_review": flagged,
        "status": "closed",
    }


async def get_daily_sales_report(date: str) -> Dict[str, Any]:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM daily_sales WHERE date = ?",
            (date,),
        )
        row = await cursor.fetchone()

    if row:
        return dict(row)

    cursor = await conn.execute(
        "SELECT SUM(total) as gross, SUM(subtotal) as net, SUM(tax) as tax, COUNT(*) as orders, AVG(total) as avg_basket FROM orders WHERE date(created_at) = ?",
        (date,),
    )
    agg = await cursor.fetchone()

    cursor = await conn.execute(
        "SELECT SUM(total) as cash FROM orders WHERE date(created_at) = ? AND payment_method = 'cash'",
        (date,),
    )
    cash = await cursor.fetchone()

    cursor = await conn.execute(
        "SELECT SUM(total) as card FROM orders WHERE date(created_at) = ? AND payment_method = 'card'",
        (date,),
    )
    card = await cursor.fetchone()

    cursor = await conn.execute(
        "SELECT SUM(total) as mobile FROM orders WHERE date(created_at) = ? AND payment_method = 'mobile'",
        (date,),
    )
    mobile = await cursor.fetchone()

    cursor = await conn.execute(
        "SELECT oi.menu_item_id, SUM(oi.quantity) as qty FROM order_items oi JOIN orders o ON oi.order_id = o.id WHERE date(o.created_at) = ? GROUP BY oi.menu_item_id ORDER BY qty DESC LIMIT 1",
        (date,),
    )
    top = await cursor.fetchone()

    gross = float(agg["gross"] or 0)
    net = float(agg["net"] or 0)
    tax = float(agg["tax"] or 0)
    orders = int(agg["orders"] or 0)
    avg_basket = float(agg["avg_basket"] or 0)

    report = {
        "date": date,
        "gross_revenue": round(gross, 2),
        "net_revenue": round(net, 2),
        "tax_collected": round(tax, 2),
        "total_orders": orders,
        "avg_basket_size": round(avg_basket, 2),
        "cash_revenue": round(float(cash["cash"] or 0), 2),
        "card_revenue": round(float(card["card"] or 0), 2),
        "mobile_revenue": round(float(mobile["mobile"] or 0), 2),
        "top_item_id": top["menu_item_id"] if top else None,
        "top_item_quantity": int(top["qty"] or 0),
    }

    report_id = f"sales-{uuid.uuid4().hex[:8]}"
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO daily_sales (id, date, gross_revenue, net_revenue, tax_collected, total_orders, avg_basket_size, cash_revenue, card_revenue, mobile_revenue, top_item_id, top_item_quantity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                date,
                report["gross_revenue"],
                report["net_revenue"],
                report["tax_collected"],
                report["total_orders"],
                report["avg_basket_size"],
                report["cash_revenue"],
                report["card_revenue"],
                report["mobile_revenue"],
                report["top_item_id"],
                report["top_item_quantity"],
                _now_iso(),
            ),
        )
        await conn.commit()

    return report


async def get_menu_engineering() -> Dict[str, Any]:
    menu_items = []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, name, category, selling_price, packaging_cost FROM menu_items WHERE active = 1"
        )
        menu_rows = await cursor.fetchall()
        for row in menu_rows:
            item = dict(row)
            cursor2 = await conn.execute(
                "SELECT ingredient_id, unit_qty FROM recipe_bom WHERE menu_item_id = ?",
                (item["id"],),
            )
            bom_rows = await cursor2.fetchall()
            ingredient_cost = Decimal("0")
            for bom in bom_rows:
                cursor3 = await conn.execute(
                    "SELECT cost_per_unit FROM inventory WHERE id = ?",
                    (bom["ingredient_id"],),
                )
                inv_row = await cursor3.fetchone()
                if inv_row:
                    ingredient_cost += Decimal(str(bom["unit_qty"])) * Decimal(str(inv_row["cost_per_unit"]))
            packaging = Decimal(str(item["packaging_cost"]))
            total_cost = (ingredient_cost + packaging).quantize(Decimal("0.01"))
            selling = Decimal(str(item["selling_price"]))
            profit_margin = (selling - total_cost).quantize(Decimal("0.01"))
            menu_items.append({
                "menu_item_id": item["id"],
                "name": item["name"],
                "category": item["category"],
                "selling_price": float(selling),
                "ingredient_cost": float(ingredient_cost),
                "packaging_cost": float(packaging),
                "total_cost": float(total_cost),
                "profit_margin": float(profit_margin),
                "margin_percent": round((float(profit_margin) / float(selling)) * 100, 1) if selling > 0 else 0.0,
            })

    stars = []
    puzzles = []
    plowhorses = []
    dogs = []

    for item in menu_items:
        if item["profit_margin"] >= 2.0 and item.get("_volume", 50) >= 30:
            stars.append(item)
        elif item["profit_margin"] >= 2.0 and item.get("_volume", 0) < 30:
            puzzles.append(item)
        elif item["profit_margin"] < 2.0 and item.get("_volume", 0) >= 30:
            plowhorses.append(item)
        else:
            dogs.append(item)

    return {
        "matrix": {
            "stars": stars,
            "puzzles": puzzles,
            "plowhorses": plowhorses,
            "dogs": dogs,
        },
        "summary": {
            "total_items": len(menu_items),
            "avg_margin_percent": round(sum(i["margin_percent"] for i in menu_items) / len(menu_items), 1) if menu_items else 0.0,
        },
    }
