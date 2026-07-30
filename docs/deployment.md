# Deployment Instructions

## Required Environment Variables

- `DATABASE_URL`
- Optional: `LOG_LEVEL`
- Optional: `CATALOG_PATH`
- Optional: `REFRESH_CONCURRENCY`
- Optional: `REFRESH_TIMEOUT_SECONDS`
- Optional: `REFRESH_MAX_ATTEMPTS`
- Optional: `RETRY_BASE_DELAY_SECONDS`
- Optional: `DB_POOL_MIN_SIZE`
- Optional: `DB_POOL_MAX_SIZE`
- Optional: `DB_COMMAND_TIMEOUT_SECONDS`
- Optional: `STARTUP_VALIDATION_TICKER`

## Required Ports

- Container port: `8000`
- Health check path: `/health`

## Required Volumes

- None required for runtime
- The bundled catalog is read from the image at `data/ies_catalog.json`

## Docker Build

Build with the repository `Dockerfile`:

```bash
docker build -t ies-metadata-refresh .
```

## Coolify Settings

- Build type: Dockerfile
- Exposed port: `8000`
- Health check path: `/health`
- Runtime environment: set `DATABASE_URL`
- Prefer the pooled Neon URL for runtime use
- Leave the container start command as the Dockerfile default

## Neon Settings

- `DATABASE_URL` should point at Neon PostgreSQL
- Allow the service to create `public.ies_company_metadata`
- Allow the service to create `public.ies_refresh_log`
- Ensure the database user has permission to create tables and indexes

## n8n Settings

- Schedule Trigger runs the workflow on the required cadence
- HTTP Request node must `POST` to `${IES_METADATA_REFRESH_URL}/refresh`
- Response handling must treat success as `status == "success"` and `failed == 0`
- Set the HTTP Request timeout longer than the expected refresh window

## Required Credentials

- None in n8n for the refresh workflow
- The service itself only needs the Neon connection string

## Expected Responses

- `GET /health` -> `200` and `{"status":"ok"}`
- `POST /refresh` success -> `200` and `{"status":"success", ...}`
- `POST /refresh` failure -> `500` or `503` and `{"status":"failed", ...}`

## Expected Logs

- Startup validation status
- Refresh start with run ID and company count
- Per-ticker fetch warnings on individual failures
- Refresh completion summary with counts and duration
- Failure stack traces only when the run crashes

## Expected First Startup Behaviour

- Connect to Neon
- Create missing tables
- Create missing indexes
- Run startup validation against the configured ticker
- Keep the API live even if validation fails, then re-check on the first refresh request
