"""Predictive Dialogue API v1, generated from contracts/predictive.v1.json."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import create_pipeline

SCHEMA_VERSION = "1.0"
router = APIRouter(prefix="/predictive/v1", tags=["predictive-v1"])
pipeline = create_pipeline()


class ClassifyBody(BaseModel):
    operation: Literal["classify"] = "classify"
    text: str
    stage: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


class PrepareBody(BaseModel):
    operation: Literal["prepare"] = "prepare"
    stage: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


class HandleBody(BaseModel):
    operation: Literal["handle"] = "handle"
    text: str
    stage: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)


@router.post("/classify")
def classify(body: ClassifyBody) -> dict[str, Any]:
    classification = pipeline.classifier.classify(body.text, body.stage, body.context)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "classify",
        "classification": asdict(classification),
    }


@router.post("/prepare")
def prepare(body: PrepareBody) -> dict[str, Any]:
    candidates = pipeline.prepare(body.stage, body.context)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "prepare",
        "stage": body.stage,
        "candidates": [asdict(c) for c in candidates],
    }


@router.post("/handle")
def handle(body: HandleBody) -> dict[str, Any]:
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="text must not be blank")
    decision = pipeline.handle(body.text, body.stage, body.context)
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": "handle",
        "decision": asdict(decision),
    }
