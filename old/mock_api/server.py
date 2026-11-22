"""Self-hosted mock business API for the competition baseline.

The organizer did not provide a sandbox API. This service defines a small,
explicit contract for the three calls required by the brief:

  * customer / lead lookup
  * order lookup
  * ticket or callback registration

It is deterministic and contains only synthetic, masked data. The real
application must never point at this service in production.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

API_KEY = os.getenv("MOCK_API_KEY", "dev-mock-key")
CUSTOMERS: dict[str, dict[str, Any]] = {
    "CUST-001": {
        "customer_id": "CUST-001",
        "merchant": "演示品牌",
        "contact_masked": "张**",
        "phone_masked": "138****0000",
        "eligible": True,
    },
    "CUST-002": {
        "customer_id": "CUST-002",
        "merchant": "演示家居",
        "contact_masked": "李**",
        "phone_masked": "139****1111",
        "eligible": False,
    },
}
ORDERS: dict[str, dict[str, Any]] = {
    "A12345678": {
        "order_id": "A12345678",
        "customer_id": "CUST-001",
        "status": "delivering",
        "promised_at": "2026-11-08T15:00:00+08:00",
        "address_masked": "北京市朝阳区***",
    },
    "A87654321": {
        "order_id": "A87654321",
        "customer_id": "CUST-002",
        "status": "created",
        "promised_at": "2026-11-09T10:00:00+08:00",
        "address_masked": "上海市浦东新区***",
    },
}
TICKETS: dict[str, dict[str, Any]] = {}
IDEMPOTENCY: dict[str, tuple[str, dict[str, Any]]] = {}


def error(code: str, message: str, status: int, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
    )


def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


class TicketCreate(BaseModel):
    request_id: str = Field(min_length=3, max_length=64)
    customer_id: str = Field(min_length=3, max_length=64)
    type: Literal["callback", "manual", "reschedule"]
    summary: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any] = Field(default_factory=dict)


app = FastAPI(title="POP Mock Business API", version="1.0.0")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return error("HTTP_ERROR", str(exc.detail), exc.status_code)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "mock-business", "version": "1.0.0"}


@app.get("/v1/customers/{customer_id}", dependencies=[Depends(require_key)])
async def customer(customer_id: str) -> dict[str, Any]:
    row = CUSTOMERS.get(customer_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": {
            "code": "CUSTOMER_NOT_FOUND", "message": "customer not found", "retryable": False}})
    return row


@app.get("/v1/orders/{order_id}", dependencies=[Depends(require_key)])
async def order(order_id: str) -> dict[str, Any]:
    row = ORDERS.get(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": {
            "code": "ORDER_NOT_FOUND", "message": "order not found", "retryable": False}})
    return row


@app.post("/v1/tickets", status_code=201, dependencies=[Depends(require_key)])
async def create_ticket(
    body: TicketCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise HTTPException(status_code=400, detail={"error": {
            "code": "IDEMPOTENCY_KEY_REQUIRED",
            "message": "Idempotency-Key header is required",
            "retryable": False,
        }})
    body_dict = body.model_dump()
    if idempotency_key in IDEMPOTENCY:
        original_body, cached = IDEMPOTENCY[idempotency_key]
        if original_body != body_dict:
            raise HTTPException(status_code=409, detail={"error": {
                "code": "IDEMPOTENCY_CONFLICT",
                "message": "same key used with different payload",
                "retryable": False,
            }})
        response.status_code = 200
        return {**cached, "idempotent_replay": True}

    ticket_id = f"T{uuid4().hex[:10].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "ticket_id": ticket_id,
        "request_id": body.request_id,
        "status": "created",
        "created_at": now,
        "idempotent_replay": False,
    }
    TICKETS[ticket_id] = {**body_dict, **result}
    IDEMPOTENCY[idempotency_key] = (body_dict, result)
    return result


@app.get("/v1/tickets/{ticket_id}", dependencies=[Depends(require_key)])
async def get_ticket(ticket_id: str) -> dict[str, Any]:
    row = TICKETS.get(ticket_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error": {
            "code": "TICKET_NOT_FOUND", "message": "ticket not found", "retryable": False}})
    return row


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8095)
