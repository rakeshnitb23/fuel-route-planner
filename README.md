# Fuel Route Planner

A Django REST API that plans a road trip between two US cities and picks cost-effective
fuel stops along the route, using real truck stop fuel prices.

Give it a start and a finish, and it returns:

- the driving route as GeoJSON, plus a link to an interactive map
- the fuel stops to use, with gallons and cost at each
- total distance, total gallons and total fuel cost

It also has a **web page** at `http://localhost:8000/` for trying the API in a browser, and
an optional **AI strategy picker** (`POST /api/route/smart/`) that chooses the fuel strategy
from a plain-English preference such as *"I'm in a hurry, I don't want many stops"*.

Vehicle assumptions: **500-mile range**, **10 miles per gallon**.

Design decisions and trade-offs are documented in [DECISIONS.md](DECISIONS.md).
Deployment to AWS (one EC2 server, HTTPS) is in [DEPLOY.md](DEPLOY.md).

---

## Features

- **One routing call per trip.** Routes come from the free OSRM server. Repeat requests,
  other strategies and the map page reuse the route cached in Redis.
- **No geocoding API.** Cities are resolved from an offline US cities table.
- **Three fuel strategies:** `lowest_cost` (default), `balanced`, `fewest_stops`.
- **Spatial search in PostGIS.** Only stations within 10 miles of the route are considered.
- **Fast.** About 0.7 s for a new trip, about 2 ms for a cached one.
- **Clear errors.** Every error has a stable `code`, a readable `message` and `details`.
- **Production-grade caching.** Redis with LFU eviction, one OSRM call even under
  concurrent requests, and the API keeps working if Redis goes down.
- **Web page.** Plan a trip, compare strategies and see the map at `/`, no Postman needed.
- **AI strategy picker (optional).** One LLM call picks a strategy from the user's own words.
  Works without an API key: it falls back to `lowest_cost` and says why.
- **Rate limited.** Per-IP limits on both endpoints, so a public deployment can't run up
  LLM costs or overload the free routing server.
- **Production setup included.** Gunicorn, Caddy (automatic HTTPS) and a step-by-step AWS
  guide.
- **Tested.** 99 tests, no internet, Redis or API key needed.

## Tech stack

| Component | Version |
|---|---|
| Python | 3.13 |
| Django | 6.1.2 |
| Django REST Framework | 3.18.3 |
| PostgreSQL + PostGIS | 17 + 3.5 |
| Redis (cache) | 7.4, client `redis` 8.0.1 |
| Routing | [OSRM](https://project-osrm.org/) public server |
| Map | [Leaflet](https://leafletjs.com/) 1.9.4 + OpenStreetMap tiles |
| AI strategy picker | [OpenRouter](https://openrouter.ai), model `anthropic/claude-haiku-5.5` (default); or the Anthropic API directly (SDK 1.13.0) |
| Production | Gunicorn 26.2.0, WhiteNoise 6.12.0, Caddy 2.11 |

---

## Quick start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Docker Compose v2
- Port `8000` free on your machine
- Internet access (for OSRM routing and map tiles)

### 1. Configure the environment

```bash
cp .env.example .env
```

The defaults work as they are. To enable the AI strategy picker, set `OPENROUTER_API_KEY`
in `.env` (optional). See [Configuration](#configuration).

### 2. Start the containers

```bash
docker compose up -d --build
```

This starts three services:

| Service | What it runs |
|---|---|
| `db` | PostgreSQL + PostGIS, with a health check |
| `redis` | Redis cache, 256 MB max, least-frequently-used keys evicted first |
| `web` | The Django app on `http://localhost:8000`, started once `db` and `redis` are healthy |

### 3. Create the database tables

```bash
docker compose exec web python manage.py migrate
```

### 4. Load the truck stops

```bash
docker compose exec web python manage.py loaddata truckstops
```

This loads 6,614 cleaned US truck stops from `fuel/fixtures/truckstops.json`.

### 5. Try it

Open **<http://localhost:8000/>** in a browser, or use curl:

```bash
curl -X POST http://localhost:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "New York, NY", "finish": "Los Angeles, CA"}'
```

Then open the `map_url` from the response in your browser, for example:

<http://localhost:8000/api/route/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&strategy=lowest_cost>

### Stopping and resetting

```bash
docker compose stop          # stop, keep data
docker compose down          # remove containers, keep data
docker compose down -v       # remove containers and the database volume
```

---

## API reference

### `POST /api/route/`

Plans a trip.

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `start` | string | yes | `"City, ST"` or `"City, State Name"`, e.g. `"St. Louis, MO"`, `"Los Angeles, California"` |
| `finish` | string | yes | Same format as `start` |
| `strategy` | string | no | `lowest_cost` (default), `balanced`, or `fewest_stops` |

```json
{"start": "Chicago, IL", "finish": "Houston, TX", "strategy": "balanced"}
```

**Response `200 OK`**

```json
{
  "start": "New York, NY",
  "finish": "Los Angeles, CA",
  "strategy": "lowest_cost",
  "total_distance_miles": 2800.6,
  "total_gallons": 280.06,
  "total_cost": 846.76,
  "fuel_stops": [
    {
      "name": "7-ELEVEN #40084",
      "city": "Palisades Park",
      "state": "NJ",
      "price": 3.099,
      "mile_marker": 3.0,
      "gallons": 6.42,
      "cost": 19.89,
      "lon": -73.995436,
      "lat": 40.846238
    }
  ],
  "route": {
    "type": "LineString",
    "coordinates": [[-73.996496, 40.748595], [-74.226568, 40.893518]]
  },
  "map_url": "/api/route/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&strategy=lowest_cost"
}
```

(`fuel_stops` and `route.coordinates` are shortened here.)

| Field | Description |
|---|---|
| `total_distance_miles` | Driving distance from OSRM |
| `total_gallons` | Always `total_distance_miles / 10` |
| `total_cost` | Sum of `cost` over all fuel stops, in USD |
| `fuel_stops[].price` | Price per gallon at that stop, in USD |
| `fuel_stops[].mile_marker` | Approximate distance from the start, in miles |
| `fuel_stops[].gallons` / `cost` | Fuel bought at that stop and what it cost |
| `fuel_stops[].lon` / `lat` | Stop location (city center), for drawing it on a map |
| `route` | GeoJSON LineString, coordinates in `[longitude, latitude]` order |
| `map_url` | Link to the map page for this trip |

### `GET /api/route/map/`

An HTML page showing the route and fuel stops on a Leaflet map. Click a marker for the
stop's price, gallons and cost.

| Query parameter | Required | Description |
|---|---|---|
| `start` | yes | Same as the API |
| `finish` | yes | Same as the API |
| `strategy` | no | Same as the API |

### `POST /api/route/smart/`

Plans a trip and lets an LLM choose the fuel strategy from a free-text preference. The
code computes all three plans and their costs; the LLM only picks one and explains why.

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `start` | string | yes | Same as `/api/route/` |
| `finish` | string | yes | Same as `/api/route/` |
| `preference` | string | no | What the driver wants, up to 300 characters |

```json
{"start": "Chicago, IL", "finish": "Houston, TX", "preference": "I'm in a hurry, I don't want many stops"}
```

**Response `200 OK`**: the same body as `/api/route/` for the chosen strategy, plus:

```json
{
  "decision": {
    "strategy": "fewest_stops",
    "reason": "Half the stops (4 vs 8) for $6.39 more (+1.98%).",
    "decided_by": "llm",
    "fallback_reason": null
  },
  "comparison": [
    {"strategy": "lowest_cost", "stops": 8, "total_cost": 323.35, "extra_cost": 0.0, "extra_cost_pct": 0.0, "description": "..."},
    {"strategy": "balanced", "stops": 5, "total_cost": 324.88, "extra_cost": 1.53, "extra_cost_pct": 0.47, "description": "..."},
    {"strategy": "fewest_stops", "stops": 4, "total_cost": 329.74, "extra_cost": 6.39, "extra_cost_pct": 1.98, "description": "..."}
  ]
}
```

| `decided_by` | When | LLM called? |
|---|---|---|
| `llm` | The LLM chose a valid strategy | yes, once |
| `default` | `preference` is empty | no, `lowest_cost` |
| `identical` | All three strategies give the same plan (common on short trips) | no, `lowest_cost` |
| `fallback` | No API key, timeout (5 s), LLM error, or invalid answer; see `fallback_reason` (`no_api_key`, `timeout`, `llm_error`, `invalid_response`) | maybe, `lowest_cost` |

Errors are the same as `/api/route/`. An infeasible trip (for example `range_gap`) returns
the error without calling the LLM. `X-Cache` works the same way, and the route cache is
shared with `/api/route/`.

### `GET /`

The web page. It calls the two endpoints above from the browser and shows the totals, the
AI decision, the strategy comparison, the map, the stop list, and the raw request and
response (with an equivalent `curl` command).

### Fuel strategies

| Strategy | Behavior | Typical result |
|---|---|---|
| `lowest_cost` | Lowest possible fuel cost | Most stops, some small purchases |
| `balanced` | Only moves on to a cheaper stop if it saves at least 5¢/gal | ~28% fewer stops, ~0.3% more cost |
| `fewest_stops` | Fills up and goes to the cheapest stop in range | ~39% fewer stops, ~1.9% more cost |

Every trip starts with an empty tank. The first stop is the cheapest station in the first
50 miles, and it also pays for the fuel used to reach it.

### Errors

Every error has the same shape:

```json
{
  "error": {
    "code": "range_gap",
    "message": "No fuel station within 500 miles after mile 259 (Halsey, OR).",
    "details": {"from_mile": 259.3, "from_stop": "Halsey, OR", "max_range": 500}
  }
}
```

| HTTP | `code` | When | Example |
|---|---|---|---|
| 400 | `invalid_input` | Missing field, unknown strategy, location without a state | `"New York"` |
| 400 | `unknown_city` | City not in the US cities table | `"Atlantis, NY"`, `"Toronto, ON"` |
| 400 | `parse_error` | Malformed JSON | |
| 405 | `method_not_allowed` | Wrong HTTP method | `GET /api/route/` |
| 415 | `unsupported_media_type` | Body is not JSON or form data | |
| 422 | `no_route` | No drivable road between the cities | Honolulu, HI → Denver, CO |
| 422 | `no_station_near_start` | No fuel station within reach of the start | Eureka, CA → San Diego, CA |
| 422 | `range_gap` | A stretch of more than 500 miles with no station | Seattle, WA → San Diego, CA |
| 502 | `routing_unavailable` | The routing service failed | |
| 429 | `throttled` | Rate limit reached (`details.retry_after_seconds`, plus a `Retry-After` header) | 21st smart request in an hour |
| 504 | `routing_timeout` | The routing service did not answer within 15 s | |

The map page shows the same message and status code.

### Postman

1. Create a `POST` request to `http://localhost:8000/api/route/`.
2. Under **Body**, choose **raw** → **JSON** and paste a request body.
3. Send. Open the `map_url` in a browser, prefixed with `http://localhost:8000`.
4. For the AI strategy picker, send the same kind of body to
   `http://localhost:8000/api/route/smart/` with a `"preference"` field instead of `"strategy"`.

---

## Configuration

Settings come from `.env` (copied from `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_DB` | `spotter` | Database name |
| `POSTGRES_USER` | `spotter` | Database user |
| `POSTGRES_PASSWORD` | `spotter` | Database password |
| `POSTGRES_HOST` | `db` | Database host (the Compose service name) |
| `POSTGRES_PORT` | `5432` | Database port |
| `REDIS_URL` | `redis://redis:6379/0` | Redis cache. Leave unset to use an in-memory cache instead |
| `STRATEGY_ROUTER_PROVIDER` | `openrouter` | AI provider: `openrouter` or `anthropic` |
| `OPENROUTER_API_KEY` | empty | Enables the AI strategy picker (OpenRouter). Empty → `/api/route/smart/` still works, using `lowest_cost` |
| `STRATEGY_ROUTER_MODEL` | `anthropic/claude-haiku-5.5` | Model for the AI strategy picker. With `anthropic` as provider, use `claude-haiku-5-5` |
| `ANTHROPIC_API_KEY` | empty | Only used when the provider is `anthropic` |
| `SMART_RATE_LIMIT` | `20/hour` | Per-IP limit on `/api/route/smart/` |
| `ROUTE_RATE_LIMIT` | `120/hour` | Per-IP limit on `/api/route/` |
| `DEBUG` | `True` | Set `False` in production |
| `SECRET_KEY` | dev key | Required when `DEBUG=False` |
| `DOMAIN` | empty | Production domain; sets `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`. Required when `DEBUG=False` |

Production values are in `.env.production.example`; see [DEPLOY.md](DEPLOY.md).

After changing `.env`, restart the app: `docker compose up -d web`.

`.env` is ignored by Git and Docker builds. Never commit real credentials.

## Caching

Each trip's route and nearby stations are cached in Redis for 24 hours, keyed by the two
cities after normalization, so `"st. louis, mo"` and `"Saint Louis, Missouri"` share an
entry. The fuel strategy runs on every request (under 1 ms), so all three strategies share
one cached route.

| Behavior | Detail |
|---|---|
| Concurrent requests | Only one request per trip calls OSRM; the others wait up to 20 s for its result |
| Redis down | Requests still succeed, treated as cache misses; a warning is logged |
| Errors | Never cached |
| Eviction | `allkeys-lfu`, 256 MB limit |
| Invalidate everything | Bump `VERSION` in `CACHES` in `spotter/settings.py` |

### Check whether a request was cached

`POST /api/route/` returns an `X-Cache` header: `MISS` when this request called OSRM, `HIT`
when the route came from the cache.

```bash
curl -s -D - -o /dev/null -X POST http://localhost:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Denver, CO", "finish": "Dallas, TX"}' | grep -i x-cache
```

Run it twice: the first shows `X-Cache: MISS`, the second `X-Cache: HIT`.

### Inspect the cache

```bash
docker compose exec redis redis-cli KEYS "fuelroute:*"
# fuelroute:1:route:denver|CO|dallas|TX

docker compose exec redis redis-cli TTL "fuelroute:1:route:denver|CO|dallas|TX"
docker compose exec redis redis-cli CONFIG GET maxmemory-policy
docker compose exec redis redis-cli FLUSHDB      # clear the cache
```

Keys are `fuelroute:<version>:route:<start city>|<state>|<finish city>|<state>`.

## Data

| File | Contents |
|---|---|
| `data/fuel-prices-for-be-assessment.csv` | Provided truck stop fuel prices (8,151 rows) |
| `data/us-cities.csv` | 29,738 US cities with coordinates, from [kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database) (MIT, see `data/us-cities.LICENSE`) |
| `fuel/fixtures/truckstops.json` | The cleaned stops, ready to load |
| `reports/load_stops.json` | Report from the last data load |

To rebuild the stops from the raw CSV instead of the fixture:

```bash
docker compose exec web python manage.py load_stops
```

This keeps the lowest price per station, matches each station's city to coordinates, skips
unmatched stations (mostly Canadian), replaces the existing stops, and writes
`reports/load_stops.json`. Current result: 6,614 loaded, 1.84% unmatched (0.18% of US
stations).

## Running tests

```bash
docker compose exec web python manage.py test fuel
```

| File | Covers |
|---|---|
| `fuel/tests/test_optimizer.py` | Every rule of all three strategies, gap and no-station errors, gallons invariant on 900 random plans |
| `fuel/tests/test_locations.py` | City/state normalization and lookup |
| `fuel/tests/test_routing.py` | OSRM success, timeout, failure and "no route" (network faked); 10-mile corridor and mile markers |
| `fuel/tests/test_api.py` | Response format, strategies, caching, map page, every error code |
| `fuel/tests/test_cache.py` | `X-Cache` hits and misses, shared entries across spellings, stampede lock, Redis down, failures not cached |
| `fuel/tests/test_router.py` | AI strategy picker: every `decided_by` path, invalid answers, timeouts, no LLM call on infeasible trips, the requests sent to OpenRouter and Anthropic (faked); rate limits (per IP, behind a proxy, Redis down); the web page |

Tests always use an in-memory cache and never have an API key, so they never touch Redis or
an LLM. Expected: `Ran 99 tests ... OK`.

---

## How it works

```
"New York, NY" ──► offline cities table ──► lon/lat
                                              │
                     one OSRM call ◄──────────┘
                          │
                route line + total miles ──► Redis (24 h)
                          │
          PostGIS: stations within 10 miles of the route
                          │
              mile marker for each station, sorted
                          │
            fuel strategy picks stops and gallons
                          │
                 JSON response  /  Leaflet map
```

## Project structure

```
.
├── data/                          fuel prices CSV, US cities CSV + license
├── reports/load_stops.json        data load report
├── fuel/                          the app
│   ├── models.py                  TruckStop (PostGIS geography point)
│   ├── locations.py               city parsing, normalization, lookup
│   ├── routing.py                 OSRM call, corridor search, mile markers
│   ├── optimizer.py               fuel strategies (pure Python)
│   ├── services.py                cached trip lookup, strategy comparison, response building
│   ├── router.py                  AI strategy picker (one LLM call, with fallback)
│   ├── throttles.py               per-IP rate limit that tolerates Redis being down
│   ├── serializers.py             request validation
│   ├── views.py                   API and map views, error mapping
│   ├── exceptions.py              error shape for framework errors
│   ├── urls.py                    /api/route/, /api/route/smart/, /api/route/map/
│   ├── templates/fuel/index.html  web page at /
│   ├── templates/fuel/map.html    Leaflet map page
│   ├── management/commands/
│   │   └── load_stops.py          CSV → database
│   ├── fixtures/truckstops.json   cleaned stops
│   └── tests/                     test suite
├── spotter/                       Django project settings and URLs
├── Dockerfile
├── docker-compose.yml             development (runserver, code mounted)
├── docker-compose.prod.yml        production (Gunicorn + Caddy HTTPS)
├── Caddyfile                      HTTPS reverse proxy config
├── .env.example                   development settings
├── .env.production.example        production settings
├── requirements.txt
├── README.md
├── DECISIONS.md
└── DEPLOY.md                      AWS deployment guide
```

## Assumptions and limitations

- Trips start with an empty tank.
- Station locations are city centers; the CSV has no coordinates.
- Mile markers are approximate.
- Detours from the route to a station are not counted.
- The data has only 8 California stations, so some West Coast trips return `range_gap`.
- The public OSRM server has no uptime guarantee.

## Production

`docker-compose.prod.yml` runs the same app for a real server. Step-by-step AWS guide:
[DEPLOY.md](DEPLOY.md).

| Item | Development (`docker-compose.yml`) | Production (`docker-compose.prod.yml`) |
|---|---|---|
| App server | `runserver`, code mounted | Gunicorn, 3 workers, 60 s timeout, built image |
| HTTPS | None | Caddy, automatic Let's Encrypt certificate |
| `DEBUG` / `SECRET_KEY` | `True` / dev key | `False` / from `.env` (required) |
| Hosts accepted | localhost | `DOMAIN` only |
| Static files (admin) | Django | WhiteNoise |
| Published ports | 8000 | 80 and 443 only (Caddy) |
| Migrations | Manual | Run on every start |
| Restart / logs | Manual | `unless-stopped`, logs rotated |
| Rate limits | On | On, real client IP from Caddy (`NUM_PROXIES=1`) |

Still worth doing at larger scale:

| Item | Now | At scale |
|---|---|---|
| Hosting | One EC2 server | ECS + RDS (PostGIS) + ElastiCache behind a load balancer |
| Routing | Public OSRM demo server | Self-hosted OSRM, or a paid provider |
| AI strategy picker | Synchronous LLM call (up to 5 s) per request | Async views if traffic grows. Users' preference text goes to OpenRouter and the model provider: mention it in a privacy policy |
| Backups | None | Scheduled `pg_dump` or RDS snapshots |

## Troubleshooting

| Problem | Fix |
|---|---|
| `port is already allocated` | Free port 8000, or change `"8000:8000"` in `docker-compose.yml` |
| `relation "fuel_truckstop" does not exist` | Run the `migrate` step before `loaddata` |
| Every trip returns `no_station_near_start` | The stops are not loaded: run `loaddata truckstops` |
| `routing_unavailable` or `routing_timeout` | The public OSRM server is busy or unreachable; retry shortly |
| Slow database start on Apple Silicon | The PostGIS image runs under x86 emulation (`platform: linux/amd64`); this is expected |
| Start over with a clean database | `docker compose down -v`, then repeat steps 2–4 |
| Warnings `Cache get failed ...` in the logs | Redis is unreachable; the API keeps working without the cache. Check `docker compose ps redis` |
| A trip keeps returning old data | Clear it with `redis-cli FLUSHDB`, or bump `VERSION` in `CACHES` |
| `/api/route/smart/` always returns `fallback_reason: no_api_key` | Set `OPENROUTER_API_KEY` in `.env`, then `docker compose up -d web` |
| `fallback_reason: llm_error` | The reason is in the `web` logs: 401 = wrong key, 402 = no credits or the key's credit limit reached |
| `429 throttled` | The per-IP rate limit; wait, or raise `SMART_RATE_LIMIT` / `ROUTE_RATE_LIMIT` in `.env` |
