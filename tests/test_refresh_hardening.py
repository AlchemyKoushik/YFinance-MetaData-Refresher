from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.catalog import CatalogCompany
from app.config import Settings
from app.database import CompanyMetadataRow, DatabaseService
from app.refresh import RefreshAlreadyRunningError, RefreshService
from app.yfinance_client import FetchFailure, FetchResult, fetch_company_metadata


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _company_row(
    *,
    ticker: str,
    company_name: str = "Alpha Corp",
    sector: str = "Technology",
    industry: str = "Software",
    country: str = "US",
    region: str = "North America",
    exchange: str = "NYSE",
    currency: str = "USD",
    revenue_ttm: Decimal | None = Decimal("5200000000"),
    market_cap: Decimal | None = Decimal("11000000000"),
    refresh_status: str = "success",
) -> CompanyMetadataRow:
    now = _utc_now()
    return CompanyMetadataRow(
        ticker=ticker,
        company_name=company_name,
        sector=sector,
        industry=industry,
        country=country,
        region=region,
        exchange=exchange,
        currency=currency,
        revenue_ttm=revenue_ttm,
        market_cap=market_cap,
        last_successful_refresh=now,
        last_refresh_attempt=now,
        refresh_status=refresh_status,
        last_error_message=None,
        refresh_duration_ms=100,
        last_updated=now,
    )


class DummyCatalog:
    def __init__(self, companies: list[CatalogCompany]) -> None:
        self._companies = companies

    def load_companies(self) -> list[CatalogCompany]:
        return list(self._companies)


class FakeLock:
    def __init__(self, database: "FakeDatabase") -> None:
        self.database = database
        self.released = False

    async def release(self) -> None:
        self.released = True
        self.database.locked = False


class FakeDatabase:
    def __init__(self, seed_rows: dict[str, CompanyMetadataRow] | None = None) -> None:
        self.locked = False
        self.rows = dict(seed_rows or {})
        self.logs: list[object] = []
        self.updated_logs: list[object] = []
        self.failure_updates: list[tuple[str, str]] = []
        self.applied_updates: list[str] = []

    async def open(self) -> None:
        return None

    async def ensure_schema(self) -> None:
        return None

    async def ping(self) -> None:
        return None

    async def verify_tables_exist(self) -> None:
        return None

    async def verify_upsert(self) -> None:
        return None

    async def acquire_refresh_lock(self):
        if self.locked:
            return None
        self.locked = True
        return FakeLock(self)

    async def insert_refresh_log(self, entry) -> None:
        self.logs.append(entry)

    async def update_refresh_log(self, entry) -> None:
        self.updated_logs.append(entry)

    async def apply_company_refresh(self, record, *, started_at, finished_at, duration_ms):
        helper = DatabaseService("postgres://example", min_size=1, max_size=1, command_timeout_seconds=1)
        existing = self.rows.get(record.ticker)
        merged, updated_fields, skipped_fields, validation_warnings, field_update_reasons = helper._merge_company_values(existing, record)
        now = finished_at
        if existing is None:
            self.rows[record.ticker] = CompanyMetadataRow(
                ticker=record.ticker,
                company_name=merged["company_name"],
                sector=merged["sector"],
                industry=merged["industry"],
                country=merged["country"],
                region=merged["region"],
                exchange=merged["exchange"],
                currency=merged["currency"],
                revenue_ttm=merged["revenue_ttm"],
                market_cap=merged["market_cap"],
                last_successful_refresh=now,
                last_refresh_attempt=started_at,
                refresh_status="success" if updated_fields else "unchanged",
                last_error_message=None,
                refresh_duration_ms=duration_ms,
                last_updated=now,
            )
            self.applied_updates.append(record.ticker)
            return type("Outcome", (), {
                "inserted": True,
                "updated_fields": updated_fields,
                "skipped_fields": skipped_fields,
                "validation_warnings": validation_warnings,
                "field_update_reasons": field_update_reasons,
            })()

        self.rows[record.ticker] = CompanyMetadataRow(
            ticker=record.ticker,
            company_name=merged["company_name"],
            sector=merged["sector"],
            industry=merged["industry"],
            country=merged["country"],
            region=merged["region"],
            exchange=merged["exchange"],
            currency=merged["currency"],
            revenue_ttm=merged["revenue_ttm"],
            market_cap=merged["market_cap"],
            last_successful_refresh=now if updated_fields else existing.last_successful_refresh,
            last_refresh_attempt=started_at,
            refresh_status="success" if updated_fields else "unchanged",
            last_error_message=existing.last_error_message,
            refresh_duration_ms=duration_ms,
            last_updated=now if updated_fields else existing.last_updated,
        )
        self.applied_updates.append(record.ticker)
        return type("Outcome", (), {
            "inserted": False,
            "updated_fields": updated_fields,
            "skipped_fields": skipped_fields,
            "validation_warnings": validation_warnings,
            "field_update_reasons": field_update_reasons,
        })()

    async def record_company_failure(self, company, *, error_message, started_at, finished_at, duration_ms):
        existing = self.rows.get(company.ticker)
        if existing is None:
            self.rows[company.ticker] = CompanyMetadataRow(
                ticker=company.ticker,
                company_name=company.company_name or company.ticker,
                sector="",
                industry="",
                country="",
                region="",
                exchange="",
                currency="",
                revenue_ttm=None,
                market_cap=None,
                last_successful_refresh=None,
                last_refresh_attempt=started_at,
                refresh_status="failed",
                last_error_message=error_message,
                refresh_duration_ms=duration_ms,
                last_updated=finished_at,
            )
        else:
            self.rows[company.ticker] = CompanyMetadataRow(
                ticker=existing.ticker,
                company_name=existing.company_name,
                sector=existing.sector,
                industry=existing.industry,
                country=existing.country,
                region=existing.region,
                exchange=existing.exchange,
                currency=existing.currency,
                revenue_ttm=existing.revenue_ttm,
                market_cap=existing.market_cap,
                last_successful_refresh=existing.last_successful_refresh,
                last_refresh_attempt=started_at,
                refresh_status="failed",
                last_error_message=error_message,
                refresh_duration_ms=duration_ms,
                last_updated=finished_at,
            )
        self.failure_updates.append((company.ticker, error_message))
        return type("Outcome", (), {
            "inserted": existing is None,
            "updated_fields": [],
            "skipped_fields": [],
            "validation_warnings": [],
            "field_update_reasons": [f"{company.ticker}:failed:{error_message}"],
        })()


class RefreshHardeningTests(unittest.IsolatedAsyncioTestCase):
    def test_merge_preserves_existing_values_on_partial_fetch(self) -> None:
        db = DatabaseService("postgres://example", min_size=1, max_size=1, command_timeout_seconds=1)
        existing = _company_row(ticker="ABC")
        record = FetchResult(
            ticker="ABC",
            attempts=1,
            catalog_company_name=None,
            started_at=_utc_now(),
            finished_at=_utc_now(),
            duration_ms=12,
            company_name=None,
            sector=None,
            industry=None,
            country=None,
            region=None,
            exchange=None,
            currency=None,
            revenue_ttm=None,
            market_cap=Decimal("11400000000"),
            last_updated=_utc_now(),
        )

        merged, updated_fields, skipped_fields, validation_warnings, reasons = db._merge_company_values(existing, record)

        self.assertEqual(merged["revenue_ttm"], Decimal("5200000000"))
        self.assertEqual(merged["market_cap"], Decimal("11400000000"))
        self.assertIn("market_cap", updated_fields)
        self.assertIn("revenue_ttm", skipped_fields)
        self.assertFalse(validation_warnings)
        self.assertIn("ABC.market_cap:newer_information", reasons)

    def test_merge_rejects_zero_market_cap_without_erasing_existing_value(self) -> None:
        db = DatabaseService("postgres://example", min_size=1, max_size=1, command_timeout_seconds=1)
        existing = _company_row(ticker="ABC")
        record = FetchResult(
            ticker="ABC",
            attempts=1,
            catalog_company_name=None,
            started_at=_utc_now(),
            finished_at=_utc_now(),
            duration_ms=12,
            company_name="Alpha Corp",
            sector="Technology",
            industry="Software",
            country="US",
            region="North America",
            exchange="NYSE",
            currency="USD",
            revenue_ttm=None,
            market_cap=0,
            last_updated=_utc_now(),
        )

        merged, _, _, validation_warnings, _ = db._merge_company_values(existing, record)

        self.assertEqual(merged["market_cap"], Decimal("11000000000"))
        self.assertIn("ABC.market_cap:non_positive_value_skipped", validation_warnings)

    async def test_fetch_retries_transient_errors_and_counts_attempts(self) -> None:
        calls: list[int] = []
        tick = CatalogCompany(ticker="MSFT")

        def fake_extract(_: str):
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("temporary timeout")
            now = _utc_now()
            return FetchResult(
                ticker="MSFT",
                attempts=1,
                catalog_company_name=None,
                started_at=now,
                finished_at=now,
                duration_ms=0,
                company_name="Microsoft",
                sector="Technology",
                industry="Software",
                country="US",
                region="North America",
                exchange="NMS",
                currency="USD",
                revenue_ttm=Decimal("100"),
                market_cap=Decimal("200"),
                last_updated=now,
            )

        with patch("app.yfinance_client._extract_metadata_sync", side_effect=fake_extract):
            result = await fetch_company_metadata(
                tick,
                timeout_seconds=5,
                max_attempts=3,
                retry_base_delay_seconds=0,
            )

        self.assertIsInstance(result, FetchResult)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(calls), 3)

    async def test_service_rejects_overlapping_refresh_requests(self) -> None:
        gate = asyncio.Event()
        fetch_calls: list[str] = []

        async def fake_fetch(company, **kwargs):
            fetch_calls.append(company.ticker)
            await gate.wait()
            now = _utc_now()
            return FetchResult(
                ticker=company.ticker,
                attempts=1,
                catalog_company_name=company.company_name,
                started_at=now,
                finished_at=now,
                duration_ms=0,
                company_name=company.company_name or company.ticker,
                sector="Technology",
                industry="Software",
                country="US",
                region="North America",
                exchange="NYSE",
                currency="USD",
                revenue_ttm=Decimal("1"),
                market_cap=Decimal("2"),
                last_updated=now,
            )

        fake_db = FakeDatabase({"ABC": _company_row(ticker="ABC")})
        service = RefreshService(
            Settings(
                database_url="postgres://example",
                catalog_path=Path("data/ies_catalog.json"),
            ),
            DummyCatalog([CatalogCompany(ticker="ABC", company_name="Alpha Corp")]),
            fake_db,  # type: ignore[arg-type]
        )

        with patch("app.refresh.fetch_company_metadata", side_effect=fake_fetch):
            first_task = asyncio.create_task(service.run_refresh())
            await asyncio.sleep(0)
            with self.assertRaises(RefreshAlreadyRunningError):
                await service.run_refresh()
            gate.set()
            summary = await first_task

        self.assertEqual(summary.processed, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(fetch_calls, ["ABC"])

    async def test_service_preserves_data_on_partial_failure_and_is_idempotent(self) -> None:
        responses = [
            FetchResult(
                ticker="ABC",
                attempts=1,
                catalog_company_name="Alpha Corp",
                started_at=_utc_now(),
                finished_at=_utc_now(),
                duration_ms=10,
                company_name=None,
                sector=None,
                industry=None,
                country=None,
                region=None,
                exchange=None,
                currency=None,
                revenue_ttm=None,
                market_cap=Decimal("11400000000"),
                last_updated=_utc_now(),
            ),
            FetchFailure(
                ticker="XYZ",
                attempts=2,
                catalog_company_name="Xylophone Inc",
                started_at=_utc_now(),
                finished_at=_utc_now(),
                duration_ms=20,
                error="temporary outage",
            ),
        ]

        async def fake_fetch(company, **kwargs):
            if company.ticker == "ABC":
                return responses[0]
            return responses[1]

        fake_db = FakeDatabase(
            {
                "ABC": _company_row(ticker="ABC"),
                "XYZ": _company_row(ticker="XYZ", company_name="Xylophone Inc", revenue_ttm=Decimal("900"), market_cap=Decimal("1000")),
            }
        )
        service = RefreshService(
            Settings(
                database_url="postgres://example",
                catalog_path=Path("data/ies_catalog.json"),
            ),
            DummyCatalog([CatalogCompany(ticker="ABC", company_name="Alpha Corp"), CatalogCompany(ticker="XYZ", company_name="Xylophone Inc")]),
            fake_db,  # type: ignore[arg-type]
        )

        with patch("app.refresh.fetch_company_metadata", side_effect=fake_fetch):
            first = await service.run_refresh()
            second = await service.run_refresh()

        self.assertEqual(first.failed, 1)
        self.assertEqual(first.updated, 1)
        self.assertEqual(first.skipped, 0)
        self.assertEqual(second.failed, 1)
        self.assertEqual(second.updated, 0)
        self.assertEqual(second.skipped, 1)
        self.assertEqual(fake_db.rows["ABC"].market_cap, Decimal("11400000000"))
        self.assertEqual(fake_db.rows["ABC"].revenue_ttm, Decimal("5200000000"))
        self.assertEqual(fake_db.rows["XYZ"].market_cap, Decimal("1000"))
        self.assertEqual(fake_db.rows["XYZ"].revenue_ttm, Decimal("900"))

    async def test_database_apply_company_refresh_rolls_back_on_failure(self) -> None:
        class FakeTransaction:
            def __init__(self) -> None:
                self.started = 0
                self.committed = 0
                self.rolled_back = 0

            async def start(self) -> None:
                self.started += 1

            async def commit(self) -> None:
                self.committed += 1

            async def rollback(self) -> None:
                self.rolled_back += 1

        class FakeConnection:
            def __init__(self) -> None:
                self.transaction_obj = FakeTransaction()
                self.execute_calls: list[str] = []

            def transaction(self):
                return self.transaction_obj

            async def fetchrow(self, query, *args):
                return {
                    "ticker": "ABC",
                    "company_name": "Alpha Corp",
                    "sector": "Technology",
                    "industry": "Software",
                    "country": "US",
                    "region": "North America",
                    "exchange": "NYSE",
                    "currency": "USD",
                    "revenue_ttm": Decimal("5200000000"),
                    "market_cap": Decimal("11000000000"),
                    "last_successful_refresh": None,
                    "last_refresh_attempt": None,
                    "refresh_status": "success",
                    "last_error_message": None,
                    "refresh_duration_ms": None,
                    "last_updated": _utc_now(),
                }

            async def execute(self, query, *args):
                self.execute_calls.append(query)
                raise RuntimeError("write failed")

        class FakeAcquire:
            def __init__(self, connection):
                self.connection = connection

            async def __aenter__(self):
                return self.connection

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakePool:
            def __init__(self, connection):
                self.connection = connection

            def acquire(self):
                return FakeAcquire(self.connection)

        db = DatabaseService("postgres://example", min_size=1, max_size=1, command_timeout_seconds=1)
        fake_connection = FakeConnection()
        db.pool = FakePool(fake_connection)  # type: ignore[assignment]

        record = FetchResult(
            ticker="ABC",
            attempts=1,
            catalog_company_name=None,
            started_at=_utc_now(),
            finished_at=_utc_now(),
            duration_ms=1,
            company_name="Alpha Corp",
            sector="Technology",
            industry="Software",
            country="US",
            region="North America",
            exchange="NYSE",
            currency="USD",
            revenue_ttm=Decimal("5200000000"),
            market_cap=Decimal("11400000000"),
            last_updated=_utc_now(),
        )

        with self.assertRaises(RuntimeError):
            await db.apply_company_refresh(record, started_at=record.started_at, finished_at=record.finished_at, duration_ms=record.duration_ms)

        self.assertEqual(fake_connection.transaction_obj.rolled_back, 1)
        self.assertEqual(fake_connection.transaction_obj.committed, 0)


if __name__ == "__main__":
    unittest.main()
