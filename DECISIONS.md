# Design Decisions

This file records the decisions made while building the Fuel Route Planner: choices the
brief left open, features added beyond the brief, and the trade-offs measured along the way.
For setup and usage, see [README.md](README.md).

## What the brief asked for

- An API that takes a start and finish location in the USA.
- A map of the route and cost-effective fuel stops along it.
- 500-mile range, 10 miles per gallon, total fuel cost.
- Use the provided fuel price CSV and a free routing API (ideally one call).
- Latest stable Django, fast responses.

Everything below is either **how** that was done (where the brief left a choice) or an
**add-on** beyond it.

---

## 1. Core design choices

| # | Area | Decision | Why |
|---|---|---|---|
| 1.1 | Routing API | OSRM public server, no API key, **one call per unique trip** | Free, no signup, returns route geometry and distance in one call |
| 1.2 | Geocoding | Offline US cities table (`data/us-cities.csv`), **zero geocoding API calls** | The CSV addresses (`I-44, EXIT 283 & US-69`) can't be geocoded reliably, and an offline table keeps the API to one external call |
| 1.3 | Cities data source | [kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database) (MIT), slimmed to `city, state, lat, lon`, one row per city/state | Free, permissive license, 29,738 cities. License shipped in `data/us-cities.LICENSE` |
| 1.4 | Input format | `"City, ST"` or `"City, State Name"`. Unknown city is an error | Simple, unambiguous; US-only is enforced by the table |
| 1.5 | Database | PostgreSQL 17 + PostGIS 3.5 via Docker Compose | Spatial queries (`ST_DWithin`) in the database |
| 1.6 | Location field | `PointField(geography=True, srid=4326)` | Geography distances are in meters on the earth's surface |
| 1.7 | Corridor | Stops within **10 miles** of the route, via `ST_DWithin` | Fast indexed filter: about 440 of 6,614 stops for a coast-to-coast trip, in under 0.1 s |
| 1.8 | Mile markers | `route.project_normalized(point) × total_miles` in Python (GEOS), on corridor stops only | Cheap, and accurate enough to order stops and size purchases |
| 1.9 | Route detail | `overview=simplified` | Small response and map line. Measured cost: about 2% of corridor stops missed (441 vs 449 on New York → Los Angeles), max 5.7-mile deviation from the real road |
| 1.10 | Starting fuel | Empty tank. First stop is the cheapest station in the first 50 miles; it also pays for the fuel used to reach it | Makes the total cost cover the whole trip |
| 1.11 | Greedy rule | At each stop, look 500 miles ahead. If a cheaper stop exists, buy just enough to reach the **first** one. Otherwise, if the finish is in range, buy just enough to finish; else fill up and go to the **cheapest** stop in range | Classic fuel-stop greedy. Matched the theoretical minimum cost on all 8 test routes |
| 1.12 | Invariant | Total gallons = total miles / 10 | Checked in code (assert) and in tests |
| 1.13 | Map | API returns GeoJSON; a separate HTML page draws it with Leaflet | Postman gets JSON; humans get a map link |
| 1.14 | Coordinate order | lon, lat everywhere (OSRM, GeoJSON, PostGIS); flipped to lat, lon **only** in Leaflet | One rule, one place to flip |

## 2. Add-ons beyond the brief

### 2.1 Three fuel strategies

The brief asks for "cost-effective" stops. The greedy rule gives the lowest cost but can use
many stops with tiny purchases (for example 0.11 gallons at Texarkana, then a cheaper stop
1.1 miles later). We measured the trade-off and kept all three options:

| Strategy | Rule | Stops (8 trips) | Cost (8 trips) | vs lowest |
|---|---|---|---|---|
| `lowest_cost` (default) | Greedy rule above | 79 | $4,088.41 | — |
| `balanced` | A stop only counts as cheaper if it saves **≥ 5¢/gal** | 57 (−28%) | $4,101.52 | +0.32% |
| `fewest_stops` | Never chase a cheaper stop: fill up, go to the cheapest in range | 48 (−39%) | $4,165.21 | +1.88% |

Routes measured: New York → Los Angeles, Chicago → Houston, Seattle → Miami,
Boston → Denver, Atlanta → Phoenix, Dallas → Minneapolis, San Francisco → Salt Lake City,
Philadelphia → Nashville.

Margins also measured: 2¢ (−13% stops, +0.04%) and 10¢ (−34% stops, +0.42%). 5¢ was
chosen as the best balance.

- All three share one engine, `plan_fuel_stops(min_saving=..., chase_cheaper=...)`.
- `STRATEGIES` maps names to functions, each with a one-line docstring describing its
  trade-off, so an agent can choose a strategy by name. The AI strategy picker (section 6)
  now uses these docstrings in its prompt.

### 2.2 One endpoint with a `strategy` field

`POST /api/route/` takes an optional `"strategy"`, defaulting to `lowest_cost`. Chosen over three
separate endpoints because it is one tool with three choices for an agent, and one URL for
clients.

### 2.3 Caching split: route cached, optimizer per request

The original plan cached the full result by `start|finish`. With strategies, the result
depends on the strategy too, so:

- The **route and corridor stops** are cached by normalized `start|finish` (24 hours).
- The **optimizer** runs on every request (under 1 ms).

So OSRM is called once per trip, even when comparing all three strategies, using different
spellings (`"st. louis, mo"`, `"Saint Louis, Missouri"`), or opening the map. The cache now
lives in Redis (see section 5).

### 2.4 Data load observability report

`load_stops` writes `reports/load_stops.json` on every run: counts, match percentages, and
every skipped station. Target: under 5% unmatched.

| Measure | Value |
|---|---|
| Unique OPIS IDs | 6,738 |
| Loaded | 6,614 |
| Unmatched, all | 124 (1.84%) |
| Unmatched, non-US (Canada) | 112 |
| Unmatched, US | 12 of 6,626 (0.18%) |

### 2.5 Structured error responses

Every error, including DRF's own (bad JSON, wrong method), uses one shape:

```json
{"error": {"code": "range_gap", "message": "...", "details": {"from_mile": 259.3}}}
```

- `code` is stable and machine-readable (an agent can react to `range_gap`).
- Status codes distinguish causes: timeout **504**, routing service down **502**, no
  drivable road **422** (not 502), no station / gap over 500 miles **422**, bad input **400**.
- Each layer raises a plain Python error; only `views.py` maps it to HTTP. The optimizer
  stays free of Django and HTTP.
- Raw routing errors are logged, never sent to the client. Failures are never cached.

### 2.6 Test suite beyond the required three

The brief's plan asked for three optimizer tests. The suite has 99: every rule of all three
strategies, the gallons invariant on 900 random plans, location parsing, OSRM handling
(network faked), the PostGIS corridor, every API error, Redis caching behavior, the AI
strategy picker (LLM faked) and rate limits. Mutation-checked: breaking the
5¢ rule or the first-cheaper rule makes tests fail.

## 3. Implementation calls

| # | Decision | Why |
|---|---|---|
| 3.1 | Duplicate OPIS IDs: keep the row with the **lowest** price | Brief's CSV has 8,151 rows for 6,738 stations |
| 3.2 | Prices rounded to 3 decimals on load (`3.00733333` → `3.007`) | Fits the model field; matches pump pricing |
| 3.3 | `load_stops` deletes all stops and reloads in one transaction | Re-running is safe and gives the same result |
| 3.4 | Fixture `fuel/fixtures/truckstops.json` committed | Reviewers load clean data in one command |
| 3.5 | First-stop search widens **50 miles at a time** up to 500 miles (or the trip length) | Keeps the first stop as close to the start as possible |
| 3.6 | Ties for cheapest stop in range: pick the **farther** one | Fewer stops for the same price |
| 3.7 | Cities are validated **before** the routing call | A typo never uses up an OSRM call |
| 3.8 | HTTP client: Python's built-in `urllib` | No extra dependency |
| 3.9 | OSRM "no route" comes back as **HTTP 400** with code `NoRoute`; read it as `no_route` | Found during testing: it was being reported as "service unavailable" |
| 3.10 | Cache backend: Redis (originally Django's in-memory cache) | Shared across workers and restarts; see section 5 |

## 4. Findings from the data

- **Few California stations.** The CSV has only 8 in California, so some West Coast trips
  are infeasible (for example Seattle → San Diego returns `range_gap`), and New York →
  Los Angeles has a 216-mile final leg with no stations.
- **"Saint / Fort / Mount" are spelled out** in both the fuel CSV and the cities table. The
  `St/Ft/Mt` rules matter for user input (`"St. Louis, MO"`), not for loading.
- **Unmatched US towns** (6): Evergreen AL, Port Wentworth GA, University Park IL,
  Elizabethport NJ, Brookpark OH, Henrico VA.

## 5. Redis caching

An infrastructure change only: the API, optimizer, strategies and response shape are
unchanged.

| # | Area | Decision | Why |
|---|---|---|---|
| 5.1 | Backend | Django's built-in `RedisCache` with the `redis` package (8.0.1). No third-party Django cache package | Supported by Django itself; one less dependency to maintain |
| 5.2 | Config | `REDIS_URL` from `.env`. Unset → in-memory cache. Added to `.env.example` | Same code runs with or without Redis |
| 5.3 | Docker | `redis:7.4-alpine` service, `--maxmemory 256mb --maxmemory-policy allkeys-lfu`, health-checked; `web` waits for it | Official image; bounded memory |
| 5.4 | Eviction | LFU via Redis config, no manual hot-key logic | Popular routes stay cached; Redis does the work |
| 5.5 | Key versioning | `KEY_PREFIX = "fuelroute"`, `VERSION = 1` in `CACHES`. Keys look like `fuelroute:1:route:denver\|CO\|dallas\|TX` | Bumping `VERSION` invalidates every old key at once |
| 5.6 | What is cached | Plain data only: route coordinates (lists), miles (float), stops (dicts of str/int/float) | Safe to serialize; no GEOS or model objects. The existing code already met this |
| 5.7 | TTL | 24 hours for routes | Roads and prices change slowly |
| 5.8 | Stampede protection | Lock `route key + ":lock"` via `cache.add(..., timeout=30)`. Winner computes, caches, releases in `finally`. Others poll every 0.2 s for up to 20 s, then compute themselves | One OSRM call per trip even under load; never fail because of the lock. Measured: 8 parallel requests → 1 MISS, 7 HIT |
| 5.9 | Redis down | Safe helpers catch Redis errors, log a warning, and act as a miss / no-op | The API keeps working. Measured: Redis stopped → 200 in ~0.9 s; Redis frozen → 200 in ~3.6 s |
| 5.10 | Failures | Never cached (timeouts, `no_route`, etc.); lock always released | No negative caching. For `range_gap` the valid route is cached and the error recomputed (under 1 ms) |
| 5.11 | Observability | `X-Cache: HIT` or `MISS` on `POST /api/route/` (set once the route lookup has run) | See cache behavior from Postman or curl. A request that waited for another's result is a HIT |
| 5.12 | Tests | Always in-memory cache under `manage.py test` | No Redis needed, and `cache.clear()` in tests can never flush a real Redis |
| 5.13 | Scope | No negative caching, no pre-warming, no caching of database queries | Keep the change small |

### Gaps found in the plan and fixed

| Gap | Risk | Fix |
|---|---|---|
| No Redis socket timeouts | With Redis frozen, every cache call could hang for a long time | `socket_connect_timeout` and `socket_timeout` = 1 s |
| Lock + Redis down | A failed `cache.add` looked like "lock held", so requests would poll for 20 s | Lock helper returns acquired / held / unavailable; unavailable → compute at once |
| Waiters after a failed winner | Failures aren't cached, so waiters would poll the full 20 s | Waiters also check the lock; lock gone and no route → compute at once |
| Tests inside Docker see `REDIS_URL` | Tests would hit real Redis, and `cache.clear()` would flush it | Force the in-memory cache when running `manage.py test` |

## 6. Phase 7: AI strategy picker and web page

Two add-ons beyond the brief: an endpoint where an LLM chooses the fuel strategy from the
user's own words, and a web page for trying the API in a browser. `/api/route/` and the
optimizer are unchanged; the only change to the existing response is two extra fields per stop
(`lon`, `lat`; row 6.14).

### 6.1 Workflow, not a free-running agent

| Option | Verdict |
|---|---|
| Full agent (LLM loops and picks tools) | Not needed: the steps never change (route → plans → pick one). Only **which strategy** is decided at runtime |
| **Workflow with one LLM decision (a "router")** | **Chosen.** One LLM call, predictable, fast, cheap |
| LangGraph | A library, not a server. Works, but heavy for one decision |
| Separate microservice | Second container and a network hop for no benefit: same team, same language, same scaling |

The router lives inside Django (`fuel/router.py`) and calls the planner functions directly.

### 6.2 Flow

```
POST /api/route/smart/ {start, finish, preference?}
1. Validate input (preference ≤ 300 chars)          → 400 on bad input
2. get_trip (Redis cache / 1 OSRM call)              → X-Cache header
3. Run all 3 strategies                              → infeasible: existing 422, no LLM call
4. Code builds the comparison (stops, cost, extra $ and % vs lowest_cost)
5. Empty preference → default | identical plans → identical | no key → fallback
6. Otherwise ONE LLM call (5 s timeout, choice limited to the 3 names)
7. Invalid or failed answer → lowest_cost + fallback_reason
8. Response = the /api/route/ body for that strategy + decision + comparison
```

### 6.3 Decisions

| # | Area | Decision | Why |
|---|---|---|---|
| 6.1 | Name | Called an "AI strategy picker" (a router), not an autonomous agent | Accurate: it makes one decision inside fixed steps |
| 6.2 | Endpoint | New `POST /api/route/smart/`; `/api/route/` untouched | Existing clients and tests unaffected |
| 6.3 | Input | `start`, `finish` (validated as before) + optional `preference`, max 300 characters | The LLM never extracts cities, so input validation stays exact. The length cap limits cost and prompt abuse |
| 6.4 | Who does the math | Code computes all three plans, stop counts, costs and differences; the LLM only picks and explains | LLMs are unreliable at arithmetic; the numbers in the response are always exact |
| 6.5 | Provider | Anthropic Python SDK 1.13.0, model `claude-haiku-5-5` from `STRATEGY_ROUTER_MODEL`. **Changed in 7.1:** OpenRouter is now the default; Anthropic stays as an option | Fast and cheap enough for picking 1 of 3; the model can be changed without code changes |
| 6.6 | Output format | Forced tool call (`tool_choice`) with `strategy` as an enum of the three names | The model must return a structured answer; free text is never parsed |
| 6.7 | Validation | Strategy must be one of the three and the reason non-empty; reason cut to 200 characters | Anything else is treated as an invalid answer |
| 6.8 | Fallback | Any problem → `lowest_cost`, HTTP 200, `decided_by: "fallback"` + `fallback_reason` (`no_api_key`, `timeout`, `llm_error`, `invalid_response`), logged as a warning | The endpoint works for a reviewer with no key, and the reason is visible |
| 6.9 | When the LLM is skipped | Empty preference (`default`), all plans identical (`identical`), infeasible trip (existing error), no key (`fallback`) | Saves time and money when the choice can't change anything |
| 6.10 | Timeout | 5 s, no retries | The brief asks for speed; a slow LLM must not double the response time |
| 6.11 | Prompt injection | Preference is wrapped in `<preference>` tags; the system prompt says to treat it only as a preference; output limited to the enum + a short reason | The worst a malicious preference can do is pick one of three valid plans |
| 6.12 | Caching | The route cache and `X-Cache` are shared with `/api/route/`. LLM decisions are **not** cached | Preference text varies too much to hit; matches the "no extra caching" rule of section 5 |
| 6.13 | Tests | The LLM is faked in every test; `ANTHROPIC_API_KEY` is forced empty under `manage.py test` | Tests never call a paid API, even inside Docker with a real key in `.env` (same idea as 5.12) |
| 6.14 | Stop coordinates | `fuel_stops[]` in the API response gains `lon` and `lat` | The web page needs them to draw stops; additive, so existing clients are unaffected |
| 6.15 | Web page | `GET /` serves `fuel/templates/fuel/index.html`: one HTML file, no build step, calls the public JSON API with `fetch` | Reviewers can try the API in a browser; it exercises the same endpoints as Postman |
| 6.16 | CSRF | `DEFAULT_AUTHENTICATION_CLASSES = []` in DRF settings | The API is public with no logins. Without this, a browser logged in to `/admin/` would get 403 when posting from the page |

### 6.4 Changes made to the first version of the web page

| Problem in the first version | Fix |
|---|---|
| Read `data.reason` / `data.decided_by`, but the API returns them inside `decision` | Reads `decision.strategy / reason / decided_by / fallback_reason` and explains each `decided_by` |
| Stop markers never appeared: the API had no stop coordinates | API now returns `lon` / `lat` per stop (6.14) |
| Comparison table built from whatever keys came back | Fixed columns: strategy, stops, total cost, extra vs `lowest_cost`, description; chosen row highlighted |
| Map popups built from HTML strings with station names (HTML injection) | Popups built with DOM nodes and `textContent` |
| Old OpenStreetMap tile URL (`{s}.tile...`) | `tile.openstreetmap.org` as OSM now recommends (also on the map page) |
| Leaflet from a CDN without integrity check | Subresource Integrity hashes added (verified against the files; also on the map page) |
| `invalid_input` errors showed only "Invalid request." | Error box lists the details (field and message) |
| Page crashed if Leaflet failed to load | Results still render; the map area shows a message |
| Nothing for API testers to copy | Status line (HTTP code, time, `X-Cache`), example trips, and a "Raw API request and response" panel with the equivalent `curl` |

### 6.5 Verified

| Check | Result |
|---|---|
| Test suite | 89 tests pass (71 existing + 18 new). Breaking the strategy validation or the identical-plan check makes tests fail |
| Chicago → Houston, preference given, no key | 200, `lowest_cost`, `fallback` / `no_api_key`, comparison 8 / 5 / 4 stops, $323.35 / $324.88 (+0.47%) / $329.74 (+1.98%) |
| Same trip, no preference | `default`, `X-Cache: HIT`, 3 ms |
| Dallas → Austin | `identical` (all three: 1 stop, $62.48), no LLM call |
| Seattle → San Diego | 422 `range_gap`, no LLM call |
| Preference of 301 characters | 400 `invalid_input` |
| Real SDK call with an invalid key | Anthropic returns 401 → `fallback` / `llm_error` in 0.43 s, warning logged |
| Web page in headless Chrome | Manual and AI modes, map with route and stop markers, comparison, errors (`range_gap`, `unknown_city`), no horizontal scroll at 375 px |

A successful LLM answer was not tested live: no API key was available. The request sent
to the SDK is checked by tests (6.13).

## 7. Phase 8: OpenRouter, rate limits and AWS deployment

Goal: deploy publicly on AWS without anyone being able to run up the LLM bill, and use
existing OpenRouter credits.

### 7.1 OpenRouter as the AI provider

| # | Decision | Why |
|---|---|---|
| 7.1 | `STRATEGY_ROUTER_PROVIDER=openrouter` (default) or `anthropic` | Uses existing OpenRouter credits; Anthropic direct still works with one setting |
| 7.2 | Model **`anthropic/claude-haiku-5.5`** | Chosen from OpenRouter's live model list (checked 2026-10-11): supports `tools` and `tool_choice`, $0.10 / $0.50 per million input/output tokens. About 500 tokens in and 60 out per decision, so **about $0.0001 per call** (around 12,000 per $1) |
| 7.3 | Called with Python's `urllib` (OpenAI-style `/chat/completions`), no new SDK | Same approach as the OSRM call (3.8); one less dependency |
| 7.4 | Same forced tool call: `tool_choice` names `choose_strategy`, `strategy` is an enum of the 3 names | Same guarantees as with Anthropic (6.6) |
| 7.5 | `provider: {"require_parameters": true}` in the request | OpenRouter only routes to providers that support forced tool calls |
| 7.6 | Timeout → `timeout`; HTTP errors (401 wrong key, 402 no credits or key limit reached, 429) and network errors → `llm_error`, logged with the status; unreadable or tool-less replies → `invalid_response` | Every failure still falls back to `lowest_cost` with HTTP 200 (6.8) |
| 7.7 | A separate OpenRouter key per deployment, with a **credit limit** (e.g. $2) | If the key leaks, it can't spend more than the limit |
| 7.8 | Keys are never set under `manage.py test` (both providers) | Tests can't spend credits (same idea as 5.12, 6.13) |

### 7.2 Rate limits

| # | Decision | Why |
|---|---|---|
| 7.9 | DRF scoped throttling per client IP: `smart` **20/hour**, `route` **120/hour**, set by `SMART_RATE_LIMIT` / `ROUTE_RATE_LIMIT` | `smart` can call a paid LLM; `route` protects the free public OSRM server from abuse through our app. The map page is not limited (it is HTML for people, not an API) |
| 7.10 | Over the limit: `429` with code `throttled`, `details.retry_after_seconds` and a `Retry-After` header | Same error shape as everything else (2.5) |
| 7.11 | Counters live in the default cache (Redis), shared by all Gunicorn workers | One limit per IP, not one per worker |
| 7.12 | `SafeScopedRateThrottle`: if Redis is down, the request is **allowed** and a warning logged | Same rule as caching (5.9): Redis trouble never breaks the API |
| 7.13 | `NUM_PROXIES=1` only in `docker-compose.prod.yml` | Behind Caddy every request comes from Caddy's IP; with this setting DRF uses the client IP Caddy adds to `X-Forwarded-For`. Not set in development, where the header could be faked |

### 7.3 Production setup

| # | Decision | Why |
|---|---|---|
| 7.14 | One EC2 server (Ubuntu 24.04 x86, t3.small) running Docker Compose | Simplest production-shaped setup. ECS + RDS + ElastiCache is the at-scale path, noted in DEPLOY.md |
| 7.15 | Separate `docker-compose.prod.yml`; development `docker-compose.yml` unchanged | Dev keeps hot reload; production has no code mount |
| 7.16 | **Caddy 2.11** reverse proxy, domain from `DOMAIN` in `.env` | Automatic HTTPS (Let's Encrypt) and HTTP → HTTPS redirect, a 4-line config file that never needs editing |
| 7.17 | **Gunicorn**, 3 workers, `--timeout 60` | Gunicorn's default 30 s is shorter than the worst valid request (20 s lock wait + 15 s OSRM + 5 s LLM) |
| 7.18 | `migrate` and `collectstatic` run on every `web` start | One command to deploy; safe on a single server |
| 7.19 | Settings from the environment: `DEBUG` (default `True` for dev), `SECRET_KEY` (required when `DEBUG=False`), `DOMAIN` → `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` | One setting to change per deployment; the app refuses to start in production with a missing secret or domain, or with a `change-me` placeholder left in `SECRET_KEY`, `DOMAIN` or `POSTGRES_PASSWORD` |
| 7.20 | When `DEBUG=False`: `SECURE_PROXY_SSL_HEADER`, secure session and CSRF cookies | Django knows requests came in over HTTPS through Caddy. `check --deploy` still lists HSTS (left off on purpose: hard to undo on a shared domain like DuckDNS) and `SECURE_SSL_REDIRECT` (Caddy already redirects) |
| 7.21 | WhiteNoise serves static files | The admin's CSS works with `DEBUG=False` without configuring Caddy for files |
| 7.22 | Only Caddy publishes ports; Postgres, Redis and Django stay on the internal Docker network | Nothing but HTTPS is reachable from the internet |
| 7.23 | `restart: unless-stopped` and log rotation (3 × 10 MB) on every service | Survives crashes and reboots; logs can't fill the disk |
| 7.24 | Console logging config in settings | Warnings (cache, OSRM, LLM, rate limits) appear in `docker compose logs web` with `DEBUG=False` |
| 7.25 | `.env.production.example` with every production setting | One file to copy and fill in on the server |

### 7.4 Changes to the deployment plan as first proposed

| Plan said | Problem | Done instead |
|---|---|---|
| `gunicorn config.wsgi` | The project module is `spotter` | `gunicorn spotter.wsgi` |
| Gunicorn defaults | 30 s timeout kills valid slow requests | `--timeout 60` (7.17) |
| DRF throttling on the smart endpoint | Behind Caddy all users share Caddy's IP, so one user would block everyone | `NUM_PROXIES=1` (7.13). Verified: faking `X-Forwarded-For` does not bypass the limit |
| (not covered) | DRF's throttle reads Redis directly: Redis down would turn every request into a 500 | Safe throttle that allows the request (7.12) |
| (not covered) | The public OSRM server could be overloaded through our API | Also limit `/api/route/` (7.9) |
| Manual `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, Caddyfile domain | Same value in three places | Derived from `DOMAIN` (7.16, 7.19) |
| `cp .env.example .env` on the server | Dev values (`DEBUG=True`, weak password) | `cp .env.production.example .env` (7.25) |
| Manual `migrate` | Easy to forget after updates | Runs on start (7.18) |
| "An unattached Elastic IP is billed" | AWS bills public IPv4 addresses even while attached (~$3.60/month) | Stated in DEPLOY.md's cost table |
| t3.small with no swap | The Docker build plus PostGIS can run out of memory on 2 GB | 2 GB swap file in the setup steps |
| Remove published ports for Postgres and Redis | They were never published | Kept so; only Caddy publishes ports (7.22) |

### 7.5 Verified

| Check | Result |
|---|---|
| Test suite | 99 tests pass (89 + 10 new: OpenRouter request/timeouts/errors/replies, Anthropic provider, rate limits per IP, behind a proxy and with Redis down) |
| Live OpenRouter call with a dummy key | OpenRouter answers 401 → `fallback` / `llm_error` in 0.41 s, logged |
| Full production stack locally (`DOMAIN=localhost`, separate Compose project, removed afterwards) | HTTP → HTTPS 308 redirect; page 200 over HTTPS; `DEBUG` off (plain 404 page); admin CSS served (WhiteNoise); unknown `Host` → 400; `X-Cache` MISS 0.76 s → HIT 6 ms; smart limit 3/hour → 4th request 429 even with a different fake `X-Forwarded-For` each time |
| Development stack | Unchanged: `runserver`, `DEBUG=True`, page and endpoints work |

Not tested here: a real AWS server and a real certificate (needs your account and domain),
and a successful OpenRouter answer (needs your key).

## 8. Known limitations

- Stop locations are city centers, not exact addresses.
- Mile markers are approximate (measured along the simplified line in lon/lat).
- Detours from the route to a stop are not counted in distance or fuel.
- The simplified route misses about 2% of corridor stops.
- The public OSRM server has no uptime guarantee or rate-limit promise.
- If the lock holder takes longer than 30 s (the lock timeout), a second request may also
  call OSRM. The 15 s OSRM timeout keeps this unlikely.
- The AI strategy picker sends the user's preference text to OpenRouter (and the model
  provider). Each call that isn't skipped costs one LLM request, limited per IP (7.9) and by
  the key's credit limit (7.7).
- Rate limits are per IP: many users behind one office network share a limit.
- The LLM call is synchronous: it holds a worker for up to 5 s.
