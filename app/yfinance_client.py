from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import yfinance as yf

from app.catalog import CatalogCompany


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FetchResult:
    ticker: str
    attempts: int
    catalog_company_name: str | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    company_name: str | None
    sector: str | None
    industry: str | None
    country: str | None
    region: str | None
    exchange: str | None
    currency: str | None
    revenue_ttm: int | float | Decimal | None
    market_cap: int | float | Decimal | None
    last_updated: datetime


@dataclass(slots=True)
class FetchFailure:
    ticker: str
    attempts: int
    catalog_company_name: str | None
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    error: str


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return None


def _coerce_numeric(value: Any) -> int | float | Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if hasattr(value, "item"):
        try:
            python_value = value.item()
        except Exception:  # pragma: no cover - defensive fallback
            python_value = value
        return _coerce_numeric(python_value)
    try:
        integer_value = int(value)
    except Exception:
        try:
            return float(value)
        except Exception:
            return None
    else:
        return integer_value


def _is_retryable_exception(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".lower()
    non_retryable_patterns = (
        "not found",
        "no data found",
        "possibly delisted",
        "invalid ticker",
        "does not exist",
    )
    if any(pattern in message for pattern in non_retryable_patterns):
        return False
    retryable_patterns = (
        "timeout",
        "timed out",
        "temporarily unavailable",
        "too many requests",
        "rate limit",
        "connection reset",
        "connection aborted",
        "connection refused",
        "service unavailable",
        "502",
        "503",
        "504",
    )
    if any(pattern in message for pattern in retryable_patterns):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, asyncio.TimeoutError))


def _extract_metadata_sync(ticker: str) -> FetchResult:
    started_at = datetime.now(timezone.utc)
    ticker_obj = yf.Ticker(ticker)
    try:
        info = ticker_obj.get_info() or {}
    except Exception:
        try:
            info = ticker_obj.info or {}
        except Exception:
            info = {}
    fast_info: dict[str, Any] = {}

    if any(info.get(field) in (None, "") for field in ("marketCap", "currency", "exchange")):
        try:
            fast_info = dict(ticker_obj.fast_info)
        except Exception:
            fast_info = {}

    return FetchResult(
        ticker=ticker,
        attempts=1,
        catalog_company_name=None,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        duration_ms=0,
        company_name=_first_text(info.get("longName"), info.get("shortName"), info.get("displayName"), info.get("symbol")),
        sector=_first_text(info.get("sector")),
        industry=_first_text(info.get("industry")),
        country=_first_text(info.get("country")),
        region=_first_text(info.get("region")),
        exchange=_first_text(info.get("exchange"), info.get("fullExchangeName"), fast_info.get("exchange")),
        currency=_first_text(info.get("currency"), info.get("financialCurrency"), fast_info.get("currency")),
        revenue_ttm=_coerce_numeric(info.get("trailingAnnualRevenue") or info.get("totalRevenue")),
        market_cap=_coerce_numeric(info.get("marketCap") or fast_info.get("market_cap")),
        last_updated=datetime.now(timezone.utc),
    )


async def fetch_company_metadata(
    company: CatalogCompany,
    *,
    timeout_seconds: float,
    max_attempts: int,
    retry_base_delay_seconds: float,
) -> FetchResult | FetchFailure:
    last_error: Exception | None = None
    overall_started_at = datetime.now(timezone.utc)

    for attempt in range(1, max_attempts + 1):
        try:
            result = await asyncio.wait_for(asyncio.to_thread(_extract_metadata_sync, company.ticker), timeout=timeout_seconds)
            result.attempts = attempt
            result.catalog_company_name = company.company_name
            result.started_at = overall_started_at
            result.finished_at = datetime.now(timezone.utc)
            result.duration_ms = int((result.finished_at - overall_started_at).total_seconds() * 1000)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - exercised in live runs
            last_error = exc
            retryable = _is_retryable_exception(exc)
            if retryable and attempt < max_attempts:
                delay = retry_base_delay_seconds * (2 ** (attempt - 1))
                logger.warning("yfinance fetch failed for %s on attempt %s/%s: %s", company.ticker, attempt, max_attempts, exc)
                await asyncio.sleep(delay)
            else:
                break

    return FetchFailure(
        ticker=company.ticker,
        attempts=attempt if last_error is not None else 0,
        catalog_company_name=company.company_name,
        started_at=overall_started_at,
        finished_at=datetime.now(timezone.utc),
        duration_ms=int((datetime.now(timezone.utc) - overall_started_at).total_seconds() * 1000),
        error=str(last_error) if last_error else "Unknown yfinance failure",
    )
