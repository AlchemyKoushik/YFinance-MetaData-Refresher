from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.database import DatabaseService


DEFAULT_ENV_FILES = (
    Path.cwd() / ".env",
    Path.cwd() / ".env.example",
    ROOT / ".env",
    ROOT / ".env.example",
)


logger = logging.getLogger("region_backfill")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill public.ies_company_metadata.region from country values.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="Neon PostgreSQL connection string. Defaults to DATABASE_URL from the environment.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional env file to load if DATABASE_URL is not already set.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and report region changes without writing any database rows.",
    )
    parser.add_argument(
        "--country",
        action="append",
        dest="countries",
        default=[],
        help="Limit the backfill to one country. Repeat the flag to target multiple countries.",
    )
    return parser.parse_args()


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and key not in os.environ:
                os.environ[key] = value


def load_database_url(explicit_url: str | None, env_file: Path | None) -> str:
    if not explicit_url and env_file is not None:
        load_env_file(env_file)
    if not explicit_url:
        for candidate in DEFAULT_ENV_FILES:
            if "DATABASE_URL" in os.environ:
                break
            load_env_file(candidate)
    database_url = explicit_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return database_url


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def _log_summary(summary, *, dry_run: bool, countries: list[str], elapsed_seconds: float) -> None:
    logger.info("--------------------------------------------------------")
    logger.info("Region Backfill Started")
    logger.info("--------------------------------------------------------")
    logger.info("Mode: %s", "dry-run" if dry_run else "write")
    logger.info("Scope: %s", ", ".join(countries) if countries else "entire database")
    logger.info("Companies scanned: %s", summary.scanned)
    logger.info("Countries recognized: %s", summary.recognized_countries)
    logger.info("Rows requiring update: %s", summary.rows_requiring_update)
    if dry_run:
        logger.info("Dry run only: no rows were updated.")
    else:
        logger.info("Updating rows in bulk...")
    for country in sorted(summary.updated_country_counts):
        logger.info(
            "%s %s -> %s (%s rows)",
            "Would update" if dry_run else "Updated",
            country,
            _expected_region(country),
            summary.updated_country_counts[country],
        )
    for country in summary.unknown_countries:
        logger.warning('Unknown Country: "%s"', country)
    logger.info("--------------------------------------------------------")
    logger.info("Region Backfill Complete")
    logger.info("--------------------------------------------------------")
    logger.info("Total scanned: %s", summary.scanned)
    logger.info("Updated: %s", 0 if dry_run else summary.updated)
    logger.info("Skipped (already correct): %s", summary.skipped)
    logger.info("Unknown countries: %s", len(summary.unknown_countries))
    logger.info("Execution time: %.3f seconds", elapsed_seconds)


def _expected_region(country: str) -> str:
    from app.services.region_normalizer import expected_region_for_country

    return expected_region_for_country(country) or "Unknown"


async def run_backfill(args: argparse.Namespace) -> int:
    configure_logging()
    database_url = load_database_url(args.database_url, args.env_file)
    settings = Settings(database_url=database_url)
    database = DatabaseService(
        settings.database_url,
        min_size=settings.db_pool_min_size,
        max_size=settings.db_pool_max_size,
        command_timeout_seconds=settings.db_command_timeout_seconds,
    )

    await database.open()
    try:
        await database.ensure_schema()
        started = time.perf_counter()
        summary = await database.normalize_regions(
            countries=args.countries or None,
            dry_run=args.dry_run,
        )
        elapsed_seconds = time.perf_counter() - started
        _log_summary(summary, dry_run=args.dry_run, countries=args.countries, elapsed_seconds=elapsed_seconds)
    finally:
        await database.close()

    return 0


def main() -> None:
    args = parse_args()
    raise SystemExit(asyncio.run(run_backfill(args)))


if __name__ == "__main__":
    main()
