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

## Test

```bash
python3 -m pytest tests/ -v
```
