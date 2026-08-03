from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.catalog import CatalogCompany, CatalogRepository
from app.config import Settings
from app.database import CompanyRefreshOutcome, DatabaseService, RefreshLogEntry
from app.services.region_normalizer import RegionNormalizationSummary
from app.time_utils import format_ist, utc_now
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
    run_id: UUID
    started_at: datetime
    finished_at: datetime
    processed: int
    inserted: int
    updated: int
    skipped: int
    failed: int
    total_api_calls: int
    validation_warnings: int
    field_update_reasons: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def duration_seconds(self) -> float:
        return self.duration_ms / 1000


class RefreshAlreadyRunningError(RuntimeError):
    pass


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
        self._refresh_guard = asyncio.Lock()

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
        if self._refresh_guard.locked():
            raise RefreshAlreadyRunningError("A metadata refresh is already in progress.")

        lock = None
        summary: RefreshSummary | None = None
        run_id: UUID | None = None
        log_entry: RefreshLogEntry | None = None
        await self._refresh_guard.acquire()
        try:
            lock = await self.database.acquire_refresh_lock()
            if lock is None:
                raise RefreshAlreadyRunningError("A metadata refresh is already in progress.")

            started_at = utc_now()
            run_id = uuid4()
            companies = self.catalog.load_companies()

            logger.info("Metadata refresh started run_id=%s companies=%s", run_id, len(companies))

            summary = RefreshSummary(
                run_id=run_id,
                started_at=started_at,
                finished_at=started_at,
                processed=len(companies),
                inserted=0,
                updated=0,
                skipped=0,
                failed=0,
                total_api_calls=0,
                validation_warnings=0,
            )
            log_entry = RefreshLogEntry(
                run_id=run_id,
                started_at=started_at,
                finished_at=started_at,
                duration_ms=0,
                processed=0,
                inserted=0,
                updated=0,
                skipped=0,
                failed=0,
                total_api_calls=0,
                validation_warnings=0,
                field_update_reasons=[],
                status="running",
            )
            log_written = False

            await self.database.insert_refresh_log(log_entry)
            log_written = True

            for batch in self._chunk_companies(companies, max(self.settings.refresh_concurrency * 4, self.settings.refresh_concurrency)):
                results = await self._fetch_batch(batch)
                for result in results:
                    summary.total_api_calls += result.attempts
                    if isinstance(result, FetchFailure):
                        summary.failed += 1
                        failure_company = CatalogCompany(ticker=result.ticker, company_name=result.catalog_company_name)
                        try:
                            await self.database.record_company_failure(
                                failure_company,
                                error_message=result.error,
                                started_at=result.started_at,
                                finished_at=result.finished_at,
                                duration_ms=result.duration_ms,
                            )
                        except Exception as exc:
                            summary.validation_warnings += 1
                            summary.field_update_reasons.append(f"{result.ticker}:failure_tracking_error:{exc}")
                            logger.exception("Failed to persist company failure for %s: %s", result.ticker, exc)
                        logger.warning("Metadata refresh failed for %s: %s", result.ticker, result.error)
                        continue

                    company_start = result.started_at
                    company_end = result.finished_at
                    try:
                        outcome = await self.database.apply_company_refresh(
                            result,
                            started_at=company_start,
                            finished_at=company_end,
                            duration_ms=result.duration_ms,
                        )
                    except Exception as exc:
                        summary.failed += 1
                        summary.field_update_reasons.append(f"{result.ticker}:database_error:{exc}")
                        logger.exception("Database refresh failed for %s: %s", result.ticker, exc)
                        continue

                    summary.validation_warnings += len(outcome.validation_warnings)
                    summary.field_update_reasons.extend(outcome.field_update_reasons)
                    if outcome.inserted:
                        summary.inserted += 1
                    elif outcome.updated_fields:
                        summary.updated += 1
                    else:
                        summary.skipped += 1

            summary.finished_at = utc_now()
            log_entry.finished_at = summary.finished_at
            log_entry.duration_ms = summary.duration_ms
            log_entry.processed = summary.processed
            log_entry.inserted = summary.inserted
            log_entry.updated = summary.updated
            log_entry.skipped = summary.skipped
            log_entry.failed = summary.failed
            log_entry.total_api_calls = summary.total_api_calls
            log_entry.validation_warnings = summary.validation_warnings
            log_entry.field_update_reasons = summary.field_update_reasons
            log_entry.status = "success" if summary.failed == 0 else "partial_failure"
            if log_written:
                await self.database.update_refresh_log(log_entry)

            try:
                region_summary = await self.database.normalize_regions()
                self._log_region_normalization_summary(region_summary)
            except Exception:
                logger.exception("Region normalization failed run_id=%s", run_id)

            logger.info(
                "Metadata refresh finished run_id=%s duration_seconds=%.3f processed=%s inserted=%s updated=%s skipped=%s failed=%s",
                run_id,
                summary.duration_seconds,
                summary.processed,
                summary.inserted,
                summary.updated,
                summary.skipped,
                summary.failed,
            )
            return summary
        except RefreshAlreadyRunningError:
            raise
        except Exception as exc:
            if summary is None or run_id is None or log_entry is None:
                summary = RefreshSummary(
                    run_id=uuid4(),
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    processed=0,
                    inserted=0,
                    updated=0,
                    skipped=0,
                    failed=1,
                    total_api_calls=0,
                    validation_warnings=0,
                )
                run_id = summary.run_id
                log_entry = RefreshLogEntry(
                    run_id=run_id,
                    started_at=summary.started_at,
                    finished_at=summary.finished_at,
                    duration_ms=summary.duration_ms,
                    processed=0,
                    inserted=0,
                    updated=0,
                    skipped=0,
                    failed=1,
                    total_api_calls=0,
                    validation_warnings=0,
                    field_update_reasons=[],
                    status="failed",
                )
            summary.finished_at = utc_now()
            log_entry.finished_at = summary.finished_at
            log_entry.duration_ms = summary.duration_ms
            log_entry.processed = summary.processed
            log_entry.inserted = summary.inserted
            log_entry.updated = summary.updated
            log_entry.skipped = summary.skipped
            log_entry.failed = max(summary.failed, 1)
            log_entry.total_api_calls = summary.total_api_calls
            log_entry.validation_warnings = summary.validation_warnings
            log_entry.field_update_reasons = summary.field_update_reasons
            log_entry.status = "failed"
            try:
                if log_written:
                    await self.database.update_refresh_log(log_entry)
            except Exception:
                logger.exception("Failed to persist refresh log failure row run_id=%s", run_id)
            logger.exception("Metadata refresh crashed run_id=%s", run_id)
            raise RefreshExecutionError("Metadata refresh crashed", summary) from exc
        finally:
            if lock is not None:
                await lock.release()
            if self._refresh_guard.locked():
                self._refresh_guard.release()

    def _log_region_normalization_summary(self, summary: RegionNormalizationSummary) -> None:
        logger.info("--------------------------------------------------------")
        logger.info("Region Normalization Started")
        logger.info("--------------------------------------------------------")
        logger.info(
            "Region normalization summary scanned=%s recognized_countries=%s rows_requiring_update=%s",
            summary.scanned,
            summary.recognized_countries,
            summary.rows_requiring_update,
        )
        if summary.updated_country_counts:
            for country in sorted(summary.updated_country_counts):
                region = self._region_for_country(country)
                logger.info("Updated %s -> %s rows=%s", country, region, summary.updated_country_counts[country])
        if summary.unknown_countries:
            for country in summary.unknown_countries:
                logger.warning('Unknown Country: "%s"', country)
        logger.info("--------------------------------------------------------")
        logger.info("Region Normalization Complete")
        logger.info("--------------------------------------------------------")
        skipped_correct = summary.skipped
        logger.info(
            "Total scanned: %s updated: %s skipped_already_correct: %s unknown_countries: %s",
            summary.scanned,
            summary.updated,
            skipped_correct,
            len(summary.unknown_countries),
        )

    @staticmethod
    def _region_for_country(country: str) -> str | None:
        from app.services.region_normalizer import expected_region_for_country

        return expected_region_for_country(country)

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
