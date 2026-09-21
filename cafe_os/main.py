"""
FastAPI application for Cafe OS Intelligence Agent POS endpoints.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field

import cafe_os.db as db
from cafe_os import kds_bus
from cafe_os.db import db_lifespan, get_connection
from cafe_os.graph import run_order, set_checkpointer
from cafe_os.tools import (
    delete_order,
    get_branch_comparison,
    get_branches,
    get_branch_sales,
    get_daily_sales_report,
    get_forecast,
    get_kds_orders,
    get_menu_engineering,
    get_waste_analytics,
    record_waste,
    reconcile_shift,
    update_kds_status,
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with db_lifespan(app):
        # Persist LangGraph order state to SQLite so it survives restarts.
        async with AsyncSqliteSaver.from_conn_string(db.DB_PATH) as checkpointer:
            await checkpointer.setup()
            set_checkpointer(checkpointer)
            app.state.checkpointer = checkpointer
            try:
                yield
            finally:
                set_checkpointer(None)


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


class ReconcileShiftIn(BaseModel):
    actual_cash: float = Field(..., ge=0)


class ReconcileShiftOut(BaseModel):
    shift_id: str
    expected_cash: float
    actual_cash: float
    cash_difference: float
    flagged_for_review: bool
    status: str


class DailySalesOut(BaseModel):
    date: str
    gross_revenue: float
    net_revenue: float
    tax_collected: float
    total_orders: int
    avg_basket_size: float
    cash_revenue: float
    card_revenue: float
    mobile_revenue: float
    top_item_id: Optional[str] = None
    top_item_quantity: int = 0


class MenuEngineeringOut(BaseModel):
    matrix: dict
    summary: dict


class WasteRecordIn(BaseModel):
    ingredient_id: str
    quantity: float = Field(..., ge=0)
    unit: str
    reason: str
    recorded_by: str


class WasteRecordOut(BaseModel):
    waste_id: str
    ingredient_id: str
    quantity: float
    unit: str


class WasteAnalyticsOut(BaseModel):
    waste_by_ingredient: dict
    theoretical_consumption: dict
    variance: dict
    high_variance_items: List[str]


class BranchOut(BaseModel):
    id: str
    name: str
    location: str
    manager_id: str
    status: str


class BranchSalesOut(BaseModel):
    branch_id: str
    days: int
    sales: List[dict]


class BranchComparisonOut(BaseModel):
    comparison: List[dict]


class ForecastOut(BaseModel):
    forecast: List[dict]
    method: str
    based_on_days: int
    avg_daily_revenue: float
    avg_daily_orders: float


class KdsOrderOut(BaseModel):
    kds_id: str
    order_id: str
    items: List[dict]
    status: str
    dispatched_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/orders", response_model=OrderOut)
async def create_order_endpoint(payload: OrderIn):
    """Accept a new order, invoke LangGraph, return structured response.

    A graph-level failure (e.g. unknown menu_item_id) cleans up the
    partially-created order row and returns 400 instead of a 500.
    """
    order_payload = payload.model_dump(by_alias=False)
    result = await run_order(order_payload)
    if result.get("error"):
        if result.get("order_id"):
            await delete_order(result["order_id"])
        raise HTTPException(status_code=400, detail=result["error"])
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


@app.post("/api/v1/shifts/{shift_id}/reconcile", response_model=ReconcileShiftOut)
async def reconcile_shift_endpoint(shift_id: str, payload: ReconcileShiftIn):
    """Cash drawer reconciliation: compare actual cash vs expected for a shift."""
    try:
        result = await reconcile_shift(shift_id, payload.actual_cash)
        return ReconcileShiftOut(**result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/daily-sales/{date}", response_model=DailySalesOut)
async def daily_sales_report_endpoint(date: str):
    """Get daily sales report for a given date (YYYY-MM-DD)."""
    try:
        result = await get_daily_sales_report(date)
        return DailySalesOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/menu-engineering", response_model=MenuEngineeringOut)
async def menu_engineering_endpoint():
    """Get menu engineering matrix: Stars, Puzzles, Plowhorses, Dogs."""
    try:
        result = await get_menu_engineering()
        return MenuEngineeringOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/v1/inventory/waste", response_model=WasteRecordOut)
async def record_waste_endpoint(payload: WasteRecordIn):
    """Record actual waste/spoilage for an ingredient."""
    try:
        result = await record_waste(
            ingredient_id=payload.ingredient_id,
            quantity=payload.quantity,
            unit=payload.unit,
            reason=payload.reason,
            recorded_by=payload.recorded_by,
        )
        return WasteRecordOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/waste", response_model=WasteAnalyticsOut)
async def waste_analytics_endpoint(date: Optional[str] = None):
    """Get waste analytics: actual vs theoretical consumption variance."""
    try:
        result = await get_waste_analytics(date=date)
        return WasteAnalyticsOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/branches", response_model=List[BranchOut])
async def list_branches():
    """List all branches."""
    try:
        result = await get_branches()
        return [BranchOut(**b) for b in result["branches"]]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/branches/{branch_id}/sales", response_model=BranchSalesOut)
async def branch_sales(branch_id: str, days: int = 7):
    """Get sales for a specific branch over the last N days."""
    try:
        result = await get_branch_sales(branch_id, days=days)
        return BranchSalesOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/branches/compare", response_model=BranchComparisonOut)
async def compare_branches():
    """Compare performance metrics across all branches."""
    try:
        result = await get_branch_comparison()
        return BranchComparisonOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/reports/forecast", response_model=ForecastOut)
async def forecast_endpoint(days: int = 7):
    """Get sales forecast for the next N days based on historical data."""
    try:
        result = await get_forecast(days=days)
        return ForecastOut(**result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/v1/kds/orders", response_model=List[KdsOrderOut])
async def list_kds_orders(status: Optional[str] = None, include_completed: bool = False):
    """List kitchen display tickets. Defaults to active (non-completed) ones."""
    try:
        result = await get_kds_orders(status=status, include_completed=include_completed)
        return [KdsOrderOut(**order) for order in result["orders"]]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.websocket("/ws/kds")
async def kds_websocket(websocket: WebSocket):
    """Kitchen display channel: live order pushes plus station status updates.

    On connect, sends {"type": "snapshot", "orders": [...]} with all active
    tickets. Every new dispatch pushes {"type": "order_dispatched", ...}.
    Clients send {"type": "status_update", "kds_id": ..., "status": ...} with
    status in acknowledged|in_progress|completed; updates are rebroadcast to
    all connected displays. {"type": "ping"} gets a {"type": "pong"}.
    """
    await websocket.accept()
    queue = kds_bus.subscribe()
    sender: Optional[asyncio.Task] = None
    try:
        backlog = await get_kds_orders()
        await websocket.send_json({"type": "snapshot", "orders": backlog["orders"]})

        async def _forward_events() -> None:
            while True:
                event = await queue.get()
                await websocket.send_json(event)

        sender = asyncio.create_task(_forward_events())

        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            msg_type = message.get("type")
            if msg_type == "status_update":
                try:
                    await update_kds_status(str(message.get("kds_id")), str(message.get("status")))
                except ValueError as exc:
                    await websocket.send_json({"type": "error", "detail": str(exc)})
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if sender is not None:
            sender.cancel()
        kds_bus.unsubscribe(queue)
