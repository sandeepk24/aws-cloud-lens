# AWS CloudLens

AWS CloudLens discovers what is **already running** in your AWS accounts and turns it into a searchable inventory and accurate, interactive architecture diagrams.

It only **reads** from AWS. It never creates, changes, or deletes AWS resources.

```
 Your AWS accounts                      CloudLens
 ┌───────────────────┐  read-only API  ┌──────────────┐   ┌──────────┐   ┌──────────────────────┐
 │ VPC, ALB, ECS,    │ ──────────────► │  collectors  │──►│ inventory│──►│ graph + diagrams      │
 │ EKS, RDS, Lambda, │  (Describe/List │  (per Region)│   │ database │   │ UI · Mermaid · SVG ·  │
 │ CloudFront, ...   │   /Get only)    └──────────────┘   └──────────┘   │ PNG · JSON · REST API │
 └───────────────────┘                                                    └──────────────────────┘
```

---

## What it does

### 1. Discovers resources across accounts and Regions

For every account in your configuration, CloudLens calls the AWS service APIs in each configured Region. It handles pagination, throttling with jittered backoff, and global services, and stores what it finds:

| Area | Resources |
|---|---|
| Networking | VPCs, subnets, route tables, internet / NAT gateways, VPC endpoints, security groups, network ACLs, network interfaces (ENIs) |
| Load balancing | ALB / NLB, listeners, listener rules, target groups, registered targets and their health |
| Containers | ECS clusters, services, tasks, task definitions; EKS clusters, managed node groups, Fargate profiles |
| Compute | EC2 instances, EBS volumes, Elastic IPs, Lambda functions |
| Data | RDS / Aurora instances and clusters, DB subnet groups, S3 buckets (configuration only, never objects) |
| Edge and DNS | CloudFront distributions, API Gateway (REST and HTTP/WebSocket), WAF web ACLs, Route 53 hosted zones and alias/CNAME records |
| Kubernetes (optional) | Ingresses, Services, EndpointSlices and Pods, read from the cluster's own API |
| Logging | CloudWatch log groups |

Each resource is stored with:
- its AWS ID and ARN, account and Region;
- the time it was first and last seen;
- the collector that observed it;
- the relationships its API record states (for example, "this instance is in subnet X").

### 2. Builds a relationship graph using only evidence

Relationships come **only** from AWS API data. Examples:

```
Route 53 alias ─► CloudFront ─► ALB ─► Listener ─► Rule ─► Target group ─► Target (EC2 / IP / Lambda)
ECS task ─► ECS service ─► Target group          EC2 ─► ENI ─► Subnet ─► VPC
RDS ─► DB subnet group ─► Subnets ─► VPC          WAF ─► protects ─► ALB / API / CloudFront
```

- **Verified** (solid line): the target's ID appears in the source resource's API record, or two API records name the same DNS endpoint.
- **Inferred** (dashed line): derived by a rule that could be wrong. Today the only such rule matches a load balancer's IP target to the ENI that had that private IP when it was collected.
- **External** node: something an API record points to but CloudLens didn't collect, such as a non-AWS CloudFront origin or an IP target with no matching ENI.
- **Never inferred:** connectivity from sharing a VPC, subnet, or security group. Security group and WAF links show configuration, not traffic.

Every edge carries its evidence: the API field, the collector, and when it was observed.

### 3. Shows interactive architecture diagrams

The **Architecture** tab in the dashboard:
- groups resources as Account → Region → VPC → Subnet;
- lays them out automatically;
- supports zoom, pan, search, filters, resource counts, and a detail panel per resource with its raw API data and edge evidence.

Views keep large accounts readable:

| View | Shows |
|---|---|
| **Application** | One request path, starting from an entry point (Route 53 record, CloudFront, API Gateway, load balancer, Ingress) or a tag such as `Application=shop` |
| **VPC** | Everything placed in one VPC, plus what directly fronts it |
| **Service** | One AWS service (e.g. all ECS), plus its direct neighbours |
| **Account** | Everything, capped at a node limit and flagged when truncated |

### 4. Exports diagrams

Mermaid (`.mmd`), SVG, and JSON (the full graph, for repeatable rendering) are available from the UI, the API, and the CLI. PNG is available from the UI.

The CLI writes files to a private folder, separated by project and account: `<CLOUDLENS_EXPORT_DIR>/<project>/<account_id>/`. Folders are `0700` and files `0600`. Every export includes the discovery time, coverage, assumptions, and **missing permissions**.

### 5. Reports what it could not see

If a permission is missing or an API call fails, CloudLens records it. Coverage shows:
- which collectors succeeded, partially succeeded, or failed;
- the exact **IAM action** that was denied (for example `elasticloadbalancing:DescribeTargetHealth`);
- references it couldn't resolve;
- the age of the data.

When a run is partial, resources are not marked as deleted, so an incomplete scan never makes live resources look gone.

---

## How CloudLens gets access to your AWS accounts

**CloudLens has no special or built-in access.** It can see exactly what the IAM credentials you give it allow, and nothing more. Access is configured per account in `config/accounts.yaml`, in one of two ways.

### Option A: AssumeRole (recommended for multiple accounts)

You create a **read-only IAM role** in each account you want to scan (for example `CloudLensReadOnly`). CloudLens starts from one identity and calls STS `AssumeRole` into each account's role, which gives it **temporary credentials**, typically valid for an hour. Permanent access keys are never used or stored.

```
             CloudLens host identity
   (SSO profile, EC2 instance role, ECS task role, or source_profile)
                        │  sts:AssumeRole (+ ExternalId)
          ┌─────────────┼──────────────┐
          ▼             ▼              ▼
   Account 1111…   Account 2222…   Account 3333…
   CloudLensReadOnly CloudLensReadOnly CloudLensReadOnly
```

```yaml
auth:
  type: assume_role
  role_arn: "arn:aws:iam::111122223333:role/CloudLensReadOnly"
  external_id: "a-long-random-string"   # strongly recommended
  source_profile: cloudlens-hub          # optional; otherwise the default AWS credential chain
  duration_seconds: 3600
```

### Option B: Named profile (good for a single account or a laptop)

CloudLens uses an AWS CLI / SSO profile you have already logged into (`aws sso login --profile …`).

```yaml
auth:
  type: profile
  profile: my-readonly-sso-profile
```

### Safeguards in either mode

1. **Identity check before collecting.** CloudLens calls `sts:GetCallerIdentity`, and if the live account ID doesn't match the configured `account_id`, it collects **nothing** for that account. A mis-pointed profile can't store one account's resources under another's ID.
2. **Read-only enforced twice.**
   - The IAM policy below grants only `Describe*`, `List*`, and `Get*` actions. This is the real control.
   - Inside CloudLens, every AWS client rejects any operation that isn't `Describe*`, `List*`, or `Get*` *before* the request is signed.
3. **No data-plane reads.** CloudLens never reads S3 objects, secrets, parameters, Lambda environment variables, or Kubernetes Secrets or ConfigMaps.
4. **Account isolation in the API.** The API only returns accounts that belong to the requested project in your configuration, and requests for other accounts are rejected.

### The IAM role to create in each account

**Permissions policy** (least privilege; exactly the calls the collectors make):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudLensInventoryReadOnly",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeVpcs", "ec2:DescribeSubnets", "ec2:DescribeRouteTables",
        "ec2:DescribeInternetGateways", "ec2:DescribeNatGateways", "ec2:DescribeVpcEndpoints",
        "ec2:DescribeSecurityGroups", "ec2:DescribeNetworkAcls", "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeInstances", "ec2:DescribeVolumes", "ec2:DescribeAddresses",
        "elasticloadbalancing:DescribeLoadBalancers", "elasticloadbalancing:DescribeListeners",
        "elasticloadbalancing:DescribeRules", "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:DescribeTargetHealth",
        "ecs:ListClusters", "ecs:DescribeClusters", "ecs:ListServices", "ecs:DescribeServices",
        "ecs:ListTasks", "ecs:DescribeTasks", "ecs:ListTaskDefinitions", "ecs:DescribeTaskDefinition",
        "eks:ListClusters", "eks:DescribeCluster", "eks:ListNodegroups", "eks:DescribeNodegroup",
        "eks:ListFargateProfiles", "eks:DescribeFargateProfile",
        "rds:DescribeDBInstances", "rds:DescribeDBClusters", "rds:DescribeDBSubnetGroups",
        "s3:ListAllMyBuckets", "s3:GetBucketLocation", "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration", "s3:GetBucketPublicAccessBlock",
        "s3:GetBucketOwnershipControls",
        "lambda:ListFunctions",
        "cloudfront:ListDistributions",
        "wafv2:ListWebACLs", "wafv2:GetWebACL", "wafv2:ListResourcesForWebACL",
        "route53:ListHostedZones", "route53:GetHostedZone", "route53:ListResourceRecordSets",
        "logs:DescribeLogGroups",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    },
    {
      "Sid": "CloudLensApiGatewayRead",
      "Effect": "Allow",
      "Action": "apigateway:GET",
      "Resource": [
        "arn:aws:apigateway:*::/restapis", "arn:aws:apigateway:*::/restapis/*",
        "arn:aws:apigateway:*::/apis", "arn:aws:apigateway:*::/apis/*"
      ]
    }
  ]
}
```

**Trust policy**, which lets only your CloudLens identity assume the role:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::<HUB_ACCOUNT_ID>:role/<CloudLensHostRole>" },
    "Action": "sts:AssumeRole",
    "Condition": { "StringEquals": { "sts:ExternalId": "<your-external-id>" } }
  }]
}
```

Notes:
- **Do not** attach the AWS-managed `ReadOnlyAccess` policy. It also allows reading data such as S3 objects and DynamoDB items, which CloudLens never needs.
- You can grant less. Collectors without permission are reported under **missing permissions**, and everything else still works.
- With AWS Organizations, you can deploy the role to every account with a CloudFormation StackSet. CloudLens itself never creates it.

---

## How to use it

### Prerequisites
- Python 3.12+ and Node.js 20+
- AWS credentials for the "hub" identity (SSO profile or a host role) and the read-only role in each target account (see above)
- PostgreSQL 16 (recommended) or SQLite for local use

### 1. Install

```bash
# Backend
cd backend/cloudlens-api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Optional, only for Kubernetes workload discovery:
pip install kubernetes

# Frontend
cd ../../frontend/cloudlens-ui
npm ci
```

### 2. Configure

```bash
cp .env.example .env                                   # DATABASE_URL, CLOUDLENS_CONFIG_PATH, CLOUDLENS_EXPORT_DIR
cp config/accounts.example.yaml config/accounts.yaml   # gitignored
```

In `config/accounts.yaml`, list your projects and accounts, their Regions, the collectors to run, and how to authenticate:

```yaml
version: "1"
aws_api:
  sts_service_name: sts
  ec2_service_name: ec2
  # ... (copy the full block from accounts.example.yaml)
  global_control_region: us-east-1   # Route 53, CloudFront and CloudFront-scope WAF are global
  s3_default_region: us-east-1

projects:
  - alias: shop
    accounts:
      - alias: prod
        account_id: "111122223333"
        regions: [us-east-1, eu-west-1]
        collectors:
          enabled: [network.vpc, network.subnet, network.eni, elbv2.load_balancer,
                    elbv2.listener, elbv2.listener_rule, elbv2.target_group,
                    elbv2.target_health, ecs.cluster, ecs.service, ecs.task,
                    rds.db_instance, rds.db_subnet_group, cloudfront.distribution,
                    route53.hosted_zone, route53.record]
        auth:
          type: assume_role
          role_arn: "arn:aws:iam::111122223333:role/CloudLensReadOnly"
          external_id: "a-long-random-string"
```

All available collector names are listed in [`config/accounts.example.yaml`](config/accounts.example.yaml). The full reference is [docs/configuration.md](docs/configuration.md).

> The backend reads `.env` from the directory you run commands in, and relative paths in it resolve from there. Since the commands below run from `backend/cloudlens-api`, either copy `.env` into that folder, or load it into your shell and use absolute paths:
>
> ```bash
> cd backend/cloudlens-api
> set -a; source ../../.env; set +a
> export CLOUDLENS_CONFIG_PATH="$(cd ../.. && pwd)/config/accounts.yaml"
> ```

### 3. Create the database tables

```bash
cd backend/cloudlens-api
alembic upgrade head
```

### 4. Discover your architecture

```bash
# All accounts in a project
python -m app.discovery collect --project shop

# One account, a subset of collectors / Regions
python -m app.discovery collect --project shop --account prod \
    --collector elbv2.load_balancer --collector elbv2.listener --region us-east-1
```

The command:
1. checks the identity of every account;
2. runs the enabled collectors (regional ones in each configured Region, global ones once);
3. prints a JSON report per account: identity verified, each collector's status, and any errors.

It exits non-zero if anything failed or was partial. Run it on a schedule (cron, EventBridge plus ECS, etc.) to keep the inventory fresh. Resources not seen in a later *complete* run are marked stale.

### 5. Explore in the dashboard

```bash
# terminal 1
cd backend/cloudlens-api && uvicorn app.main:app --host 127.0.0.1 --port 8000
# terminal 2
cd frontend/cloudlens-ui && npm run dev
```

Open <http://localhost:5173> and choose **Architecture**:

1. Pick the **project** (and optionally the account and Region).
2. Pick a **view**: Application (choose an entry point or app tag), VPC, Service, or Account.
3. Use **search** to jump to a resource by name, ID, or ARN. Click any resource, or a VPC or subnet box, to see its details and the evidence behind each relationship.
4. Toggle **inferred** edges, **security** associations, **stale** resources, and **full detail** (shows route tables, NACLs, volumes, task definitions, and log groups).
5. Check the **Coverage** panel for missing permissions, failed collectors, and data age.
6. Use **Export** to download Mermaid, SVG, or JSON from the server, or PNG or SVG of exactly what's on screen.

Official AWS architecture icons are not bundled. To use them, download the AWS Architecture Icons package yourself and follow [frontend/cloudlens-ui/README.md](frontend/cloudlens-ui/README.md). Otherwise, neutral service badges are shown.

### 6. Export diagrams from the command line

```bash
python -m app.discovery export --project shop --view vpc --vpc-id vpc-0abc123
python -m app.discovery export --project shop --view application --app-tag Application=checkout
python -m app.discovery export --project shop --view service --service ecs --format mmd
python -m app.discovery export --project shop --account-id 111122223333 --view account --verified-only
```

Files are written to `$CLOUDLENS_EXPORT_DIR/<project>/<account_id>/<view>-<qualifier>-<UTC time>.{json,mmd,svg}`.

### 7. Use the REST API

All endpoints are read-only `GET`s. Interactive docs are at <http://localhost:8000/docs>.

| Endpoint | Purpose |
|---|---|
| `/graph/projects` | Projects, accounts, VPCs, services, entry points, and app tags (for pickers) |
| `/graph?project=…&view=…` | Graph for a view, with coverage and assumptions. Options: `account_id`, `region`, `vpc_id`, `service`, `seed`, `app_tag`, `include_inferred`, `include_stale`, `detail`, `max_nodes` |
| `/graph/node?project=…&id=…` | One resource: raw API data, inbound and outbound edges with evidence |
| `/graph/export/mermaid`, `/svg`, `/json` | Same parameters as `/graph`; returns a download |
| `/inventory/counts`, `/inventory/resources` | Flat inventory queries with freshness |
| `/metrics` | API p50/p95 latency and error counts |
| `/health` | Liveness |

### Optional: Kubernetes workloads on EKS

The EKS APIs don't expose workloads, so CloudLens can additionally read Ingresses, Services, EndpointSlices, and Pods from the cluster itself:
1. Install the `kubernetes` Python package.
2. Grant the read-only ClusterRole documented in `backend/cloudlens-api/app/collectors/kubernetes/adapter.py` (`get`/`list` only, no Secrets).
3. Add `kubernetes.clusters` and the `kubernetes.workloads` collector to the account in `accounts.yaml`.

Pods are linked to EC2 nodes via `Node.spec.providerID`, and Ingresses to their ALB by exact DNS name.

---

## Security notes (read before deploying)

- **The API has no authentication yet.** Anyone who can reach port 8000 can read your inventory. Keep it on `127.0.0.1` (Docker Compose already binds to localhost) or put it behind an authenticating proxy. See finding H1 in [docs/audits/SECURITY_PERFORMANCE_AUDIT.md](docs/audits/SECURITY_PERFORMANCE_AUDIT.md).
- Keep `config/accounts.yaml`, `.env`, and the export directory private. They are gitignored.
- Inventory contains sensitive configuration (security group rules, IPs, endpoints). Protect the database accordingly.

---

## Repository layout

```
aws-cloud-lens/
├── backend/cloudlens-api/
│   ├── app/collectors/     # one module per AWS service + optional Kubernetes adapter
│   ├── app/connectors/     # AWS sessions: profile / AssumeRole, identity check, read-only guard
│   ├── app/discovery/      # discovery runner + CLI (python -m app.discovery)
│   ├── app/graph/          # NetworkX graph builder, views, Mermaid/SVG/JSON exports
│   ├── app/api/            # FastAPI routes (/graph, /inventory, /metrics, /health)
│   ├── app/db/             # SQLAlchemy models, repository, Alembic migrations
│   └── benchmarks/         # reproducible performance benchmarks
├── frontend/cloudlens-ui/  # React + Vite + React Flow architecture explorer
├── config/                 # accounts example + JSON schema
├── docs/                   # architecture, configuration, discovery, audit
└── deployments/            # Docker
```

## Tests

```bash
# from the repo root: backend unit + mocked end-to-end discovery tests
backend/cloudlens-api/.venv/bin/python -m pytest -q
# frontend
cd frontend/cloudlens-ui && npm test && npx tsc --noEmit
```

The end-to-end tests run the real collectors, runner, database, and graph against mocked AWS responses for five reference architectures (ALB → ECS Fargate, ALB → EC2, EKS with managed node groups, private RDS, CloudFront → ALB). No real AWS account is contacted.

## Documentation

- [Architecture discovery: how it works, accuracy rules, limitations](docs/ARCHITECTURE_DISCOVERY.md)
- [Account configuration](docs/configuration.md)
- [Getting started](docs/getting-started.md)
- [System architecture](docs/architecture.md)
- [Security and performance audit](docs/audits/SECURITY_PERFORMANCE_AUDIT.md)

## License

Proprietary. All rights reserved unless otherwise stated.
