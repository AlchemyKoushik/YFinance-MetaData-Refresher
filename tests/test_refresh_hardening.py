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
from app.services.region_normalizer import (
    country_filter_terms,
    expected_region_for_country,
    plan_region_normalization,
    RegionNormalizationInput,
    RegionNormalizationUpdate,
)
from app.yfinance_client import FetchFailure, FetchResult, fetch_company_metadata


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _company_row(
    *,
    ticker: str,
    company_name: str = "Alpha Corp",
    sector: str = "Technology",
    industry: str = "Software",
    listing_country: str = "United Kingdom",
    listing_region: str = "Europe",
    listing_exchange: str = "LSE",
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
        listing_country=listing_country,
        listing_region=listing_region,
        listing_exchange=listing_exchange,
        company_country=country,
        company_region=region,
        company_exchange=exchange,
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
        self.region_normalization_calls = 0
        self.region_normalization_summary = None

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
                listing_country=merged["listing_country"],
                listing_region=merged["listing_region"],
                listing_exchange=merged["listing_exchange"],
                company_country=merged["company_country"],
                company_region=merged["company_region"],
                company_exchange=merged["company_exchange"],
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
            listing_country=merged["listing_country"],
            listing_region=merged["listing_region"],
            listing_exchange=merged["listing_exchange"],
            company_country=merged["company_country"],
            company_region=merged["company_region"],
            company_exchange=merged["company_exchange"],
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
                listing_country=company.listing_country or "",
                listing_region=company.listing_region or "",
                listing_exchange=company.listing_exchange or "",
                company_country="",
                company_region="",
                company_exchange="",
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
                listing_country=existing.listing_country,
                listing_region=existing.listing_region,
                listing_exchange=existing.listing_exchange,
                company_country=existing.company_country,
                company_region=existing.company_region,
                company_exchange=existing.company_exchange,
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

    async def normalize_regions(self, *, countries=None, dry_run=False):
        self.region_normalization_calls += 1
        if countries:
            filter_terms = set()
            for country in countries:
                filter_terms.update(term.strip() for term in country_filter_terms(country))
            rows = [
                RegionNormalizationInput(ticker=row.ticker, country=row.country, current_region=row.region)
                for row in self.rows.values()
                if row.country and row.country.strip() in filter_terms
            ]
        else:
            rows = [
                RegionNormalizationInput(ticker=row.ticker, country=row.country, current_region=row.region)
                for row in self.rows.values()
                if row.country
            ]
        plan = plan_region_normalization(rows)
        self.region_normalization_summary = plan.summary
        if not dry_run:
            for update in plan.updates:
                row = self.rows[update.ticker]
                self.rows[update.ticker] = CompanyMetadataRow(
                    ticker=row.ticker,
                    company_name=row.company_name,
                    sector=row.sector,
                    industry=row.industry,
                    listing_country=row.listing_country,
                    listing_region=row.listing_region,
                    listing_exchange=row.listing_exchange,
                    company_country=row.company_country,
                    company_region=update.expected_region,
                    company_exchange=row.company_exchange,
                    currency=row.currency,
                    revenue_ttm=row.revenue_ttm,
                    market_cap=row.market_cap,
                    last_successful_refresh=row.last_successful_refresh,
                    last_refresh_attempt=row.last_refresh_attempt,
                    refresh_status=row.refresh_status,
                    last_error_message=row.last_error_message,
                    refresh_duration_ms=row.refresh_duration_ms,
                    last_updated=row.last_updated,
                )
        return plan.summary


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
            listing_country=None,
            listing_region=None,
            listing_exchange=None,
            company_country=None,
            company_region=None,
            company_exchange=None,
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

    def test_merge_preserves_listing_country_separately_from_company_country(self) -> None:
        db = DatabaseService("postgres://example", min_size=1, max_size=1, command_timeout_seconds=1)
        existing = _company_row(
            ticker="ABC",
            listing_country="United Kingdom",
            listing_region="Europe",
            listing_exchange="LSE",
            country="United States",
            region="North America",
            exchange="NYSE",
        )
        record = FetchResult(
            ticker="ABC",
            attempts=1,
            catalog_company_name="Alpha Corp",
            started_at=_utc_now(),
            finished_at=_utc_now(),
            duration_ms=12,
            company_name="Alpha Corp",
            sector="Technology",
            industry="Software",
            listing_country="United Kingdom",
            listing_region="Europe",
            listing_exchange="LSE",
            company_country="France",
            company_region="Europe",
            company_exchange="PAR",
            currency="EUR",
            revenue_ttm=None,
            market_cap=Decimal("11400000000"),
            last_updated=_utc_now(),
        )

        merged, updated_fields, _, _, _ = db._merge_company_values(existing, record)

        self.assertEqual(merged["listing_country"], "United Kingdom")
        self.assertEqual(merged["listing_region"], "Europe")
        self.assertEqual(merged["listing_exchange"], "LSE")
        self.assertEqual(merged["company_country"], "France")
        self.assertEqual(merged["company_region"], "Europe")
        self.assertEqual(merged["company_exchange"], "PAR")
        self.assertEqual(existing.country, "United States")
        self.assertIn("company_country", updated_fields)
        self.assertNotIn("listing_country", updated_fields)
        self.assertNotIn("listing_region", updated_fields)
        self.assertNotIn("listing_exchange", updated_fields)

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
            listing_country="United Kingdom",
            listing_region="Europe",
            listing_exchange="LSE",
            company_country="US",
            company_region="North America",
            company_exchange="NYSE",
            currency="USD",
            revenue_ttm=None,
            market_cap=0,
            last_updated=_utc_now(),
        )

        merged, _, _, validation_warnings, _ = db._merge_company_values(existing, record)

        self.assertEqual(merged["market_cap"], Decimal("11000000000"))
        self.assertIn("ABC.market_cap:non_positive_value_skipped", validation_warnings)

    def test_region_mapping_covers_common_country_names(self) -> None:
        self.assertEqual(expected_region_for_country("United States"), "North America")
        self.assertEqual(expected_region_for_country("USA"), "North America")
        self.assertEqual(expected_region_for_country("India"), "Asia")
        self.assertEqual(expected_region_for_country("UAE"), "Asia")
        self.assertEqual(expected_region_for_country("United Kingdom"), "Europe")
        self.assertEqual(expected_region_for_country("Brazil"), "South America")
        self.assertEqual(expected_region_for_country("Australia"), "Oceania")
        self.assertEqual(expected_region_for_country("South Africa"), "Africa")

    def test_region_plan_skips_unknown_and_already_correct_rows(self) -> None:
        plan = plan_region_normalization(
            [
                RegionNormalizationInput(ticker="AAA", country="India", current_region="US"),
                RegionNormalizationInput(ticker="BBB", country="Germany", current_region="Europe"),
                RegionNormalizationInput(ticker="CCC", country="Congo DR", current_region="US"),
            ]
        )

        self.assertEqual(plan.summary.scanned, 3)
        self.assertEqual(plan.summary.rows_requiring_update, 1)
        self.assertEqual(plan.summary.updated, 1)
        self.assertEqual(plan.summary.skipped, 1)
        self.assertEqual(plan.summary.unknown_countries, ["Congo DR"])
        self.assertEqual(plan.updates[0].expected_region, "Asia")

    def test_country_filter_terms_resolve_aliases(self) -> None:
        terms = country_filter_terms("USA")
        self.assertIn("United States", terms)
        self.assertIn("US", terms)

    async def test_bulk_region_update_uses_typed_arrays(self) -> None:
        class CaptureConnection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[object, ...]]] = []

            async def execute(self, query, *args):
                self.calls.append((query, args))

        db = DatabaseService("postgres://example", min_size=1, max_size=1, command_timeout_seconds=1)
        connection = CaptureConnection()

        await db._bulk_update_regions(
            connection,  # type: ignore[arg-type]
            [RegionNormalizationUpdate(ticker="ABC", country="India", expected_region="Asia")],
        )

        self.assertEqual(len(connection.calls), 1)
        query, args = connection.calls[0]
        self.assertIn("unnest($1::text[], $2::text[], $3::timestamptz[])", query)
        self.assertEqual(args[0], ["ABC"])
        self.assertEqual(args[1], ["Asia"])
        self.assertEqual(len(args[2]), 1)
        self.assertIsInstance(args[2][0], datetime)

    async def test_region_backfill_dry_run_does_not_mutate_rows(self) -> None:
        fake_db = FakeDatabase({"ABC": _company_row(ticker="ABC", country="India", region="US")})

        summary = await fake_db.normalize_regions(countries=["India"], dry_run=True)

        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.rows_requiring_update, 1)
        self.assertEqual(fake_db.rows["ABC"].region, "US")

    async def test_region_backfill_country_filter_updates_only_matching_rows(self) -> None:
        fake_db = FakeDatabase(
            {
                "IND": _company_row(ticker="IND", country="India", region="US"),
                "DEU": _company_row(ticker="DEU", country="Germany", region="US"),
            }
        )

        summary = await fake_db.normalize_regions(countries=["India"])

        self.assertEqual(summary.scanned, 1)
        self.assertEqual(summary.rows_requiring_update, 1)
        self.assertEqual(fake_db.rows["IND"].region, "Asia")
        self.assertEqual(fake_db.rows["DEU"].region, "US")

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
                listing_country="United Kingdom",
                listing_region="Europe",
                listing_exchange="LSE",
                company_country="US",
                company_region="North America",
                company_exchange="NMS",
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
                listing_country="United Kingdom",
                listing_region="Europe",
                listing_exchange="LSE",
                company_country="US",
                company_region="North America",
                company_exchange="NYSE",
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
                listing_country="United Kingdom",
                listing_region="Europe",
                listing_exchange="LSE",
                company_country=None,
                company_region=None,
                company_exchange=None,
                currency=None,
                revenue_ttm=None,
                market_cap=Decimal("11400000000"),
                last_updated=_utc_now(),
            ),
            FetchFailure(
                ticker="XYZ",
                attempts=2,
                catalog_company_name="Xylophone Inc",
                listing_country="Germany",
                listing_region="Europe",
                listing_exchange="FRA",
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
        self.assertEqual(fake_db.region_normalization_calls, 2)
        self.assertEqual(fake_db.rows["ABC"].region, "North America")

    async def test_service_invokes_region_normalization_after_successful_refresh(self) -> None:
        async def fake_fetch(company, **kwargs):
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
                listing_country="India",
                listing_region="Asia",
                listing_exchange="NSE",
                company_country="India",
                company_region="US",
                company_exchange="NSE",
                currency="INR",
                revenue_ttm=Decimal("1"),
                market_cap=Decimal("2"),
                last_updated=now,
            )

        fake_db = FakeDatabase()
        service = RefreshService(
            Settings(
                database_url="postgres://example",
                catalog_path=Path("data/ies_catalog.json"),
            ),
            DummyCatalog([CatalogCompany(ticker="ABC", company_name="Alpha Corp")]),
            fake_db,  # type: ignore[arg-type]
        )

        with patch("app.refresh.fetch_company_metadata", side_effect=fake_fetch):
            summary = await service.run_refresh()

        self.assertEqual(summary.failed, 0)
        self.assertEqual(fake_db.region_normalization_calls, 1)
        self.assertEqual(fake_db.rows["ABC"].region, "Asia")

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
            listing_country="United Kingdom",
            listing_region="Europe",
            listing_exchange="LSE",
            company_country="US",
            company_region="North America",
            company_exchange="NYSE",
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
