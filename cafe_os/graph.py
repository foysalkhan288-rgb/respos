"""
LangGraph graph definition for Cafe OS Intelligence Agent.

Sequential 5-node flow:
  parse_order → validate_modifiers → calculate_totals → dispatch_kds → deduct_inventory

State persistence via MemorySaver (SQLiteSaver planned for production).
Optional LLM-backed natural language parsing via OpenRouter.
"""

from __future__ import annotations

import os
import uuid
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
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
# Configuration
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class OrderState(dict):
    """TypedDict-like state container for the order graph."""

    order_id: Optional[str]
    counter_number: Optional[str]
    table_number: Optional[str]
    customer_id: Optional[str]
    raw_order_text: Optional[str]
    items: Optional[List[Dict[str, Any]]]
    modifiers: Optional[List[Dict[str, Any]]]
    subtotal: Optional[float]
    tax: Optional[float]
    total: Optional[float]
    payment_status: Optional[str]
    kds_payload: Optional[Dict[str, Any]]
    inventory_deltas: Optional[Dict[str, float]]
    low_stock_alerts: Optional[List[str]]
    customer: Optional[Dict[str, Any]]
    loyalty_discount: Optional[float]
    loyalty_points_threshold: Optional[int]
    error: Optional[str]


# ---------------------------------------------------------------------------
# LLM setup (optional)
# ---------------------------------------------------------------------------

def _get_llm() -> Optional[ChatOpenAI]:
    if not OPENROUTER_API_KEY:
        return None
    return ChatOpenAI(
        model=OPENROUTER_MODEL,
        openai_api_key=OPENROUTER_API_KEY,
        openai_api_base=OPENROUTER_BASE_URL,
        temperature=0,
    )


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
    """Compute subtotal, tax, and total including modifier price deltas and loyalty discounts."""
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

    discount = Decimal(str(state.get("loyalty_discount") or 0))
    totals = await calculate_totals(subtotal=subtotal, discount=discount)

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
# Optional LLM nodes
# ---------------------------------------------------------------------------

async def llm_parse_order_node(state: OrderState) -> OrderState:
    """Use OpenRouter LLM to parse free-text order into structured items."""
    if state.get("error"):
        return state

    raw_text = state.get("raw_order_text")
    if not raw_text:
        return state

    llm = _get_llm()
    if llm is None:
        # No LLM configured — skip parsing, rely on structured items
        return state

    menu_items = []
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, name, selling_price FROM menu_items WHERE active = 1"
        )
        rows = await cursor.fetchall()
        menu_items = [dict(row) for row in rows]

    menu_context = "\n".join(
        f"- {m['id']}: {m['name']} (${m['selling_price']:.2f})" for m in menu_items
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a cafe order parser. Parse the customer's natural language order into structured JSON.
Return ONLY a JSON object with this exact schema:
{{"items": [{{"menu_item_id": "str", "quantity": int, "modifiers": ["str"], "special_instructions": "str"}}]}}

Available menu items:
{menu_context}

Rules:
- Match menu_item_id exactly from the list above
- quantity defaults to 1 if not specified
- Extract modifier keywords from the order text (e.g. "oat milk", "extra shot", "no whip")
- Put any additional free-text instructions in special_instructions
- If the order is ambiguous, make your best guess and set special_instructions to note the ambiguity"""),
        ("user", "{order_text}"),
    ])

    try:
        chain = prompt | llm
        response = await chain.ainvoke({"order_text": raw_text, "menu_context": menu_context})
        content = response.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        parsed = json.loads(content)
        state["items"] = parsed.get("items", [])
    except Exception as exc:  # pragma: no cover
        state["error"] = f"llm_parse_failed: {exc}"

    return state


async def customer_lookup_node(state: OrderState) -> OrderState:
    """Look up customer by loyalty ID or phone number for personalization."""
    if state.get("error"):
        return state

    customer_id = state.get("customer_id")
    if not customer_id:
        state["customer"] = None
        state["loyalty_discount"] = 0.0
        return state

    try:
        async with get_connection() as db:
            cursor = await db.execute(
                "SELECT id, name, phone, reward_points, preferences FROM customers WHERE id = ?",
                (customer_id,),
            )
            row = await cursor.fetchone()

        if row:
            state["customer"] = dict(row)
            points = row["reward_points"] or 0
            if points >= 100:
                state["loyalty_discount"] = None  # Will be calculated after subtotal
                state["loyalty_points_threshold"] = points
            else:
                state["loyalty_discount"] = 0.0
        else:
            state["customer"] = None
            state["loyalty_discount"] = 0.0
    except Exception:
        # customers table may not exist in MVP — graceful fallback
        state["customer"] = None
        state["loyalty_discount"] = 0.0

    return state


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

# Module-level singletons to avoid recompiling on every call
_graph = None
_graph_use_llm = None
_memory_checkpointer = MemorySaver()
_active_checkpointer: Any = None


def set_checkpointer(saver: Any = None) -> None:
    """Install the checkpointer used to persist order state.

    Called by the app lifespan with an AsyncSqliteSaver so order state survives
    restarts. Passing None restores the default in-memory checkpointer. Resets
    the cached compiled graph so the next call picks up the change.
    """
    global _active_checkpointer, _graph
    _active_checkpointer = saver
    _graph = None


def _get_checkpointer() -> Any:
    return _active_checkpointer if _active_checkpointer is not None else _memory_checkpointer


def build_graph(use_llm: bool = False) -> StateGraph:
    """Build and compile the LangGraph StateGraph with optional LLM nodes."""
    graph = StateGraph(OrderState)

    # Core nodes
    graph.add_node("parse_order", parse_order_node)
    graph.add_node("validate_modifiers", validate_modifiers_node)
    graph.add_node("calculate_totals", calculate_totals_node)
    graph.add_node("dispatch_kds", dispatch_kds_node)
    graph.add_node("deduct_inventory", deduct_inventory_node)
    graph.add_node("customer_lookup", customer_lookup_node)

    # Optional LLM parsing node
    if use_llm:
        graph.add_node("llm_parse_order", llm_parse_order_node)

    if use_llm:
        graph.set_entry_point("llm_parse_order")
        graph.add_edge("llm_parse_order", "customer_lookup")
    else:
        graph.set_entry_point("customer_lookup")

    graph.add_edge("customer_lookup", "parse_order")
    graph.add_edge("parse_order", "validate_modifiers")
    graph.add_edge("validate_modifiers", "calculate_totals")
    graph.add_edge("calculate_totals", "dispatch_kds")
    graph.add_edge("dispatch_kds", "deduct_inventory")
    graph.add_edge("deduct_inventory", END)

    return graph.compile(checkpointer=_get_checkpointer())


def get_graph(use_llm: bool = False) -> StateGraph:
    """Return a cached compiled graph instance."""
    global _graph, _graph_use_llm
    if _graph is None or _graph_use_llm != use_llm:
        _graph = build_graph(use_llm=use_llm)
        _graph_use_llm = use_llm
    return _graph


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------

async def run_order(order_payload: Dict[str, Any], use_llm: bool = False) -> Dict[str, Any]:
    """Invoke the graph with an order payload and return the final state."""
    app = get_graph(use_llm=use_llm)
    initial_state: OrderState = {
        "order_id": None,
        "counter_number": order_payload.get("counter_number"),
        "table_number": order_payload.get("table_number"),
        "customer_id": order_payload.get("customer_id"),
        "raw_order_text": order_payload.get("raw_order_text"),
        "items": order_payload.get("items", []),
        "modifiers": None,
        "subtotal": None,
        "tax": None,
        "total": None,
        "payment_status": None,
        "kds_payload": None,
        "inventory_deltas": None,
        "low_stock_alerts": None,
        "customer": None,
        "loyalty_discount": None,
        "error": None,
    }

    config = {"configurable": {"thread_id": initial_state.get("order_id") or uuid.uuid4().hex}}
    final_state = await app.ainvoke(initial_state, config=config)

    # Calculate loyalty discount if points threshold met
    loyalty_discount = Decimal("0")
    if final_state.get("customer") and final_state.get("loyalty_points_threshold"):
        points = final_state["loyalty_points_threshold"]
        subtotal = Decimal(str(final_state.get("subtotal") or 0))
        if points >= 100:
            loyalty_discount = (subtotal * Decimal("0.10")).quantize(Decimal("0.01"))
            final_state["loyalty_discount"] = float(loyalty_discount)
            final_state["total"] = float(
                (Decimal(str(final_state.get("total") or 0)) - loyalty_discount).quantize(Decimal("0.01"))
            )

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
        "customer": final_state.get("customer"),
        "loyalty_discount": final_state.get("loyalty_discount"),
        "error": final_state.get("error"),
    }
    return response
