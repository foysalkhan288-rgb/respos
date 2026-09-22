# Cafe OS Intelligence Agent — MVP

LangGraph-backed POS intelligence layer with FastAPI, SQLite, and OpenRouter.

## Local Development

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest tests/ -v
uvicorn cafe_os.main:app --reload
```

The server initializes the schema and demo seed data on startup. To seed a
database file manually (e.g. before deploying), run:

```bash
python3 scripts/seed_db.py --db path/to/cafe_os.db
```

Order state is persisted via a SQLite LangGraph checkpointer (in the same DB
file), so in-flight order graphs survive restarts.

## Payments

- `POST /api/v1/orders/{id}/pay` — `{"payment_method": "cash|card|mobile", "shift_id"?}`.
  Marks the order paid, accrues loyalty points (1 pt per whole currency unit),
  and adds cash totals to the open shift's `expected_cash` (the given
  `shift_id`, or the only open shift). Paying twice → 400.

## Kitchen Display (KDS)

- `GET /api/v1/kds/orders` — list active tickets (`?status=`, `?include_completed=true`)
- `WS /ws/kds` — live channel. On connect the server sends a `snapshot` of
  active tickets; each new order dispatch pushes `order_dispatched`. Clients
  send `{"type": "status_update", "kds_id": ..., "status": "acknowledged|in_progress|completed"}`
  to move a ticket; updates are rebroadcast as `status_changed`. `{"type": "ping"}`
  returns `{"type": "pong"}`.

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

## Test

```bash
python3 -m pytest tests/ -v
```
