# Cafe OS Intelligence Agent — MVP

LangGraph-backed POS intelligence layer with FastAPI, SQLite, and OpenRouter.

## Local Development

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest tests/ -v
uvicorn cafe_os.main:app --reload
```

## Deployment

### Option A: Render (Recommended — Free Tier)

1. Push this repo to GitHub
2. Go to https://render.com and sign up
3. Click **New +** → **Web Service**
4. Connect your GitHub repo
5. Render auto-detects `render.yaml`
6. Click **Create Web Service**
7. Your public URL will be: `https://cafe-os.onrender.com`

### Option B: Railway (Free Tier)

1. Push this repo to GitHub
2. Go to https://railway.app and sign up
3. Click **New Project** → **Deploy from GitHub repo**
4. Select your repo
5. Railway auto-detects `railway.json`
6. Your public URL will be: `https://cafe-os-production.up.railway.app`

### Option C: Docker (Any Provider)

```bash
docker build -t cafe-os .
docker run -p 8000:8000 cafe-os
```

## Environment Variables

See `.env.example` for required variables.

## API Documentation

Once deployed, visit `https://your-url.com/docs` for interactive API docs.

### Kitchen Display System (WebSocket)

KDS clients connect to `/ws/kds` and receive `kds.dispatched` JSON events in
real time as orders are dispatched — no polling required:

```js
const ws = new WebSocket("wss://your-url.com/ws/kds");
ws.onmessage = (e) => console.log(JSON.parse(e.data));
```

A REST fallback is available at `GET /api/v1/kds/orders`, and tickets advance
through their lifecycle via `PATCH /api/v1/kds/{kds_id}` with body
`{"status": "preparing" | "ready" | "served"}` (pushed to connected KDS clients).

### Payments

Charge an order via `POST /api/v1/orders/{order_id}/pay` with body
`{"payment_method": "cash" | "card" | "mobile"}` (optional `"amount"`).
Cash settles through the drawer; card/mobile use Stripe when `STRIPE_API_KEY`
is set, otherwise a local mock gateway approves automatically.

### Seeding

```bash
python scripts/seed_db.py            # seeds ./cafe_os.db
python scripts/seed_db.py --db x.db  # seeds a custom path
```

### Health Probes

- `GET /healthz` — liveness (process is up)
- `GET /readyz` — readiness (verifies database, and Redis when configured; 503 when degraded)

## Production Deployment (PostgreSQL + Redis)

SQLite and in-process KDS delivery are the defaults. For production scale:

```bash
pip install -e ".[prod]"   # asyncpg + redis drivers
```

| Variable | Effect |
|---|---|
| `DATABASE_URL` | `postgresql://user:pass@host:5432/db` — switches all persistence to PostgreSQL via asyncpg |
| `REDIS_URL` | `redis://host:6379/0` — fans KDS websocket events out across multiple uvicorn workers |

The PostgreSQL backend translates the SQLite-flavored SQL on the fly
(placeholders, date functions, upserts); timestamps stay ISO-8601 text in both
backends, so no schema drift. Redis fan-out uses pub/sub with per-worker
origin tagging to avoid duplicate deliveries.

Full stack locally with Docker Compose (app + Postgres + Redis):

```bash
docker compose up --build
# app on http://localhost:8000 — /docs for API reference
```

## Test

```bash
python3 -m pytest tests/ -v
```
