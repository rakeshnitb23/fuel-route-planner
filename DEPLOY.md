# Deploying to AWS

One EC2 server runs the whole stack with Docker Compose. Caddy serves HTTPS with a free,
automatically renewed certificate. Postgres, Redis and Django are never exposed to the internet.

```
Browser / Postman
      │  https://your-domain
      ▼
┌──────────────────── EC2 (Ubuntu 24.04, t3.small) ────────────────────┐
│  Caddy :80 :443  (HTTPS, HTTP → HTTPS redirect)                      │
│      │                                                               │
│      ▼                                                               │
│  Django + Gunicorn (3 workers) ──► OSRM (routing)                    │
│      │           │              ──► OpenRouter (AI strategy picker)  │
│      ▼           ▼                                                   │
│  PostGIS       Redis   (internal network only)                       │
└──────────────────────────────────────────────────────────────────────┘
```

Files used: `docker-compose.prod.yml`, `Caddyfile`, `.env.production.example`.

> **Why one EC2 server?** It is the simplest setup that is still production-shaped (HTTPS,
> Gunicorn, shared Redis cache, private database). At larger scale the same containers move to
> ECS (app), RDS for PostgreSQL with PostGIS (database) and ElastiCache (Redis) behind a load
> balancer. For an assessment, that adds cost and setup without adding anything to show.

Time needed: about 30–45 minutes.

---

## 1. Before you start

- [ ] The code is on GitHub, and **`.env` is not in the repository** (`git ls-files .env`
      prints nothing).
- [ ] You have an AWS account.
- [ ] You have an OpenRouter account with credits (for the AI strategy picker; optional).

## 2. Create the OpenRouter key

1. Go to <https://openrouter.ai/settings/keys> → **Create API key**.
2. Name: `fuel-route-prod`. **Credit limit: `2`** (US dollars). If the key leaks, it can't
   spend more than that.
3. Copy the key (it starts with `sk-or-v1-`). You only see it once. You'll paste it in step 8.

Model: **`anthropic/claude-haiku-5.5`** (already the default). It supports forced tool
calls, is fast, and costs about **$0.0001 per request**: roughly 12,000 AI decisions for $1.

## 3. Set a budget alert (do this first)

AWS Console → **Billing and Cost Management → Budgets → Create budget** → *Use a template* →
**Monthly cost budget**, amount **$10**, your email. You get an email before costs surprise you.

## 4. Launch the server

AWS Console → **EC2 → Launch instance**:

| Setting | Value | Why |
|---|---|---|
| Name | `fuel-route` | |
| Image | **Ubuntu Server 24.04 LTS, 64-bit (x86)** | The PostGIS image is x86-only; do not pick Arm |
| Instance type | **t3.small** (2 GB RAM) | t3.micro (1 GB) is too small for PostGIS + Redis + Django |
| Key pair | **Create new key pair** → `fuel-route`, RSA, `.pem` → download | For SSH |
| Storage | **20 GiB gp3** | Docker images, database, logs |

Under **Network settings → Edit → Security group rules**:

| Type | Port | Source | Why |
|---|---|---|---|
| SSH | 22 | **My IP** | Only you can log in |
| HTTP | 80 | Anywhere (0.0.0.0/0) | Certificate check + redirect to HTTPS |
| HTTPS | 443 | Anywhere (0.0.0.0/0) | The site and API |

Do **not** open 5432 (Postgres), 6379 (Redis) or 8000 (Django).

Click **Launch instance**.

## 5. Give it a fixed IP (Elastic IP)

EC2 → **Elastic IPs → Allocate Elastic IP address → Allocate**. Then select it →
**Actions → Associate Elastic IP address** → choose the `fuel-route` instance → **Associate**.

Without this, the public IP changes whenever the instance stops, and your domain breaks.
Write down the IP: it's `<ELASTIC_IP>` below.

## 6. Point a domain at it

Pick one:

- **Free: DuckDNS.** Sign in at <https://www.duckdns.org>, add a subdomain (for example
  `fuelroute`), put `<ELASTIC_IP>` in **current ip**, click **update ip**. Your domain is
  `fuelroute.duckdns.org`.
- **Your own domain** (Route 53 or any registrar): add an **A record**, for example `api` →
  `<ELASTIC_IP>`. Your domain is `api.yourdomain.com`.
  On **Spaceship**: Launchpad → **Domain Manager** → your domain → **Advanced DNS** →
  **Add record** → Type `A`, Host `fuel`, Value `<ELASTIC_IP>`, TTL 5 min → Save. Your
  domain is `fuel.yourdomain.com`.

Check from your laptop (may take a few minutes):

```bash
dig +short fuelroute.duckdns.org     # must print <ELASTIC_IP>
```

Caddy can only get the HTTPS certificate once this works.

## 7. Prepare the server

```bash
chmod 400 ~/Downloads/fuel-route.pem
ssh -i ~/Downloads/fuel-route.pem ubuntu@<ELASTIC_IP>
```

On the server:

```bash
# 2 GB swap: headroom for the Docker build and PostGIS on a 2 GB machine
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Docker + Compose
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```

Log back in so the `docker` group applies:

```bash
ssh -i ~/Downloads/fuel-route.pem ubuntu@<ELASTIC_IP>
docker compose version     # should print a version
```

## 8. Get the code and configure it

```bash
git clone https://github.com/<you>/<repo>.git fuel-route
cd fuel-route
```

For a **private** repository, GitHub asks for a username and password. Use a
[personal access token](https://github.com/settings/tokens) (read-only, "Contents") as the
password.

Create the production `.env`:

```bash
cp .env.production.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(50))"   # copy this for SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # copy this for POSTGRES_PASSWORD
nano .env
chmod 600 .env
```

In `nano`, set these (Ctrl+O, Enter to save; Ctrl+X to exit):

| Variable | Set to |
|---|---|
| `DOMAIN` | Your domain from step 6, without `https://`, e.g. `fuelroute.duckdns.org` |
| `SECRET_KEY` | The first generated value |
| `POSTGRES_PASSWORD` | The second generated value. Set it **before** the first start: the database is created with it |
| `OPENROUTER_API_KEY` | The key from step 2 (or leave empty: the AI mode then uses `lowest_cost`) |

Leave the rest as it is. `DEBUG=False` is already set. `ALLOWED_HOSTS` and
`CSRF_TRUSTED_ORIGINS` are derived from `DOMAIN`.

## 9. Start it

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

The first build takes a few minutes. On every start, the `web` container applies database
migrations and collects static files by itself. Then load the truck stops (once):

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py loaddata truckstops
# Installed 6614 object(s) from 1 fixture(s)
```

Check all four services are running:

```bash
docker compose -f docker-compose.prod.yml ps
```

## 10. Verify

1. Open `https://<your domain>/`: the web page loads with a padlock in the address bar.
2. Plan Chicago, IL → Houston, TX twice: the **Route cache** card shows `MISS`, then `HIT`.
3. Switch to **Let the AI choose**, type "I'm in a hurry, few stops": the decision box shows
   `Decided by: llm`. If it shows `fallback`, see Troubleshooting below.
4. Postman: `POST https://<your domain>/api/route/` with
   `{"start": "Chicago, IL", "finish": "Houston, TX"}`.
5. `http://<your domain>/` redirects to `https://`.

## Everyday commands

All run in `~/fuel-route` on the server.

```bash
docker compose -f docker-compose.prod.yml logs -f web        # app logs (Ctrl+C to stop)
docker compose -f docker-compose.prod.yml logs caddy         # HTTPS / certificate logs
docker compose -f docker-compose.prod.yml up -d             # after editing .env (restart does not reload it)

# Deploy new code
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Tip: `alias dcp='docker compose -f docker-compose.prod.yml'` saves typing.

The containers restart automatically after a crash or a server reboot
(`restart: unless-stopped`). Logs are rotated (3 × 10 MB per container).

## Security and cost limits in place

| Protection | Where |
|---|---|
| HTTPS only, certificate auto-renewed | Caddy |
| Only ports 22 (your IP), 80, 443 open | Security group; only Caddy publishes ports |
| `DEBUG=False`, secret key from `.env`, only your domain accepted | `spotter/settings.py` |
| Per-IP limits: **20/hour** on `/api/route/smart/` (paid LLM), **120/hour** on `/api/route/` (protects the free OSRM server). Over the limit: `429 throttled` | `SMART_RATE_LIMIT`, `ROUTE_RATE_LIMIT` in `.env` |
| Real client IP used for limits (not Caddy's), and a fake `X-Forwarded-For` header can't bypass them | `NUM_PROXIES=1` in `docker-compose.prod.yml` |
| OpenRouter spend capped by the key's credit limit | OpenRouter dashboard |
| AWS spend alert | Budgets |

## Cost

| Item | Approximate cost |
|---|---|
| t3.small, running all month | ~$15/month (us-east-1) |
| 20 GB gp3 disk | ~$1.60/month |
| Public IPv4 address (Elastic IP) | ~$3.60/month, **charged even while attached** |
| OpenRouter | ~$0.0001 per AI decision, capped by the key limit |

New AWS accounts usually get free-tier credits; check the current AWS Free Tier page.

**When you are done** (after the hiring process): EC2 → **Terminate** the instance, then
Elastic IPs → **Release** the address (it keeps billing otherwise), and delete the
OpenRouter key.

## Troubleshooting

| Problem | Fix |
|---|---|
| Browser: certificate error, or Caddy logs `challenge failed` | The domain doesn't point at `<ELASTIC_IP>` yet (`dig +short <domain>`), or port 80 is closed in the security group. Fix it, then `docker compose -f docker-compose.prod.yml restart caddy` |
| `400 Bad Request` | `DOMAIN` in `.env` doesn't match the address you opened. Fix it, then `docker compose -f docker-compose.prod.yml up -d` |
| `502` from Caddy right after starting | `web` is still migrating or starting; wait 20 s. If it persists: `logs web` |
| `web` exits with `SECRET_KEY must be set`, `Set DOMAIN` or `Replace 'change-me'` | Fill in `.env` (step 8), then `docker compose -f docker-compose.prod.yml up -d` |
| Database `password authentication failed` | The password was changed after the first start. Either put the old one back, or reset the database: `down -v`, then step 9 again |
| AI decision shows `fallback_reason: no_api_key` | `OPENROUTER_API_KEY` is empty; set it, then `docker compose -f docker-compose.prod.yml up -d` |
| AI decision shows `llm_error` | `logs web` shows the reason: 401 = wrong key, 402 = key credit limit reached or no credits |
| `429 throttled` | The rate limit worked. Wait, or raise `SMART_RATE_LIMIT` / `ROUTE_RATE_LIMIT` |
| Build is killed / server freezes | Out of memory: check the swap from step 7 (`free -h`) |
| `routing_unavailable` | The public OSRM server is busy; retry |
