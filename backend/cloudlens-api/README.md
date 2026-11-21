# cloudlens-api

FastAPI backend for AWS CloudLens.

## Layers

- **`app.api`** — HTTP surface (`/health`, future inventory/analytics routes)
- **`app.accounts`** — load/validate external accounts YAML (`CLOUDLENS_CONFIG_PATH`)
- **`app.connectors`** — AWS connectors + thread-safe `AWSSessionManager` (profile / AssumeRole, STS refresh, GetCallerIdentity)
- **`app.collectors`** — paginated collectors + `CollectionOrchestrator` (concurrency, retries, upserts, stale)
- **`app.collectors.network`** — VPC/subnet/RT/IGW/NAT/endpoint/SG/NACL/ENI collectors (Boto3 EC2)
- **`app.collectors.compute`** — EC2 instances, EBS volumes, Elastic IPs
- **`app.collectors.elbv2`** — ALB/NLB, listeners, target groups, target health
- **`app.collectors.ecs`** — ECS clusters, services, task definitions
- **`app.collectors.eks`** — EKS clusters, node groups, Fargate profiles (no kubectl)
- **`app.collectors.rds`** — RDS/Aurora DB instances and clusters (config metadata only)
- **`app.collectors.s3`** — S3 buckets by resolved region (no object/data-plane access)
- **`app.collectors.wafv2`** — WAFv2 Web ACLs + associations (regional and CloudFront scope)
- **`app.collectors.route53`** — Route 53 hosted zones and record-set counts (no record values)
- **`app.collectors.logs`** — CloudWatch log group metadata (never log contents)
- **`app.analytics`** — analysis pipelines over collected inventory
- **`app.db`** — SQLAlchemy engine/session, inventory ORM models, repositories
- **`app.core`** — settings and shared utilities

### Database migrations

```bash
export DATABASE_URL=sqlite+pysqlite:///./data/cloudlens.db
alembic upgrade head
```

AWS account IDs, regions, roles, and profiles are supplied only via accounts YAML — never hardcoded in Python.

## Development

```bash
export CLOUDLENS_CONFIG_PATH=../../config/accounts.yaml
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
pytest
```
