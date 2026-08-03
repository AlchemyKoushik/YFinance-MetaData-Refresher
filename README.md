# IES Metadata Refresh Service

Independent FastAPI service that reads the bundled IES catalog, fetches lightweight company metadata through the official `yfinance` Python library, and upserts the results into Neon PostgreSQL.

## What it does

- Loads the bundled `data/ies_catalog.json` file locally.
- Pulls only the lightweight fields needed for the metadata table.
- Updates fields selectively so existing valid data is never replaced by null or empty Yahoo values.
- Uses a refresh lock so only one run can execute at a time.
- Records each refresh run in `public.ies_refresh_log` with counts, warnings, and field-change reasons.
- Automatically normalizes company `region` values from `country` after a successful refresh.
- Exposes `GET /health` and `POST /refresh`.

## Project Layout

- `app/main.py` - FastAPI entrypoint
- `app/catalog.py` - bundled catalog loader
- `app/database.py` - asyncpg pool, schema creation, and upserts
- `app/refresh.py` - refresh orchestration and validation
- `app/yfinance_client.py` - yfinance fetch logic with retries and timeouts
- `data/ies_catalog.json` - bundled IES catalog copy
- `n8n/ies_metadata_refresh_workflow.json` - import-ready workflow export

## Required Environment Variables

Only one variable is required:

- `DATABASE_URL` - Neon PostgreSQL connection string

Optional tuning variables are documented in `.env.example`.

## Local Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker compose up --build
```

## Database Schema

The service creates or preserves these tables automatically if they do not exist:

- `public.ies_company_metadata`
- `public.ies_refresh_log`

The current live Neon schema expects:

- `public.ies_company_metadata` with a surrogate `id` primary key, unique `ticker`, and non-null discovery fields
- `public.ies_refresh_log` for refresh run history and status tracking

Recommended supporting indexes:

- `public.ies_company_metadata(last_updated DESC)`
- `public.ies_refresh_log(started_at DESC)`
- `public.ies_refresh_log(status)`

## Health Check

- Endpoint: `GET /health`
- Response: `{"status":"ok"}`

For Coolify or Docker health checks, use:

- Path: `/health`
- Port: `8000`

## Coolify Deployment

1. Create a new service from this repository.
2. Set the build type to Dockerfile.
3. Add `DATABASE_URL` in Coolify environment variables.
4. Expose port `8000`.
5. Configure the health check path as `/health`.
6. Keep the container running with the default command from the Dockerfile.

If your Neon project provides both pooled and direct URLs, prefer the pooled URL for runtime use.

## How n8n Should Call It

Use a scheduled workflow that sends a `POST` request to `/refresh`.

Recommended n8n handling:

1. Cron or Schedule Trigger.
2. HTTP Request node with `POST` to the service `/refresh` endpoint.
3. Parse the JSON response.
4. Treat the run as successful only when `status === "success"` and `failed === 0`.
5. Log the response summary or route it to your own notification step.
6. Use a response timeout that is comfortably longer than the service's own refresh timeout and the n8n execution window.

The `/refresh` response includes IST-formatted timestamps for `started_at_ist` and `finished_at_ist`.

## Region Backfill Utility

Use this when you want to repair historical data or rerun region normalization without a full metadata refresh.

Examples:

```bash
python scripts/backfill_regions.py
python scripts/backfill_regions.py --dry-run
python scripts/backfill_regions.py --country India
python scripts/backfill_regions.py --country India --country Germany --dry-run
```

Notes:
- The command uses the same country-to-region mapping as the refresh service.
- By default it scans and updates the entire `ies_company_metadata` table.
- `--dry-run` scans and reports changes without writing rows.
- `--country` scopes the run to one or more countries.

## Notes

- The refresh is concurrent, retry-aware, and continues if individual tickers fail.
- After a successful refresh, the service automatically normalizes `region` from `country` and only updates rows whose region is wrong.
- If another refresh is already in progress, the service returns HTTP 409 with `{"status":"already_running","message":"A metadata refresh is already in progress."}`.
- User-facing timestamps are exposed in IST (`Asia/Kolkata`).
- The service does not scrape Yahoo and does not use the OSINT backend.
- The bundled catalog is read locally from disk.

## Integration Checklist

Use this when you deploy or validate the final integration:

- [ ] Refresh Service starts
- [ ] `GET /health` returns `{"status":"ok"}`
- [ ] Neon tables exist
- [ ] Neon indexes exist
- [ ] `POST /refresh` succeeds
- [ ] Rows are inserted or updated
- [ ] Refresh log is written
- [ ] n8n workflow imports cleanly
- [ ] n8n workflow executes cleanly
- [ ] n8n receives a success response
- [ ] No companies are marked failed on a clean run
- [ ] Launch Analysis reads Neon
- [ ] Top N filtering still works
- [ ] Deep enrichment still works
- [ ] Frontend contract is unchanged
