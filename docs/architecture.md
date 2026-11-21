# Architecture

AWS CloudLens is a monorepo with a clear separation between the HTTP API and the domain pipelines that will eventually talk to AWS.

```
┌─────────────────────────────────────────────────────────────┐
│  cloudlens-ui (React / Vite)                                │
│  — dashboard shell, health badge, future inventory views    │
└────────────────────────────┬────────────────────────────────┘
                             │ HTTP
┌────────────────────────────▼────────────────────────────────┐
│  API layer  app.api                                         │
│  — routes, deps, Pydantic request/response models           │
└───────┬──────────────────┬──────────────────┬───────────────┘
        │                  │                  │
┌───────▼───────┐  ┌───────▼───────┐  ┌───────▼───────────────┐
│ Connectors    │  │ Collectors    │  │ Analytics             │
│ app.connectors│  │ app.collectors│  │ app.analytics         │
│ AWS session / │  │ inventory     │  │ summaries & pipelines │
│ client factory│  │ orchestration │  │ over collected data   │
└───────┬───────┘  └───────▲───────┘  └──────────▲────────────┘
        │                  │                     │
        │         uses     │         consumes    │
        └──────────────────┘─────────────────────┘
                             │
                    ┌────────▼────────┐
                    │ SQLAlchemy / DB │
                    │ app.db          │
                    └─────────────────┘
```

## Layer rules

1. **API** may depend on connectors, collectors, analytics, and DB — never the reverse for HTTP concerns.
2. **Collectors** use **connectors** to obtain clients; they must not import FastAPI routers.
3. **Analytics** consumes collector results (or persisted inventory); it does not call AWS directly.
4. **Connectors** own credential/session construction. In this scaffold they expose metadata and session kwargs only — **no live AWS API calls**.
5. **Accounts YAML** (`CLOUDLENS_CONFIG_PATH`) is the sole source of project/account aliases, account IDs, regions, enabled collectors, API service names, and auth (profile or STS AssumeRole). Nothing AWS-specific is hardcoded in Python.
6. **`AWSSessionManager`** builds thread-safe Boto3 sessions, refreshes STS temporary credentials, validates identity with GetCallerIdentity (enforcing configured account ID), and creates region-scoped clients with retry/backoff. Permanent access keys are not accepted.
7. **Inventory persistence** stores accounts, inventory runs, resources (keyed by account/region/service/type + ARN or canonical ID), relationships, and collection errors. **`CollectionOrchestrator`** runs paginated collectors with bounded concurrency, retries, upserts, and stale marking (Alembic-managed schema).
8. **Network collectors** (`app.collectors.network`) inventory VPCs, subnets, route tables, IGWs, NAT gateways, VPC endpoints, security groups, NACLs, and ENIs via paginated EC2 Describe* APIs with throttling retries, and emit verified relationship edges into the inventory model.
9. **Compute / ELBv2 collectors** inventory EC2 instances, EBS volumes, Elastic IPs, and ALB/NLB (listeners, target groups, target health) with paginated/batched Describe* calls, preserving status and discovery timestamps.
10. **ECS / EKS collectors** inventory ECS clusters/services/task definitions and EKS clusters/node groups/Fargate profiles via AWS control-plane APIs only (no kubectl). Access failures on individual resources soft-fail without aborting the run.
11. **Data / edge collectors** inventory RDS/Aurora instances & clusters, S3 bucket configuration (region-resolved; no objects), WAFv2 Web ACLs (regional + CloudFront scope), Route 53 hosted zones (record counts only), and CloudWatch log group metadata (never log events). Global services run only in `global_control_region`.
12. **Inventory API** exposes `GET /inventory/counts` and `GET /inventory/resources` with account/region/service/type/stale filters.

## Current surface

- `GET /health` — liveness payload with service name, version, environment, and UTC timestamp.

See also [configuration.md](configuration.md).

