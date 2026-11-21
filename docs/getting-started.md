# Getting started

## Clone and configure

```bash
cp .env.example .env
cp config/accounts.example.yaml config/accounts.yaml
# Edit config/accounts.yaml — set project/account aliases, account IDs,
# regions, collectors, and profile or AssumeRole auth.
# Ensure CLOUDLENS_CONFIG_PATH in .env points at that file.
```

Details: [configuration.md](configuration.md).


## Backend only

```bash
cd backend/cloudlens-api
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload --port 8000
```

Verify:

```bash
curl -s http://localhost:8000/health | python -m json.tool
```

## Frontend only

```bash
cd frontend/cloudlens-ui
npm install
npm run dev
```

Ensure `VITE_API_BASE_URL` matches the API (default in `.env.example`: `http://localhost:8000`).

## Full stack with Docker

```bash
docker compose up --build
```

## Tests

```bash
cd backend/cloudlens-api && pytest
cd ../.. && pytest tests/
```
