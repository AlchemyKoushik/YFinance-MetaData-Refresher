from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.catalog import CatalogRepository
from app.config import get_settings
from app.database import DatabaseService
from app.logging_setup import configure_logging
from app.models import HealthResponse, RefreshResponse
from app.refresh import RefreshAlreadyRunningError, RefreshExecutionError, RefreshService
from app.time_utils import format_ist


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("ies_metadata_refresh")

    catalog = CatalogRepository(settings.catalog_path)
    database = DatabaseService(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout_seconds=settings.db_command_timeout_seconds,
    )
    service = RefreshService(settings, catalog, database)

    app.state.settings = settings
    app.state.catalog = catalog
    app.state.database = database
    app.state.service = service
    app.state.validation_report = None

    try:
        app.state.validation_report = await service.initialize()
        if app.state.validation_report.passed:
            logger.info("Startup validation passed")
        else:
            logger.warning("Startup validation completed with an unexpected incomplete state")
    except Exception:
        logger.exception("Startup validation failed")
        app.state.validation_report = None

    try:
        yield
    finally:
        await database.close()


app = FastAPI(title="IES Metadata Refresh Service", version="1.0.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


def _failure_payload(
    processed: int,
    inserted: int,
    updated: int,
    skipped: int,
    failed: int,
    duration_seconds: float,
    message: str,
    *,
    run_id: str | None = None,
    started_at_ist: str | None = None,
    finished_at_ist: str | None = None,
    total_api_calls: int = 0,
    validation_warnings: int = 0,
) -> RefreshResponse:
    return RefreshResponse(
        status="failed",
        run_id=run_id,
        started_at_ist=started_at_ist,
        finished_at_ist=finished_at_ist,
        processed=processed,
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        failed=failed,
        total_api_calls=total_api_calls,
        validation_warnings=validation_warnings,
        duration_seconds=round(duration_seconds, 3),
        message=message,
    )


@app.post("/refresh", response_model=RefreshResponse)
async def refresh(request: Request) -> RefreshResponse | JSONResponse:
    service: RefreshService = request.app.state.service
    if getattr(request.app.state, "validation_report", None) is None:
        try:
            request.app.state.validation_report = await service.validate_system()
        except Exception as exc:
            payload = _failure_payload(0, 0, 0, 0, 0, 0.0, f"Service validation failed: {exc}")
            return JSONResponse(status_code=503, content=payload.model_dump())

    if not request.app.state.validation_report.passed:
        try:
            request.app.state.validation_report = await service.validate_system()
        except Exception as exc:
            payload = _failure_payload(0, 0, 0, 0, 0, 0.0, f"Service validation failed: {exc}")
            return JSONResponse(status_code=503, content=payload.model_dump())

    try:
        summary = await service.run_refresh()
    except RefreshAlreadyRunningError:
        return JSONResponse(
            status_code=409,
            content={
                "status": "already_running",
                "message": "A metadata refresh is already in progress.",
            },
        )
    except RefreshExecutionError as exc:
        summary = exc.summary
        payload = _failure_payload(
            summary.processed,
            summary.inserted,
            summary.updated,
            summary.skipped,
            max(summary.failed, 1),
            summary.duration_seconds,
            f"Metadata refresh failed: {exc}",
            run_id=str(summary.run_id),
            started_at_ist=format_ist(summary.started_at),
            finished_at_ist=format_ist(summary.finished_at),
            total_api_calls=summary.total_api_calls,
            validation_warnings=summary.validation_warnings,
        )
        return JSONResponse(status_code=500, content=payload.model_dump())
    except Exception as exc:
        payload = _failure_payload(0, 0, 0, 0, 0, 0.0, f"Metadata refresh failed: {exc}")
        return JSONResponse(status_code=500, content=payload.model_dump())

    return RefreshResponse(
        status="success",
        run_id=str(summary.run_id),
        started_at_ist=format_ist(summary.started_at),
        finished_at_ist=format_ist(summary.finished_at),
        processed=summary.processed,
        inserted=summary.inserted,
        updated=summary.updated,
        skipped=summary.skipped,
        failed=summary.failed,
        total_api_calls=summary.total_api_calls,
        validation_warnings=summary.validation_warnings,
        duration_seconds=round(summary.duration_seconds, 3),
        message="Metadata refresh completed successfully",
    )
