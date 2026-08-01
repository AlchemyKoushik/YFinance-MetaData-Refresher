from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    status: Literal["ok"]


class RefreshResponse(BaseModel):
    status: Literal["success", "failed"]
    run_id: str | None = None
    started_at_ist: str | None = None
    finished_at_ist: str | None = None
    processed: int
    inserted: int
    updated: int
    skipped: int
    failed: int
    total_api_calls: int
    validation_warnings: int
    duration_seconds: float
    message: str


class CompanyMetadataRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticker: str
    company_name: str | None = None
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    region: str | None = None
    exchange: str | None = None
    currency: str | None = None
    revenue_ttm: int | float | None = None
    market_cap: int | float | None = None
    last_updated: datetime
