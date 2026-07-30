from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.catalog import CatalogCompany, CatalogRepository
from app.config import Settings
from app.database import DatabaseService, RefreshLogEntry
from app.yfinance_client import FetchFailure, FetchResult, fetch_company_metadata


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ValidationReport:
    database_connected: bool = False
    tables_exist: bool = False
    upsert_works: bool = False
    yfinance_works: bool = False

    @property
    def passed(self) -> bool:
        return self.database_connected and self.tables_exist and self.upsert_works and self.yfinance_works


@dataclass(slots=True)
class RefreshSummary:
    processed: int
    inserted: int
    updated: int
    failed: int
    duration_seconds: float


class RefreshExecutionError(RuntimeError):
    def __init__(self, message: str, summary: RefreshSummary) -> None:
        super().__init__(message)
        self.summary = summary


class RefreshService:
    def __init__(self, settings: Settings, catalog: CatalogRepository, database: DatabaseService) -> None:
        self.settings = settings
        self.catalog = catalog
        self.database = database
        self._validation_report: ValidationReport | None = None

    async def initialize(self) -> ValidationReport:
        await self.database.open()
        await self.database.ensure_schema()
        report = await self.validate_system()
        self._validation_report = report
        return report

    async def validate_system(self) -> ValidationReport:
        report = ValidationReport()
        await self.database.ping()
        report.database_connected = True
        await self.database.ensure_schema()
        await self.database.verify_tables_exist()
        report.tables_exist = True
        await self.database.verify_upsert()
        report.upsert_works = True
        await self._validate_yfinance()
        report.yfinance_works = True
        return report

    async def _validate_yfinance(self) -> None:
        validation_company = CatalogCompany(ticker=self.settings.startup_validation_ticker)
        result = await fetch_company_metadata(
            validation_company,
            timeout_seconds=self.settings.refresh_timeout_seconds,
            max_attempts=self.settings.refresh_max_attempts,
            retry_base_delay_seconds=self.settings.retry_base_delay_seconds,
        )
        if isinstance(result, FetchFailure):
            raise RuntimeError(f"yfinance validation failed for {validation_company.ticker}: {result.error}")

    async def run_refresh(self) -> RefreshSummary:
        started_at = datetime.now(timezone.utc)
        run_id = uuid4()
        companies = self.catalog.load_companies()

        logger.info("Metadata refresh started run_id=%s companies=%s", run_id, len(companies))

        summary = RefreshSummary(processed=0, inserted=0, updated=0, failed=0, duration_seconds=0.0)
        log_entry = RefreshLogEntry(
            run_id=run_id,
            started_at=started_at,
            finished_at=started_at,
            processed=0,
            inserted=0,
            updated=0,
            failed=0,
            duration_seconds=Decimal("0.000"),
            status="running",
        )
        log_written = False
        try:
            await self.database.insert_refresh_log(log_entry)
            log_written = True

            summary.processed = len(companies)
            for batch in self._chunk_companies(companies, max(self.settings.refresh_concurrency * 4, self.settings.refresh_concurrency)):
                results = await self._fetch_batch(batch)
                for result in results:
                    if isinstance(result, FetchFailure):
                        summary.failed += 1
                        logger.warning("Metadata refresh failed for %s: %s", result.ticker, result.error)
                        continue

                    try:
                        inserted = await self.database.upsert_company(result)
                    except Exception as exc:  # pragma: no cover - defensive live path
                        summary.failed += 1
                        logger.exception("Database upsert failed for %s: %s", result.ticker, exc)
                        continue

                    if inserted:
                        summary.inserted += 1
                    else:
                        summary.updated += 1

            summary.duration_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
            log_entry.finished_at = datetime.now(timezone.utc)
            log_entry.processed = summary.processed
            log_entry.inserted = summary.inserted
            log_entry.updated = summary.updated
            log_entry.failed = summary.failed
            log_entry.duration_seconds = Decimal(str(round(summary.duration_seconds, 3)))
            log_entry.status = "success" if summary.failed == 0 else "partial_failure"
            if log_written:
                await self.database.update_refresh_log(log_entry)

            logger.info(
                "Metadata refresh finished run_id=%s duration_seconds=%.3f processed=%s inserted=%s updated=%s failed=%s",
                run_id,
                summary.duration_seconds,
                summary.processed,
                summary.inserted,
                summary.updated,
                summary.failed,
            )
            return summary
        except Exception as exc:
            log_entry.finished_at = datetime.now(timezone.utc)
            log_entry.duration_seconds = Decimal(str(round((log_entry.finished_at - started_at).total_seconds(), 3)))
            log_entry.processed = summary.processed
            log_entry.inserted = summary.inserted
            log_entry.updated = summary.updated
            log_entry.failed = max(summary.failed, 1)
            log_entry.status = "failed"
            try:
                if log_written:
                    await self.database.update_refresh_log(log_entry)
            except Exception:
                logger.exception("Failed to persist refresh log failure row run_id=%s", run_id)
            logger.exception("Metadata refresh crashed run_id=%s", run_id)
            raise RefreshExecutionError("Metadata refresh crashed", summary) from exc

    def _chunk_companies(self, companies: list[CatalogCompany], chunk_size: int):
        for index in range(0, len(companies), chunk_size):
            yield companies[index : index + chunk_size]

    async def _fetch_batch(self, companies: list[CatalogCompany]) -> list[FetchResult | FetchFailure]:
        semaphore = asyncio.Semaphore(self.settings.refresh_concurrency)

        async def worker(company: CatalogCompany) -> FetchResult | FetchFailure:
            async with semaphore:
                return await fetch_company_metadata(
                    company,
                    timeout_seconds=self.settings.refresh_timeout_seconds,
                    max_attempts=self.settings.refresh_max_attempts,
                    retry_base_delay_seconds=self.settings.retry_base_delay_seconds,
                )

        tasks = [asyncio.create_task(worker(company)) for company in companies]
        results: list[FetchResult | FetchFailure] = []
        for task in asyncio.as_completed(tasks):
            results.append(await task)
        return results
