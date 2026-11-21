# AWS CloudLens — Security, Performance & Accuracy Audit

**Date:** 2026-11-20  **Scope:** entire repository (`backend/cloudlens-api`, `frontend/cloudlens-ui`, `config/`, `docker-compose.yml`, `deployments/`, `tests/`)
**Approach:** read the code, run local tools, benchmark before and after each change. No source code, configuration, or secrets were sent to external services. Dependency audits sent only package names and versions to the vulnerability databases. Semgrep ran with `--metrics=off` and downloaded its rule packs.

---

## 1. Executive summary

| | |
|---|---|
| Findings | **0 Critical · 5 High · 10 Medium · 9 Low** (plus 3 confirmed false positives) |
| Fixed in this pass | 14 (fully or partially). Every code fix has a regression test; infra and config edits are listed as not build-tested. |
| Needs your approval | API authentication design, vite major upgrade, identity-validation gate, Python lockfile |
| Tests | 77 → **94 passing** (17 new). Run against the original code, **9 fail** (the bugs they target), 4 pass (they pin existing correct behavior, such as the cross-account filter), and 4 can't run (they test the new guard and metrics features). |
| Biggest measured wins | Persistence SQL statements **10,088 → 148** per 2,000-resource run. Boto3 client acquisition **~5.4 ms → ~0.15 ms** mean. S3 API calls **423 → 243**. Worst-case HTTP attempts per throttled page **250 → 75** |
| No measured gain | `/inventory/*` API latency was unchanged within noise (see §5). |

**What the system is today.** CloudLens is a FastAPI service with read-only inventory endpoints, a SQLAlchemy store, and a library of paginated AWS collectors. **Nothing in production code runs the collectors yet:** no API route, CLI, or scheduler calls `CollectionOrchestrator`. **The repository contains no LLM, agent, tool-calling, or prompt-handling code.** The brief's agent and prompt-injection items are covered in §4 as requirements for when that code is added. They could not be tested because there is nothing to test.

---

## 2. Tooling: what ran and what did not

| Tool | Status | Result |
|---|---|---|
| pytest (repo `.venv`, Python 3.14.6) | ✅ ran | Before: 77 passed. After: 94 passed. |
| ruff | ✅ ran | Before: 20 issues. After: 0 in files changed here. 6 pre-existing issues in untouched files, plus 5 FastAPI `Depends()` B008 hits (idiomatic; suggest ignoring B008 in route modules). |
| mypy `--strict` | ✅ ran | 75 → 66 errors. No new error classes (diffed). The remaining errors are pre-existing, mainly boto3 stub overloads and `**kwargs` typing in collector factories. |
| `tsc --noEmit` + `vite build` | ✅ ran | Clean before and after. |
| pip-audit 2.x (installed venv) | ✅ ran | Runtime dependencies: **0 known vulnerabilities**. Tooling: `pip 26.1.2` has PYSEC-2026-3721 (fixed in 26.2). |
| npm audit | ✅ ran | **vite ≤6.4.3 (High)** and **esbuild ≤0.24.2 (Moderate)**. Both affect the dev server only, and the fix is vite 8.x (semver-major). |
| Bandit 1.9.4 | ✅ ran | 1 Medium (B104 bind-all), 16 Low (B101 asserts; B105 false positives on pagination constants such as `token_param = "Marker"`). |
| Semgrep (p/python, p/fastapi, p/react, p/typescript, p/dockerfile, p/secrets; metrics off) | ✅ ran | 3 findings: 2 × container runs as root, 1 false positive (the logger call logs only the credential *expiry*). |
| **Gitleaks** | ❌ not installed | Substitute: a local regex sweep for AWS keys, private keys, and tokens. Only AWS documentation example IDs were found. The directory is not a git repo, so there was no history to scan. |
| **Trivy** | ❌ not installed | Container images were not scanned. |
| **Docker / docker compose** | ❌ not installed | Dockerfile, nginx, and compose changes were **not build-tested**. `nginx -t` was not run. |
| PostgreSQL | ❌ not available | Every DB test and benchmark ran on SQLite. The Postgres-specific parameter limit (M3) is reasoned from documentation, not reproduced. |
| Frontend unit tests | ➖ none exist | — |
| Live AWS integration tests | ➖ not run by design | No credentials. A read-only audit should not touch real accounts. |

---

## 3. Findings

Status: **Fixed** · **Partial** · **Open** (needs a decision or approval) · **FP** (false positive)

### High

#### H1 — No authentication or authorization on the API · Open (approval needed)
- **Evidence:** [`app/api/routes/inventory.py`](../../backend/cloudlens-api/app/api/routes/inventory.py) and [`health.py`](../../backend/cloudlens-api/app/api/routes/health.py) have no auth dependency. `GET /inventory/resources` returns `observed_state`, which includes security-group rules, CIDRs, ENI and private IPs, endpoints, ARNs, and tags for every account. `API_HOST` defaults to `0.0.0.0` (Bandit B104), and compose published `8000` on all interfaces.
- **Impact:** anyone who can reach the port can read a full infrastructure map for reconnaissance. There is also no per-caller account scoping: any caller can query any `account_id`.
- **Done now:** compose binds `api`, `ui`, and `db` to `127.0.0.1` only ([`docker-compose.yml`](../../docker-compose.yml)).
- **Remediation (needs a decision):** add an auth dependency on the router: OIDC/JWT behind an ALB/IdP, or at minimum a hashed API key. Map each principal to allowed `project_alias`/`account_id` values and enforce that filter in `_apply_filters`. This changes the API contract, so it was not done without approval. **Escalate to Critical** if the service is deployed anywhere other than a single-user laptop.

#### H2 — Partial scans marked live resources as stale · Fixed
- **Evidence:** the original orchestrator ran `mark_stale_resources` when the status was `SUCCEEDED` **or `PARTIAL`**. A PARTIAL run stops at the first failed page, so resources on later pages were never fetched but were still marked stale.
- **Proof:** the regression test `test_partial_run_does_not_mark_unfetched_resources_stale` fails on the original code with **2 live resources wrongly marked stale**.
- **Fix:** stale marking now runs only on `SUCCEEDED` ([`orchestration.py:261`](../../backend/cloudlens-api/app/collectors/orchestration.py#L261)). Each run records `stats.stale_marking = "applied" | "skipped"` so consumers can tell which happened.

#### H3 — Account identity is never verified before data is labelled with an account ID · Open
- **Evidence:** `AWSSessionManager.validate_identity()` enforces that `GetCallerIdentity.Account` matches the configured `account_id`, but no code path calls it. `validate_on_init` defaults to `False` ([`session.py:111`](../../backend/cloudlens-api/app/connectors/aws/session.py#L111)). Collectors label every resource with the *configured* account ID, not one observed from AWS. `_item_matches_scope` ([`orchestration.py:226`](../../backend/cloudlens-api/app/collectors/orchestration.py#L226)) compares two configured values, so it cannot catch a wrong profile.
- **Impact:** if a profile or role points at the wrong account, that account's inventory is stored under another account's ID. This is a cross-account contamination and accuracy failure.
- **Remediation:** when the collection runner is built, call `validate_identity()` once per session manager before any collector runs, and fail the scope with `AWSAccountMismatchError`. Optionally cross-check the account segment of ARNs in the returned data. This was not wired now because no runner exists, and changing the factory default would add an STS call to every existing caller.
- **Test added:** `test_cross_account_items_are_dropped` pins the existing scope filter.

#### H4 — Retries were stacked three layers deep · Partial
- **Evidence:** botocore `max_attempts=10` × `AwsDescribeCollector._invoke` (5) × orchestrator `_fetch_page_with_retry` (5). A page-level retry also repeats every per-item sub-call on that page, such as the S3 metadata calls or `describe_cluster`.
- **Measured** (benchmark `retry_amplification`, stub client that always throttles): **25 application-level attempts per page**, so the worst case is **250 HTTP requests** for one throttled page.
- **Fixed:** botocore `max_attempts` is now **3**, the AWS SDK default for standard mode ([`session.py:60`](../../backend/cloudlens-api/app/connectors/aws/session.py#L60)). Worst case drops to **75**.
- **Open:** remove one application layer. The recommended approach is to let the orchestrator retry pages only for non-AWS transient errors, or mark `CollectorError` non-retryable after `_invoke` has used its budget. This changes the existing retry semantics and tests, so it needs sign-off.

#### H5 — N+1 queries when persisting relationships · Fixed
- **Evidence:** the original code ran 1 `SELECT` for the source resource plus 1 per edge, for every resource on every page.
- **Measured** (2,000 instances × 3 edges): **8,045 SELECTs / 10,088 statements → 85 / 148**. Wall time 2.75–2.86 s → 1.07–1.20 s.
- **Fix:** `resolve_resource_pks()` and `upsert_relationships_batch()` ([`repository.py:241`](../../backend/cloudlens-api/app/db/repository.py#L241), [`:293`](../../backend/cloudlens-api/app/db/repository.py#L293)) resolve keys in chunks of 500 per (account, region, service, type) via the existing unique index. Duplicate edges within a statement are de-duplicated, because Postgres `ON CONFLICT` rejects touching the same row twice. The single-resource `upsert_relationships()` keeps its signature.

### Medium

| ID | Finding | Evidence | Status / remediation |
|---|---|---|---|
| M1 | A new boto3 client was built on every page, and `boto3.Session.client()` was called concurrently from orchestrator threads. boto3 sessions are not thread-safe. | `session.py` `client()` called `session.client` outside the lock. Measured 3.97–4.12 ms p50 per acquisition. | **Fixed.** Clients are cached per (service, region) and built under the session lock. The cache is cleared on credential refresh ([`session.py:291`](../../backend/cloudlens-api/app/connectors/aws/session.py#L291)). Measured mean 5.4 → 0.15 ms. Tests cover cache, refresh, and 8-thread concurrency. |
| M2 | Read-only access was enforced only by convention: nothing stopped a collector from calling a mutating API. | Every collector operation is `Describe*`/`List*`/`Get*` (enumerated), but there was no runtime guard. | **Fixed.** A botocore `before-call` guard rejects any operation outside `Describe/List/Get` before the request is signed ([`session.py:207`](../../backend/cloudlens-api/app/connectors/aws/session.py#L207)). It is registered first on the most specific pattern so no other hook can short-circuit it (found while testing with Stubber). It also counts calls per operation (`api_call_counts()`). The IAM role should still be read-only (see M10). |
| M3 | Stale marking bound one SQL parameter per seen resource (`NOT IN (...)`). | Measured 40,006 bind parameters for a 40k-resource scope. PostgreSQL's protocol limit is 65,535 parameters, so large scopes (for example log groups) would fail at that size. | **Fixed.** The orchestrator now uses `last_inventory_run_id != run.id`, with a constant 7 parameters. Stale UPDATE time went 46–48 → 14–15 ms. The explicit-key path is kept for existing callers. *Not reproduced on Postgres.* |
| M4 | S3 called `GetBucketLocation` for **every bucket in every configured region**, because `ListBuckets` is global. | Measured 180 calls for 60 buckets × 3 regions. | **Fixed.** Uses the `BucketRegion` field that `ListBuckets` returns, and falls back to `GetBucketLocation` only when it is missing. 423 → 243 calls. |
| M5 | CORS allowed credentials with `allow_methods=["*"]` and `allow_headers=["*"]`. If `API_CORS_ORIGINS` contained `*`, Starlette would reflect any origin with credentials. | [`main.py`](../../backend/cloudlens-api/app/main.py#L38) | **Fixed.** `allow_credentials=False`, GET only, no extra headers. The UI sends neither cookies nor auth headers. |
| M6 | vite ≤6.4.3 (High) and esbuild ≤0.24.2 (Moderate). | npm audit | **Open (approval).** These affect the dev server only and are not in the nginx production bundle. The fix is vite 8.x, a semver-major upgrade. Until then, don't run `vite dev` on untrusted networks; note `host: true` in `vite.config.ts`. |
| M7 | Containers ran as root. The UI image used `npm install` without the lockfile. | Semgrep `missing-user` ×2. `frontend/Dockerfile` | **Partial.** The API image now runs as UID 10001. The UI image uses `npm ci` with `package-lock.json`. nginx still runs as root: switching to `nginx-unprivileged` changes the port (80 → 8080), so it is a compose change for you to approve. *Builds not tested (no Docker).* |
| M8 | No Python lockfile, and every dependency is `>=`. | `pyproject.toml` | **Open.** Builds aren't reproducible, and a malicious or broken upstream release would be picked up automatically. Add `uv lock` or `pip-compile --generate-hashes` and install with `--require-hashes` in the Dockerfile. |
| M9 | Hard-coded Postgres credentials `cloudlens:cloudlens` in compose. | `docker-compose.yml` | **Partial.** Ports are now loopback-only. For anything beyond local development, move the credentials to secrets or `.env`. |
| M10 | No least-privilege IAM policy is documented for the collector role. | `docs/configuration.md` | **Open.** Use the policy in §6, and require `ExternalId` for any cross-organization role (see L4). |

### Low

| ID | Finding | Status |
|---|---|---|
| L1 | No React error boundary: one render error blanks the page. | **Fixed.** [`ErrorBoundary.tsx`](../../frontend/cloudlens-ui/src/components/ErrorBoundary.tsx) wraps `<App/>`. |
| L2 | nginx sends no security headers. | **Fixed** (not tested with `nginx -t`): `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, `server_tokens off`. A CSP is **open** because it must match the deployed `VITE_API_BASE_URL` and the Google Fonts origins. |
| L3 | `/inventory/counts` ran a second `COUNT` for `total`, which could disagree with the buckets under concurrent writes. The response also had no freshness information. | **Fixed.** `total` is now the sum of the buckets. Per-bucket and top-level `oldest_last_seen_at`/`newest_last_seen_at` are added (backward-compatible). |
| L4 | `external_id` is optional for AssumeRole. | **Open.** Require it when the role is in another organization (confused-deputy protection). |
| L5 | One `AccessDenied` on a single load balancer or target group aborts the whole ELBv2 listener or target-health page. Those collectors use `_invoke`, not `_invoke_soft`. | **Open.** [`elbv2/collectors.py:200`](../../backend/cloudlens-api/app/collectors/elbv2/collectors.py#L200), `:410`. Use `_invoke_soft` and an `access_failed` placeholder, as ECS and EKS already do. |
| L6 | `assert` used for runtime invariants (Bandit B101). Asserts are stripped under `python -O`. | **Open.** [`session.py:153`](../../backend/cloudlens-api/app/connectors/aws/session.py#L153), `:185`, `:403`. Replace with explicit `if ...: raise`. |
| L7 | Unbounded `offset` pagination; filter strings have no `max_length`. | **Open.** Move to keyset pagination on the natural-key order, and add `max_length` to query params. |
| L8 | `pip 26.1.2` in the dev venv (PYSEC-2026-3721). | **Open.** Run `.venv/bin/pip install -U pip`. This is tooling only and not a runtime dependency. |
| L9 | No API latency, error, or collector-runtime metrics. | **Fixed.** `GET /metrics` returns per-route p50/p95/max, request counts, and 5xx counts from bounded per-process ring buffers labelled by route template. `InventoryRun.stats` now records pages, `duration_ms`, `fetch_ms`, `persist_ms`, and `stale_marking`. `AWSSessionManager.api_call_counts()` gives per-operation AWS call counts. `/metrics` is unauthenticated like the rest of the API (H1). |

### Confirmed false positives
- **Bandit B105** ×6 (`rds`, `logs` collectors): `token_param = "Marker"` / `"nextToken"` are pagination parameter names, not passwords.
- **Semgrep `python-logger-credential-disclosure`** ([`session.py`](../../backend/cloudlens-api/app/connectors/aws/session.py)): the message logs only project, account alias, and the credential *expiry*. No key material is logged anywhere; checked every `logger.*` call.
- **Secret sweep hits:** `111122223333`, `444455556666`, `123456789012` are AWS documentation example account IDs.

### Checked and found sound
- **SQL injection:** every query uses SQLAlchemy expressions with bound parameters. There is no raw SQL.
- **SSRF:** there are no user-controlled URLs or outbound HTTP calls besides AWS SDK endpoints chosen from config.
- **XSS:** React escapes all output, and there is no `dangerouslySetInnerHTML`.
- **YAML:** `yaml.safe_load`. The accounts config is strictly validated with Pydantic (account ID regex, role-ARN regex, unique aliases).
- **Credentials:** STS temporary credentials live only in memory and are never logged or returned by `describe()`. Permanent keys are rejected by design. `.env`, `accounts.yaml`, `*.pem`, and `.aws/` are gitignored.
- **S3 collector:** it never calls object-data APIs, and the encryption config records the KMS key *ID* only.

---

## 4. Accuracy and hallucination prevention

**No LLM or agent component exists**, so the requirements for prompt-injection defense, typed agent tools, and the "Unable to verify from available AWS data" response cannot be tested yet. What exists today and what is still missing:

| Requirement | Today |
|---|---|
| Counts come from verified API data | ✅ Counts are SQL aggregates over persisted collector observations. Nothing is estimated. `total` is now derived deterministically from the buckets. |
| Source, account, region, timestamp | ✅ Every `Resource` row has `source`, `collector_version`, `aws_account_id`, `region`, `discovered_at`, `last_seen_at`, and `last_inventory_run_id`. ⚠️ The account ID is the *configured* one, not a verified one (H3). |
| Observed / unavailable / stale distinguished | ✅ Partly. ECS and EKS write `status="access_failed"` placeholders when a describe call is denied, and `is_stale`/`stale_at` mark absence. **H2 fixed the case where absence was inferred from an incomplete scan.** ❌ There is no explicit `observed \| inferred \| unknown \| unavailable` field. Relationships are only persisted when both endpoints were observed; the code never invents edges, and a test asserts that missing targets are skipped. |
| Scan completeness reported | ⚠️ `InventoryRun.status` (succeeded/partial/failed), `error_count`, and `collection_errors` are persisted but **not exposed by any API**. A consumer can't tell that a count came from a partial run. *Recommended next step:* `GET /inventory/runs` and a `coverage` block on `/inventory/counts` showing the latest run status per (account, region, collector). |
| Costs, utilization, savings | ➖ Not implemented. No cost or CloudWatch-metrics collectors exist, so there is nothing to fabricate. |

**Requirements for the future agent layer** (none of this exists yet):
1. Tools are typed Pydantic functions over the read-side query layer (`inventory_queries`). They never call boto3 and never run free-form SQL. Every tool has a hard `limit` and a timeout.
2. The authorization context (allowed accounts from H1) is injected server-side into every tool call. The model can't widen it, and a request for an account outside that set returns a refusal.
3. Tool outputs return rows with `source`, `aws_account_id`, `region`, `last_seen_at`, `run_status`. The model may only restate or format those values. All arithmetic, totals, and savings are computed in Python and passed in.
4. When the evidence is empty, stale, or comes from a partial run, return the fixed string `Unable to verify from available AWS data`. That decision is made in code, not by the model.
5. Treat resource tags, names, and descriptions as untrusted data, because they are attacker-controllable. Put them in delimited data fields, never in instructions. Test with prompt-injection payloads in tag values.
6. Required tests: missing metrics, stale inventories, partial scans (reuse H2's fixture), contradictory sources, injected tags, and cross-account requests.

---

## 5. Benchmarks (before → after)

Harness: [`backend/cloudlens-api/benchmarks/bench_audit.py`](../../backend/cloudlens-api/benchmarks/bench_audit.py). Raw JSON is in [`benchmarks/`](benchmarks/). Environment: macOS (Darwin 24.6), Python 3.14.6, SQLAlchemy 2.1.9, file-backed SQLite, no network. "Before" ran against an unmodified copy of the original `app/`. Each version ran twice, back to back, on an otherwise idle machine. Ranges show the two runs.

| Scenario | Before (run 1 / run 2) | After (run 1 / run 2) | Change |
|---|---|---|---|
| Persist 2,000 instances × 3 edges: SQL statements | 10,088 / 10,088 | 148 / 148 | **−98.5%** |
| … SELECT statements | 8,045 / 8,045 | 85 / 85 | **−98.9%** |
| … wall time | 2,857 / 2,750 ms | 1,068 / 1,202 ms | **~−59%** |
| Stale UPDATE, 40k-resource scope: bind params | 40,006 | 7 | constant |
| … UPDATE time | 48.0 / 45.9 ms | 14.8 / 14.0 ms | ~−69% |
| … full 40-page run | 6,817 / 6,757 ms | 6,817 / 6,775 ms | no change (dominated by the resource upserts) |
| Boto3 client acquisition ×100: mean | 5.52 / 5.39 ms | 0.146 / 0.151 ms | **~−97%** |
| … p50 / p95 | 4.12 / 6.67 ms | 0.000 / 0.001 ms | cached |
| S3, 60 buckets × 3 regions: AWS API calls | 423 | 243 | **−180 calls** |
| Persistent throttling: worst-case HTTP per page | 250 (25 × 10) | 75 (25 × 3) | −70% |
| `GET /inventory/counts` (20k rows): p50 / p95 | 32.9 / 55.0 · 32.9 / 53.4 ms | 33.7 / 58.5 · 34.1 / 59.6 ms | **no improvement, ≈ +1 ms p50** |
| `GET /inventory/counts?account_id&service`: p50 / p95 | 8.85 / 10.25 · 8.83 / 10.35 ms | 8.99 / 12.40 · 8.85 / 10.16 ms | within noise |
| `GET /inventory/resources?limit=100`: p50 / p95 | 4.39 / 5.23 · 4.48 / 5.06 ms | 4.47 / 5.05 · 4.50 / 5.33 ms | within noise |
| `GET /inventory/resources?service=logs&offset=2000`: p50 / p95 | 12.2 / 15.0 · 12.2 / 16.8 ms | 12.3 / 16.8 · 12.3 / 17.8 ms | within noise |

**How to read this:**
- The API endpoints did **not** get faster. Dropping the second `COUNT` from `/inventory/counts` was offset by the new `MIN`/`MAX(last_seen_at)` freshness aggregates and the metrics middleware. The net is about +1 ms p50 and +4–5 ms p95 on unfiltered counts, at or near the noise floor. That trade buys consistent totals and freshness reporting.
- To actually speed up unfiltered counts, add a covering index on `(aws_account_id, region, service, resource_type, is_stale, last_seen_at)` (needs an Alembic migration), or precompute counts per run. Neither has been benchmarked.
- The collector-path numbers are what the change affects most. The AWS-side numbers (S3 calls, retry attempts) are counts against stubs. Real AWS latency was not measured.
- SQLite numbers will differ from PostgreSQL. Statement and parameter counts carry over across databases; wall-clock times won't.

---

## 6. Recommended least-privilege IAM policy (collector role)

> **Superseded:** architecture discovery added collectors (listener rules, ECS tasks, Lambda, DB subnet groups, CloudFront, API Gateway, Route 53 records). The current, complete policy and trust policy are in the [README](../../README.md#the-iam-role-to-create-in-each-account).

Derived by listing every operation the collectors call. Pair it with a trust policy that requires `sts:ExternalId` for cross-organization use.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "CloudLensInventoryReadOnly",
    "Effect": "Allow",
    "Action": [
      "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeRouteTables",
      "ec2:DescribeInternetGateways", "ec2:DescribeNatGateways", "ec2:DescribeVpcEndpoints",
      "ec2:DescribeSecurityGroups", "ec2:DescribeNetworkAcls", "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeAddresses",
      "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeListeners",
      "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:DescribeTargetHealth",
      "ecs:ListClusters", "ecs:DescribeClusters", "ecs:ListServices", "ecs:DescribeServices",
      "ecs:ListTaskDefinitions", "ecs:DescribeTaskDefinition",
      "eks:ListClusters", "eks:DescribeCluster", "eks:ListNodegroups", "eks:DescribeNodegroup",
      "eks:ListFargateProfiles", "eks:DescribeFargateProfile",
      "rds:DescribeDBInstances", "rds:DescribeDBClusters",
      "s3:ListAllMyBuckets", "s3:GetBucketLocation", "s3:GetBucketVersioning",
      "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketOwnershipControls",
      "wafv2:ListWebACLs", "wafv2:GetWebACL", "wafv2:ListResourcesForWebACL",
      "route53:ListHostedZones", "route53:GetHostedZone",
      "logs:DescribeLogGroups",
      "sts:GetCallerIdentity"
    ],
    "Resource": "*"
  }]
}
```
Notes:
- `ecs:DescribeTaskDefinition` with `include=["TAGS"]` also needs `ecs:ListTagsForResource` in some partitions; check this against your accounts.
- Do **not** use the AWS-managed `ReadOnlyAccess` policy. It grants data-plane reads such as `s3:GetObject`, `secretsmanager:GetSecretValue`, and `dynamodb:Scan`, which CloudLens never needs.

---

## 7. Changes made

| File | Change |
|---|---|
| `backend/cloudlens-api/app/collectors/orchestration.py` | Stale marking only on SUCCEEDED; batched relationship upsert; run-ID stale predicate; `run.stats` timing |
| `backend/cloudlens-api/app/db/repository.py` | `resolve_resource_pks`, `upsert_relationships_batch`, `mark_stale_resources(seen_identity_keys=None)` mode |
| `backend/cloudlens-api/app/connectors/aws/session.py`, `errors.py`, `__init__.py` | Client cache and lock, read-only guard, per-operation call counts, botocore retries 10 → 3, `AWSReadOnlyViolationError` |
| `backend/cloudlens-api/app/collectors/s3/collectors.py` | Use `BucketRegion` from `ListBuckets` |
| `backend/cloudlens-api/app/collectors/aws_api.py` | Explicit re-exports (fixes 3 mypy errors) |
| `backend/cloudlens-api/app/main.py`, `app/core/metrics.py` (new), `app/api/routes/health.py` | CORS tightened; latency middleware; `GET /metrics` |
| `backend/cloudlens-api/app/api/routes/inventory.py`, `app/db/inventory_queries.py`, `app/models/schemas.py` | `total` derived from buckets; freshness fields (additive) |
| `backend/cloudlens-api/tests/test_audit_regressions.py` (new) | 17 regression tests |
| `backend/cloudlens-api/benchmarks/bench_audit.py` (new) | Benchmark harness |
| `frontend/cloudlens-ui/src/components/ErrorBoundary.tsx` (new), `src/main.tsx` | Error boundary |
| `frontend/cloudlens-ui/Dockerfile`, `nginx.conf` | `npm ci` with lockfile; security headers |
| `backend/cloudlens-api/Dockerfile` | Non-root UID 10001 |
| `docker-compose.yml` | Loopback-only port bindings |

**API compatibility:** all response changes are additive. The `CORS` change rejects cross-origin non-GET and credentialed requests; the API has no non-GET routes and the UI sends no credentials. `mark_stale_resources` and `upsert_relationships` keep their signatures.

---

## 8. Remaining risk register

| # | Risk | Likelihood | Impact | Owner action |
|---|---|---|---|---|
| R1 | Unauthenticated inventory API exposed beyond localhost (H1) | Medium | High | Choose an auth model (OIDC or API key) and add per-principal account scoping |
| R2 | Wrong-account credentials store data under another account's ID (H3) | Low–Med | High | Call `validate_identity()` in the future collection runner, before any collector |
| R3 | Throttling storms amplified by nested retries (H4) | Medium | Medium | Approve removing one application retry layer |
| R4 | Dev-server vulnerabilities in vite/esbuild (M6) | Low | Medium | Approve the vite 8 upgrade; until then, don't expose `vite dev` |
| R5 | Unpinned supply chain (M8) | Low | High | Add a hashed lockfile |
| R6 | Docker, nginx, and compose edits not build-tested | Medium | Low | Run `docker compose build && docker compose up` and `nginx -t` |
| R7 | Behavior on PostgreSQL unverified (upsert de-dup, stale predicate, param limits) | Low | Medium | Run the test suite against Postgres 16 in CI |
| R8 | Partial-run status not visible to API consumers (§4) | Medium | Medium | Expose run coverage on `/inventory/counts` |
| R9 | Future LLM layer could invent data without the §4 guardrails | — | High | Treat §4 as acceptance criteria for that work |
| R10 | Images not scanned (no Trivy) and no history secret scan (no Gitleaks, no git) | — | Medium | Add Trivy and Gitleaks to CI once the repo is under git |

---

## 9. How to reproduce
```bash
# tests (from repo root)
backend/cloudlens-api/.venv/bin/python -m pytest -q
# lint / types
cd backend/cloudlens-api && .venv/bin/ruff check app tests && .venv/bin/mypy app
# benchmarks
.venv/bin/python benchmarks/bench_audit.py --label after --out ../../docs/audits/benchmarks/after.json
# frontend
cd frontend/cloudlens-ui && npx tsc --noEmit && npm audit
```
