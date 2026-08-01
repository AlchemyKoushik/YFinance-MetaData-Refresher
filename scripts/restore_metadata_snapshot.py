from __future__ import annotations

import argparse
import asyncio
import csv
import os
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import asyncpg


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


TABLE_SCHEMA = "public"
TABLE_NAME = "ies_company_metadata"
DEFAULT_CSV_PATH = Path(
    r"C:\Users\KoushikBhandary\OneDrive - Alchemy Research and Analytics\Centralized Downloads\ies_company_metadata.csv"
)
PROGRESS_INTERVAL = 5000
EXCLUDED_COLUMNS = {
    "id",
    "last_successful_refresh",
    "last_refresh_attempt",
    "refresh_status",
    "last_error_message",
    "refresh_duration_ms",
}


@dataclass(slots=True)
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool


@dataclass(slots=True)
class RestoreSummary:
    total_rows_processed: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    rows_not_found: int = 0
    validation_errors: list[str] = field(default_factory=list)


logger = logging.getLogger("metadata_restore")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-time safe restore utility for ies_company_metadata")
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=DEFAULT_CSV_PATH,
        help="Path to the known-good metadata CSV snapshot.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Neon PostgreSQL connection string. Defaults to DATABASE_URL from the environment.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and compare rows without writing to the database.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional env file to load if DATABASE_URL is not already set.",
    )
    parser.add_argument(
        "--progress-interval",
        type=int,
        default=PROGRESS_INTERVAL,
        help="Log progress after every N processed rows.",
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
        for candidate in (
            Path.cwd() / ".env",
            Path.cwd() / ".env.example",
            ROOT / ".env",
            ROOT / ".env.example",
        ):
            if "DATABASE_URL" in os.environ:
                break
            load_env_file(candidate)

    database_url = explicit_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. Provide --database-url or set the environment variable.")
    return database_url


def load_csv_rows(csv_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV snapshot not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise RuntimeError("CSV file is missing a header row")
        rows = [dict(row) for row in reader]
        return rows, list(reader.fieldnames)


def normalize_ticker(raw_value: Any) -> str | None:
    if raw_value is None:
        return None
    text = str(raw_value).strip().upper()
    return text or None


def parse_timestamp(raw_value: str) -> datetime | None:
    text = raw_value.strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def coerce_value(raw_value: Any, column: ColumnInfo) -> tuple[Any, str | None]:
    if raw_value is None:
        raw_text = ""
    else:
        raw_text = str(raw_value).strip()

    if raw_text == "":
        if column.data_type in {"text", "character varying", "character", "citext"}:
            return "", None
        if column.is_nullable:
            return None, None
        return None, f"{column.name}: blank value is not allowed"

    if column.data_type in {"text", "character varying", "character", "citext"}:
        return raw_text, None

    if column.data_type in {"numeric", "real", "double precision"}:
        try:
            return Decimal(raw_text), None
        except (InvalidOperation, ValueError):
            return None, f"{column.name}: invalid numeric value '{raw_text}'"

    if column.data_type in {"smallint", "integer", "bigint"}:
        try:
            return int(raw_text), None
        except ValueError:
            return None, f"{column.name}: invalid integer value '{raw_text}'"

    if column.data_type in {"timestamp with time zone", "timestamp without time zone"}:
        try:
            return parse_timestamp(raw_text), None
        except ValueError:
            return None, f"{column.name}: invalid timestamp value '{raw_text}'"

    if column.data_type == "boolean":
        lowered = raw_text.lower()
        if lowered in {"true", "t", "1", "yes", "y"}:
            return True, None
        if lowered in {"false", "f", "0", "no", "n"}:
            return False, None
        return None, f"{column.name}: invalid boolean value '{raw_text}'"

    return raw_text, None


def values_differ(existing_value: Any, new_value: Any, data_type: str) -> bool:
    if existing_value is None and new_value is None:
        return False
    if existing_value is None or new_value is None:
        return True

    if data_type in {"numeric", "real", "double precision"}:
        return Decimal(str(existing_value)) != Decimal(str(new_value))
    if data_type in {"smallint", "integer", "bigint"}:
        return int(existing_value) != int(new_value)
    if data_type in {"timestamp with time zone", "timestamp without time zone"}:
        existing_dt = existing_value if existing_value.tzinfo is not None else existing_value.replace(tzinfo=timezone.utc)
        new_dt = new_value if new_value.tzinfo is not None else new_value.replace(tzinfo=timezone.utc)
        return existing_dt != new_dt
    return str(existing_value) != str(new_value)


def build_bulk_restore_records(
    csv_rows: list[dict[str, str]],
    csv_columns: list[str],
    column_info: dict[str, ColumnInfo],
    *,
    progress_interval: int,
) -> tuple[list[str], list[tuple[Any, ...]], RestoreSummary]:
    summary = RestoreSummary(total_rows_processed=len(csv_rows))
    known_columns = set(column_info)
    restore_columns = [
        column
        for column in csv_columns
        if column not in EXCLUDED_COLUMNS and column in known_columns
    ]
    unknown_columns = [
        column
        for column in csv_columns
        if column not in EXCLUDED_COLUMNS and column not in known_columns
    ]
    for column in unknown_columns:
        summary.validation_errors.append(f"CSV column '{column}' does not exist in {TABLE_SCHEMA}.{TABLE_NAME} and was ignored.")

    if "ticker" not in restore_columns:
        raise RuntimeError("The CSV snapshot must contain a ticker column")

    logger.info(
        "Loaded CSV snapshot rows=%s columns=%s restore_columns=%s",
        len(csv_rows),
        len(csv_columns),
        len(restore_columns),
    )
    if unknown_columns:
        logger.info("Ignored %s CSV columns that do not exist in the target table", len(unknown_columns))

    records: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    logger.info("Validating rows and building bulk load payload")

    for index, row in enumerate(csv_rows, start=2):
        if progress_interval > 0 and (index - 1) % progress_interval == 0:
            logger.info(
                "Validated %s/%s CSV rows | valid=%s skipped=%s validation_errors=%s",
                index - 1,
                len(csv_rows),
                len(records),
                summary.rows_skipped,
                len(summary.validation_errors),
            )

        row_errors: list[str] = []
        normalized_row: list[Any] = []
        ticker = normalize_ticker(row.get("ticker"))
        if ticker is None:
            summary.rows_skipped += 1
            summary.validation_errors.append(f"Row {index}: missing ticker")
            continue
        if ticker in seen:
            summary.rows_skipped += 1
            summary.validation_errors.append(f"Row {index}: duplicate ticker '{ticker}'")
            continue
        seen.add(ticker)

        for column_name in restore_columns:
            column = column_info[column_name]
            value, error = coerce_value(row.get(column_name), column)
            if error:
                row_errors.append(f"Row {index} ({ticker}): {error}")
                continue
            normalized_row.append(value)

        if row_errors:
            summary.rows_skipped += 1
            summary.validation_errors.extend(row_errors)
            continue

        records.append(tuple(normalized_row))

    summary.rows_updated = len(records)
    return restore_columns, records, summary


async def bulk_replace_table(
    connection: asyncpg.Connection,
    *,
    restore_columns: list[str],
    records: list[tuple[Any, ...]],
    progress_interval: int,
) -> None:
    logger.info("Preparing bulk replace: truncating %s.%s", TABLE_SCHEMA, TABLE_NAME)
    await connection.execute(f"TRUNCATE TABLE {TABLE_SCHEMA}.{TABLE_NAME} RESTART IDENTITY")
    logger.info("Writing %s rows back with asyncpg COPY", len(records))
    await connection.copy_records_to_table(
        TABLE_NAME,
        schema_name=TABLE_SCHEMA,
        records=iter(records),
        columns=restore_columns,
    )
    logger.info("Bulk replace complete")


async def fetch_column_info(connection: asyncpg.Connection) -> dict[str, ColumnInfo]:
    rows = await connection.fetch(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = $1
          AND table_name = $2
        ORDER BY ordinal_position
        """,
        TABLE_SCHEMA,
        TABLE_NAME,
    )
    return {
        row["column_name"]: ColumnInfo(
            name=row["column_name"],
            data_type=row["data_type"],
            is_nullable=row["is_nullable"] == "YES",
        )
        for row in rows
    }


async def fetch_existing_rows(connection: asyncpg.Connection, tickers: list[str]) -> dict[str, asyncpg.Record]:
    if not tickers:
        return {}
    rows = await connection.fetch(
        f"SELECT * FROM {TABLE_SCHEMA}.{TABLE_NAME} WHERE ticker = ANY($1::text[])",
        tickers,
    )
    return {row["ticker"]: row for row in rows}


async def apply_restore(
    connection: asyncpg.Connection,
    csv_rows: list[dict[str, str]],
    csv_columns: list[str],
    *,
    dry_run: bool,
    progress_interval: int,
) -> RestoreSummary:
    summary = RestoreSummary(total_rows_processed=len(csv_rows))
    column_info = await fetch_column_info(connection)
    known_columns = set(column_info)
    restore_columns = [
        column
        for column in csv_columns
        if column not in EXCLUDED_COLUMNS and column in known_columns
    ]
    unknown_columns = [
        column
        for column in csv_columns
        if column not in EXCLUDED_COLUMNS and column not in known_columns
    ]
    for column in unknown_columns:
        summary.validation_errors.append(f"CSV column '{column}' does not exist in {TABLE_SCHEMA}.{TABLE_NAME} and was ignored.")

    logger.info(
        "Loaded CSV snapshot rows=%s columns=%s restore_columns=%s dry_run=%s",
        len(csv_rows),
        len(csv_columns),
        len(restore_columns),
        dry_run,
    )
    if unknown_columns:
        logger.info("Ignored %s CSV columns that do not exist in the target table", len(unknown_columns))

    row_states: list[dict[str, Any]] = []
    seen: set[str] = set()
    logger.info("Validating tickers and detecting duplicates")
    for index, row in enumerate(csv_rows, start=2):
        ticker = normalize_ticker(row.get("ticker"))
        if ticker is None:
            summary.validation_errors.append(f"Row {index}: missing ticker")
            row_states.append({"index": index, "ticker": None, "valid": False, "reason": "missing"})
            continue
        if ticker in seen:
            summary.validation_errors.append(f"Row {index}: duplicate ticker '{ticker}'")
            row_states.append({"index": index, "ticker": ticker, "valid": False, "reason": "duplicate"})
            continue
        seen.add(ticker)
        row_states.append({"index": index, "ticker": ticker, "valid": True, "reason": None})
        if progress_interval > 0 and len(row_states) % progress_interval == 0:
            logger.info("Validated %s/%s CSV rows", len(row_states), len(csv_rows))

    tickers = [state["ticker"] for state in row_states if state["valid"] and state["ticker"] is not None]
    logger.info("Fetching %s existing Neon rows for restore comparison", len(tickers))
    existing_rows = await fetch_existing_rows(connection, tickers)
    logger.info("Fetched %s matching Neon rows", len(existing_rows))

    logger.info("Beginning row comparison and restore pass")
    for position, (state, row) in enumerate(zip(row_states, csv_rows), start=1):
        index = state["index"]
        ticker = state["ticker"]
        if not state["valid"] or ticker is None:
            summary.rows_skipped += 1
            continue

        existing = existing_rows.get(ticker)
        if existing is None:
            summary.rows_not_found += 1
            continue

        row_errors: list[str] = []
        changed_columns: list[str] = []
        changed_values: list[Any] = []

        for column_name in restore_columns:
            column = column_info[column_name]
            new_value, error = coerce_value(row.get(column_name), column)
            if error:
                row_errors.append(f"Row {index} ({ticker}): {error}")
                continue
            existing_value = existing[column_name]
            if values_differ(existing_value, new_value, column.data_type):
                changed_columns.append(column_name)
                changed_values.append(new_value)

        if row_errors:
            summary.validation_errors.extend(row_errors)
            summary.rows_skipped += 1
            continue

        if not changed_columns:
            summary.rows_skipped += 1
            continue

        if not dry_run:
            assignments = ", ".join(f"{column} = ${idx}" for idx, column in enumerate(changed_columns, start=2))
            sql = f"UPDATE {TABLE_SCHEMA}.{TABLE_NAME} SET {assignments} WHERE ticker = $1"
            await connection.execute(sql, ticker, *changed_values)
        summary.rows_updated += 1

        if progress_interval > 0 and position % progress_interval == 0:
            logger.info(
                "Processed %s/%s rows | updated=%s skipped=%s not_found=%s validation_errors=%s",
                position,
                len(csv_rows),
                summary.rows_updated,
                summary.rows_skipped,
                summary.rows_not_found,
                len(summary.validation_errors),
            )

    return summary


def print_summary(summary: RestoreSummary, *, dry_run: bool, csv_path: Path) -> None:
    mode = "DRY RUN" if dry_run else "RESTORE COMPLETE"
    print(mode)
    print(f"CSV path: {csv_path}")
    print(f"Total rows processed: {summary.total_rows_processed}")
    print(f"Rows updated: {summary.rows_updated}")
    print(f"Rows skipped: {summary.rows_skipped}")
    print(f"Rows not found: {summary.rows_not_found}")
    print(f"Validation errors: {len(summary.validation_errors)}")
    if summary.validation_errors:
        print("Validation error details:")
        for error in summary.validation_errors:
            print(f" - {error}")


async def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
        force=True,
    )

    logger.info("Starting restore utility")
    logger.info("CSV path: %s", args.csv_path)
    logger.info("Dry run: %s", args.dry_run)
    logger.info("Progress interval: %s", args.progress_interval)

    logger.info("Resolving database connection string")
    database_url = load_database_url(args.database_url, args.env_file)

    logger.info("Loading CSV snapshot into memory")
    csv_rows, csv_columns = load_csv_rows(args.csv_path)
    logger.info("CSV snapshot loaded successfully")

    connection = await asyncpg.connect(dsn=database_url)
    transaction = connection.transaction()
    try:
        logger.info("Opening database transaction")
        await transaction.start()
        try:
            column_info = await fetch_column_info(connection)
            restore_columns, records, summary = build_bulk_restore_records(
                csv_rows,
                csv_columns,
                column_info,
                progress_interval=args.progress_interval,
            )
            if args.dry_run:
                logger.info("Rolling back dry run transaction")
                await transaction.rollback()
                print_summary(summary, dry_run=True, csv_path=args.csv_path)
                return 0 if not summary.validation_errors else 1
            logger.info("Starting bulk restore for %s validated rows", len(records))
            await bulk_replace_table(
                connection,
                restore_columns=restore_columns,
                records=records,
                progress_interval=args.progress_interval,
            )
            logger.info("Committing restore transaction")
            await transaction.commit()
            print_summary(summary, dry_run=False, csv_path=args.csv_path)
            return 0 if not summary.validation_errors else 1
        except Exception:
            logger.exception("Restore failed, rolling back transaction")
            await transaction.rollback()
            raise
    except Exception as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await connection.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
