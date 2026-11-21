# Docker deployments

The primary Compose stack lives at the repository root:

```bash
cp ../../.env.example ../../.env
cp ../../config/accounts.example.yaml ../../config/accounts.yaml
# Edit config/accounts.yaml before starting the stack.
cd ../..
docker compose up --build
```

The API container mounts `config/accounts.yaml` at `/config/accounts.yaml` and
sets `CLOUDLENS_CONFIG_PATH` accordingly.

Services:

| Service | Image build context | Port |
|---------|---------------------|------|
| `db` | `postgres:16-alpine` | 5432 |
| `api` | `backend/cloudlens-api` | 8000 |
| `ui` | `frontend/cloudlens-ui` | 5173 → 80 |

Health: `GET http://localhost:8000/health`
