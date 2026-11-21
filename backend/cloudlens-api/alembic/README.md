# Alembic migrations

```bash
cd backend/cloudlens-api
export DATABASE_URL=postgresql+psycopg://cloudlens:cloudlens@localhost:5432/cloudlens
alembic upgrade head
alembic revision --autogenerate -m "describe change"
```

Migrations are driven by `DATABASE_URL` / app settings (`alembic/env.py`).
