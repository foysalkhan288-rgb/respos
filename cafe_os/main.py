"""
FastAPI application for Cafe OS Intelligence Agent POS endpoints.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from cafe_os.db import db_lifespan, get_connection
from cafe_os.graph import run_order


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with db_lifespan(app):
        yield


app = FastAPI(
    title="Cafe OS Intelligence Agent",
    description="POS intelligence layer — MVP",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class OrderItemIn(BaseModel):
    menu_item_id: str = Field(..., alias="menu_item_id")
    quantity: int = Field(..., ge=1)
    modifiers: List[str] = Field(default_factory=list)
    special_instructions: str = Field(default="")


class OrderIn(BaseModel):
    counter_number: str
    table_number: Optional[str] = None
    customer_id: Optional[str] = None
    items: List[OrderItemIn]
    payment_method: Optional[str] = None
    raw_order_text: Optional[str] = None


class OrderOut(BaseModel):
    order_id: str
    counter_number: str
    items: List[dict]
    subtotal: float
    tax: float
    total: float
    payment_status: str
    kds_status: str
    inventory_alerts: List[str]
    customer: Optional[dict] = None
    loyalty_discount: Optional[float] = None
    error: Optional[str] = None


class MenuItemOut(BaseModel):
    id: str
    name: str
    category: str
    selling_price: float
    packaging_cost: float
    active: bool


class LowStockOut(BaseModel):
    id: str
    name: str
    unit: str
    current_stock: float
    reorder_threshold: float


class CustomerOut(BaseModel):
    id: str
    name: Optional[str] = None
    phone: Optional[str] = None
    reward_points: Optional[int] = None
    preferences: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/orders", response_model=OrderOut)
async def create_order_endpoint(payload: OrderIn):
    """Accept a new order, invoke LangGraph, return structured response."""
    order_payload = payload.model_dump(by_alias=False)
    result = await run_order(order_payload)
    return OrderOut(**result)


@app.get("/api/v1/orders/{order_id}", response_model=OrderOut)
async def get_order_endpoint(order_id: str):
    """Fetch order status, items, and KDS status by order_id."""
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        )
        order_row = await cursor.fetchone()

    if not order_row:
        raise HTTPException(status_code=404, detail="Order not found")

    # Fetch items
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT * FROM order_items WHERE order_id = ?", (order_id,)
        )
        item_rows = await cursor.fetchall()

    # Fetch KDS status
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT status FROM kds_orders WHERE order_id = ? ORDER BY dispatched_at DESC LIMIT 1",
            (order_id,),
        )
        kds_row = await cursor.fetchone()

    items = []
    for row in item_rows:
        items.append(
            {
                "order_item_id": row["id"],
                "menu_item_id": row["menu_item_id"],
                "quantity": row["quantity"],
                "unit_price": row["unit_price"],
                "modifiers": json.loads(row["modifiers_json"] or "[]"),
                "special_instructions": row["special_instructions"],
            }
        )

    kds_status = kds_row["status"] if kds_row else "unknown"

    return OrderOut(
        order_id=order_row["id"],
        counter_number=order_row["counter_number"],
        items=items,
        subtotal=order_row["subtotal"],
        tax=order_row["tax"],
        total=order_row["total"],
        payment_status=order_row["payment_status"],
        kds_status=kds_status,
        inventory_alerts=[],
        customer=None,
        loyalty_discount=None,
        error=None,
    )


@app.get("/api/v1/menu", response_model=List[MenuItemOut])
async def list_menu():
    """List all active menu items."""
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, name, category, selling_price, packaging_cost, active FROM menu_items WHERE active = 1"
        )
        rows = await cursor.fetchall()

    return [
        MenuItemOut(
            id=row["id"],
            name=row["name"],
            category=row["category"],
            selling_price=row["selling_price"],
            packaging_cost=row["packaging_cost"],
            active=bool(row["active"]),
        )
        for row in rows
    ]


@app.get("/api/v1/inventory/low-stock", response_model=List[LowStockOut])
async def low_stock():
    """List inventory items at or below reorder threshold."""
    async with get_connection() as db:
        cursor = await db.execute(
            "SELECT id, name, unit, current_stock, reorder_threshold FROM inventory WHERE current_stock <= reorder_threshold"
        )
        rows = await cursor.fetchall()

    return [
        LowStockOut(
            id=row["id"],
            name=row["name"],
            unit=row["unit"],
            current_stock=row["current_stock"],
            reorder_threshold=row["reorder_threshold"],
        )
        for row in rows
    ]


@app.get("/api/v1/customers/{customer_id}", response_model=Optional[CustomerOut])
async def get_customer(customer_id: str):
    """Look up customer by ID for CRM personalization (stub for MVP)."""
    try:
        async with get_connection() as db:
            cursor = await db.execute(
                "SELECT id, name, phone, reward_points, preferences FROM customers WHERE id = ?",
                (customer_id,),
            )
            row = await cursor.fetchone()

        if row:
            return CustomerOut(
                id=row["id"],
                name=row["name"],
                phone=row["phone"],
                reward_points=row["reward_points"],
                preferences=row["preferences"],
            )
        raise HTTPException(status_code=404, detail="Customer not found")
    except HTTPException:
        raise
    except Exception as exc:
        if "no such table" in str(exc).lower():
            raise HTTPException(status_code=404, detail="CRM not initialized")
        raise HTTPException(status_code=500, detail=str(exc))
