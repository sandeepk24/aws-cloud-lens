# Account configuration

AWS CloudLens loads multi-account targets from an **external YAML file**. Python source never hardcodes AWS account IDs, regions, role ARNs, profile names, credentials, or client names.

## Setup

```bash
cp config/accounts.example.yaml config/accounts.yaml
# edit config/accounts.yaml
export CLOUDLENS_CONFIG_PATH=./config/accounts.yaml
```

`CLOUDLENS_CONFIG_PATH` may be absolute or relative to the process working directory.

## Document shape

- **`version`** — currently `"1"`
- **`aws_api.sts_service_name`** — Boto3 service name for STS (config-driven)
- **`aws_api.ec2_service_name`** — Boto3 service name for EC2 / EBS / EIP collectors
- **`aws_api.elbv2_service_name`** — Boto3 service name for ALB/NLB collectors
- **`aws_api.ecs_service_name`** — Boto3 service name for ECS collectors
- **`aws_api.eks_service_name`** — Boto3 service name for EKS collectors (control plane only; no kubectl)
- **`aws_api.rds_service_name`** / **`s3_service_name`** / **`wafv2_service_name`** / **`route53_service_name`** / **`logs_service_name`** — Boto3 service names for data-plane inventory collectors (metadata only)
- **`aws_api.lambda_service_name`** / **`cloudfront_service_name`** / **`apigateway_service_name`** / **`apigatewayv2_service_name`**: optional. Required only when the matching collectors (`lambda.function`, `cloudfront.distribution`, `apigateway.rest_api`, `apigatewayv2.api`) are enabled.
- **`aws_api.global_control_region`** — region used for Route 53 and CloudFront-scope WAFv2
- **`aws_api.s3_default_region`** — region assumed when S3 `LocationConstraint` is empty
- **`aws_api.s3_location_constraint_map`** — optional S3 `LocationConstraint` → region aliases (e.g. `EU`)
- **`projects[]`** — each has an `alias` and one or more `accounts`
- **`accounts[]`** — each has:
  - `alias` — stable name used by CloudLens
  - `account_id` — 12-digit AWS account ID
  - `regions` — list of region codes to collect from
  - `collectors.enabled` — collector identifiers to run
  - `auth` — either profile or assume-role
  - `kubernetes.clusters` (optional): `[{eks_cluster_name, region, kubeconfig_context, kubeconfig_path?}]`, used only by the `kubernetes.workloads` collector

How CloudLens uses these credentials, the IAM policy to grant, and the identity check are explained in the [README](../README.md#how-cloudlens-gets-access-to-your-aws-accounts).

### Profile auth

```yaml
auth:
  type: profile
  profile: your-aws-profile
```

### STS AssumeRole auth

```yaml
auth:
  type: assume_role
  role_arn: "arn:aws:iam::123456789012:role/YourRole"
  external_id: optional
  session_name: optional
  source_profile: optional
  duration_seconds: 3600
  sts_region: optional  # defaults to the account's first region
```

## Validation

- Runtime: Pydantic models in `backend/cloudlens-api/app/accounts/models.py`
- Static / CI: JSON Schema at `config/schemas/accounts.schema.json`

## Security

- Keep real `accounts.yaml` out of Git (ignored by default).
- Prefer SSO profiles or AssumeRole over long-lived access keys.
- Do not put secrets in the YAML beyond what AWS shared config already requires (external ID if used).
