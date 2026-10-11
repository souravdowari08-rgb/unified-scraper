# Unified scraper service

One web service replacing `first_scraper`, `driveseed_scraper` and `gdrive-link`.
All original endpoint paths are unchanged, so clients only need the new base URL.

| Endpoint | Was | Needs browser |
|---|---|---|
| `GET /scrape?url=` | first_scraper | no |
| `GET /getlink?url=` | driveseed_scraper | yes |
| `GET/POST /api/link` (`url`) | gdrive-link | yes |
| `POST /admin/session` | gdrive-link | no |
| `GET /health`, `/ping`, `/` | all | - |

Both browser services share **one** Chromium (async Playwright 1.49.1).

## Deploy (Back4app / any Docker host)
1. Push this folder to the root of a **private** GitHub repo.
2. New container app, port `8080`, health check path `/health`.
3. Give it at least ~1 GB RAM (Chromium).
4. Set env vars (below). At minimum: `API_KEY`.

## Driveseed login session (for /api/link)
Pick ONE:
- `STATE_JSON_B64` = base64 of your `state.json`, and `USER_AGENT` = value from `meta.json`
- mount a volume at `SESSION_DIR` containing `state.json` + `meta.json`
- after deploy: `POST /admin/session` with header `X-API-Key` and body
  `{"state": <contents of state.json>, "user_agent": "<from meta.json>"}`

When the session expires, `/api/link` returns `503 session_invalid`; log in again and re-post it.
**state.json / cookies.json are logins, never commit them to a public repo.**

## Environment variables
| Var | Default | Notes |
|---|---|---|
| `PORT` | 8080 | |
| `API_KEY` | - | header `X-API-Key`. Protects /getlink, /api/link, /admin/session (the last two *require* it) |
| `SCRAPE_REQUIRES_KEY` | 0 | `1` = also protect /scrape |
| `CORS_ORIGIN` | * | set to your site |
| `MAX_CONCURRENT` | 2 | simultaneous browser jobs across /getlink + /api/link. Use 1 on small RAM |
| `SCRAPE_ALLOWED_HOSTS` | any public | comma list |
| `GETLINK_ALLOWED_HOSTS` | any public | comma list (was `ALLOWED_HOSTS`) |
| `DRIVESEED_ALLOWED_HOSTS` | driveseed.org | comma list (was `ALLOWED_HOSTS`) |
| `PROXY_URL` | - | proxy for /getlink only |
| `REQUEST_TIMEOUT` | 150 | /getlink total seconds |
| `CACHE_TTL` | 900 | /getlink result cache (0=off) |
| `DRIVESEED_CACHE_TTL` | 600 | /api/link result cache (0=off) |
| `QUEUE_WAIT` | 60 | seconds /api/link waits for a free browser slot |
| `VARIANTS`, `BLOCK_THIRD_PARTY`, `TIME_SPEEDUP`, `DEBUG_DIR` | as before | /getlink tuning |
| `SESSION_DIR` | /tmp/driveseed | use a volume to keep the session across restarts |

## Local run
    pip install -r requirements.txt && playwright install chromium
    API_KEY=test hypercorn app:app --bind 0.0.0.0:8080 --workers 1
Keep `--workers 1`: more workers means more Chromium instances and separate caches.
