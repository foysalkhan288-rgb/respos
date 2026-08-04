"""
LangGraph graph definition for Cafe OS Intelligence Agent.

Sequential 5-node flow:
  parse_order → validate_modifiers → calculate_totals → dispatch_kds → deduct_inventory
"""

from __future__ import annotations

import uuid
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from langgraph.graph import END, StateGraph

from cafe_os.db import get_connection
from cafe_os.tools import (
    add_order_item,
    calculate_totals,
    create_order,
    deduct_inventory,
    dispatch_kds,
    extract_modifier_keywords,
    get_menu_item,
    lookup_modifiers,
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class OrderState(dict):
    """TypedDict-like state container for the order graph."""

    order_id: Optional[str]
    counter_number: Optional[str]
    items: Optional[List[Dict[str, Any]]]
    modifiers: Optional[List[Dict[str, Any]]]
    subtotal: Optional[float]
    tax: Optional[float]
    total: Optional[float]
    payment_status: Optional[str]
    kds_payload: Optional[Dict[str, Any]]
    inventory_deltas: Optional[Dict[str, float]]
    low_stock_alerts: Optional[List[str]]
    error: Optional[str]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def parse_order_node(state: OrderState) -> OrderState:
    """Create the order record and attach order_id to state."""
    try:
        order_id = await create_order(
            counter_number=state.get("counter_number", "UNKNOWN"),
            table_number=state.get("table_number"),
            customer_id=state.get("customer_id"),
        )
        state["order_id"] = order_id["order_id"]
        state["payment_status"] = "pending"
        state["error"] = None
    except Exception as exc:  # pragma: no cover
        state["error"] = f"parse_order_failed: {exc}"
    return state


async def validate_modifiers_node(state: OrderState) -> OrderState:
    """Validate and persist modifiers for each order item."""
    if state.get("error"):
        return state

    order_id = state["order_id"]
    raw_items = state.get("items", [])
    state["items"] = []
    state["modifiers"] = []

    for raw in raw_items:
        menu_item_id = raw.get("menu_item_id")
        quantity = raw.get("quantity", 1)
        modifiers = raw.get("modifiers", []) or []
        special_instructions = raw.get("special_instructions", "") or ""

        # Extract additional modifier keywords from free-text special instructions
        extracted = await extract_modifier_keywords(special_instructions)
        merged_modifiers = list(dict.fromkeys(modifiers + extracted))

        # Resolve menu item for unit price
        menu = await get_menu_item(menu_item_id)
        unit_price = Decimal(str(menu["menu_item"]["selling_price"]))

        # Add order item (merged modifiers stored as JSON string of keywords)
        await add_order_item(
            order_id=order_id,
            menu_item_id=menu_item_id,
            quantity=quantity,
            unit_price=unit_price,
            modifiers=merged_modifiers,
            special_instructions=special_instructions,
        )

        # Track item details for response (show original modifiers, not extracted)
        state["items"].append({
            "menu_item_id": menu_item_id,
            "quantity": quantity,
            "unit_price": float(unit_price),
            "modifiers": modifiers,
            "special_instructions": special_instructions,
        })

        # Lookup merged modifiers
        lookup = await lookup_modifiers(merged_modifiers)
        state["modifiers"].append(lookup)

    return state


async def calculate_totals_node(state: OrderState) -> OrderState:
    """Compute subtotal, tax, and total including modifier price deltas."""
    if state.get("error"):
        return state

    order_id = state["order_id"]
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, quantity, unit_price, modifiers_json FROM order_items WHERE order_id = ?",
            (order_id,),
        )
        rows = await cursor.fetchall()

    subtotal = Decimal("0")
    for row in rows:
        item_total = Decimal(str(row["quantity"])) * Decimal(str(row["unit_price"]))
        modifiers = json.loads(row["modifiers_json"] or "[]")
        if modifiers:
            lookup = await lookup_modifiers(modifiers)
            for mod in lookup.get("matched", []):
                item_total += mod["price_delta"] * Decimal(str(row["quantity"]))
        subtotal += item_total

    totals = await calculate_totals(subtotal=subtotal)

    state["subtotal"] = totals["subtotal"]
    state["tax"] = totals["tax"]
    state["total"] = totals["total"]

    # Persist totals back to orders table
    async with get_connection() as db:
        await db.execute(
            "UPDATE orders SET subtotal = ?, tax = ?, total = ? WHERE id = ?",
            (state["subtotal"], state["tax"], state["total"], order_id),
        )
        await db.commit()

    return state


async def dispatch_kds_node(state: OrderState) -> OrderState:
    """Dispatch order to Kitchen Display System."""
    if state.get("error"):
        return state

    kds = await dispatch_kds(state["order_id"])
    state["kds_payload"] = kds
    return state


async def deduct_inventory_node(state: OrderState) -> OrderState:
    """Deduct recipe BOM units and modifier units from inventory."""
    if state.get("error"):
        return state

    result = await deduct_inventory(state["order_id"])
    state["inventory_deltas"] = result.get("deltas", {})
    state["low_stock_alerts"] = result.get("alerts", [])
    return state


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph():
    """Build and compile the LangGraph StateGraph."""
    graph = StateGraph(OrderState)

    graph.add_node("parse_order", parse_order_node)
    graph.add_node("validate_modifiers", validate_modifiers_node)
    graph.add_node("calculate_totals", calculate_totals_node)
    graph.add_node("dispatch_kds", dispatch_kds_node)
    graph.add_node("deduct_inventory", deduct_inventory_node)

    graph.set_entry_point("parse_order")
    graph.add_edge("parse_order", "validate_modifiers")
    graph.add_edge("validate_modifiers", "calculate_totals")
    graph.add_edge("calculate_totals", "dispatch_kds")
    graph.add_edge("dispatch_kds", "deduct_inventory")
    graph.add_edge("deduct_inventory", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------

async def run_order(order_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke the graph with an order payload and return the final state."""
    app = build_graph()
    initial_state: OrderState = {
        "order_id": None,
        "counter_number": order_payload.get("counter_number"),
        "table_number": order_payload.get("table_number"),
        "customer_id": order_payload.get("customer_id"),
        "items": order_payload.get("items", []),
        "modifiers": None,
        "subtotal": None,
        "tax": None,
        "total": None,
        "payment_status": None,
        "kds_payload": None,
        "inventory_deltas": None,
        "low_stock_alerts": None,
        "error": None,
    }

    final_state = await app.ainvoke(initial_state)

    # Build strict POS response schema
    response = {
        "order_id": final_state.get("order_id"),
        "counter_number": final_state.get("counter_number"),
        "items": final_state.get("items"),
        "subtotal": final_state.get("subtotal"),
        "tax": final_state.get("tax"),
        "total": final_state.get("total"),
        "payment_status": final_state.get("payment_status", "pending"),
        "kds_status": (
            final_state.get("kds_payload", {}).get("status", "unknown")
            if final_state.get("kds_payload")
            else "unknown"
        ),
        "inventory_alerts": final_state.get("low_stock_alerts", []),
        "error": final_state.get("error"),
    }
    return response
