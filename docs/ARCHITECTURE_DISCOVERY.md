# Architecture discovery

How CloudLens turns live AWS metadata into an architecture graph and diagrams, the rules that keep it accurate, and its known limitations. For setup and IAM, see the [README](../README.md).

## Pipeline

```
accounts.yaml ──► discovery runner ──► collectors ──► orchestrator ──► resources table
                  (identity check)     (read-only     (pagination,     (+ declared_edges
                                        Boto3)         retries, stale)    with evidence)
                                                                              │
UI (React Flow) ◄── /graph API ◄── views (app/vpc/service/account) ◄── GraphBuilder (NetworkX)
CLI exports  ◄──────────────────── Mermaid / SVG / JSON ◄───────────────────┘
```

| Stage | Code | Notes |
|---|---|---|
| Runner | `app/discovery/runner.py` | Per account: build a session (profile or AssumeRole), **`GetCallerIdentity` must match `account_id` or nothing is collected**, then run enabled collectors. Regional collectors run per configured Region; global ones run once in `global_control_region`. |
| Collectors | `app/collectors/**`, registry in `app/collectors/registry.py` | One per resource type. Paginated, with throttling retries (botocore standard mode plus jittered backoff). A per-item `AccessDenied` is skipped *and recorded*, never fatal. |
| Orchestrator | `app/collectors/orchestration.py` | Bounded concurrency across Regions; upserts resources; records errors. Marks resources stale **only after a fully successful run**. |
| Storage | `resources.declared_edges` (migration `20261120_0002`) | Every relationship the collector read for a resource, with `evidence` (API field), `collector`, and `confidence`. Kept even if the target wasn't collected yet, so collection order never drops edges. |
| Graph | `app/graph/builder.py` | Resolves edges at read time, applies the DNS/IP matching rules, collapses target-health records into `target-group → target` edges, and derives VPC/subnet placement. |
| Views | `app/graph/views.py` | Selects nodes per view, adds context, builds Account › Region › VPC › Subnet groups, and truncates with a flag. |
| Exports | `app/graph/export.py`, `artifacts.py` | Deterministic Mermaid / SVG / JSON. Private per-project/per-account directories. |
| API / UI | `app/api/routes/graph.py`, `frontend/cloudlens-ui/src/components/architecture/` | Read-only endpoints; React Flow with ELK hierarchical layout. |

## Collectors

| Collector name | Resource type | Main relationships (evidence) |
|---|---|---|
| `network.vpc` / `.subnet` / `.route_table` / `.internet_gateway` / `.nat_gateway` / `.vpc_endpoint` / `.security_group` / `.network_acl` / `.eni` | vpc, subnet, … , network-interface | subnet→vpc, ENI→subnet/SG, NAT→subnet, … (`Describe*` fields) |
| `compute.ec2_instance` / `.ebs_volume` / `.elastic_ip` | instance, volume, elastic-ip | instance→subnet, VPC, ENI, SG, volume |
| `elbv2.load_balancer` / `.listener` / `.listener_rule` / `.target_group` / `.target_health` | load-balancer, listener, listener-rule, target-group, target-health | listener→LB; listener/rule→TG (`DescribeRules:Actions[].TargetGroupArn`, incl. weighted forward); target-health→instance / Lambda / ALB by ID or ARN |
| `ecs.cluster` / `.service` / `.task` / `.task_definition` | ecs-* | service→TG (`loadBalancers[].targetGroupArn`); task→service (`DescribeTasks:group`); task→ENI and subnet (`attachments[].details`) |
| `eks.cluster` / `.node_group` / `.fargate_profile` | eks-* | nodegroup/profile→cluster, subnets. **No workload edges:** the EKS APIs don't expose them. |
| `rds.db_instance` / `.db_cluster` / `.db_subnet_group` | rds-* | instance→subnet group (`DBSubnetGroup.DBSubnetGroupName`); subnet group→subnets, VPC; cluster→members |
| `lambda.function` | lambda-function | VPC config → VPC, subnets, SGs. Environment variables are never stored. |
| `cloudfront.distribution` (global) | cloudfront-distribution | origins recorded; WebACL ARN link |
| `apigateway.rest_api`, `apigatewayv2.api` | apigateway-* | integrations → Lambda ARN / ELB listener ARN (parsed from integration URI) |
| `route53.hosted_zone`, `route53.record` (global) | route53-* | alias A/AAAA and CNAME records only; other record values are never stored |
| `wafv2.web_acl_regional` / `_cloudfront` | wafv2-web-acl | `protects` → ALB / API Gateway stage (`ListResourcesForWebACL`) |
| `s3.bucket`, `logs.log_group` | s3-bucket, logs-log-group | configuration only |
| `kubernetes.workloads` (optional) | k8s-ingress / service / endpointslice / pod | ingress→service (backend), endpointslice→service and →pod (`targetRef`), pod→EC2 instance (`Node.spec.providerID`). **Never from label selectors.** |

## Accuracy rules

1. **No invented resources.** Nodes are stored resources from authorized accounts, or `external` placeholders for an identifier an API record references but CloudLens didn't collect. Placeholders are labelled "not collected".
2. **Every edge has evidence.** Each edge lists the API field and the record (node id, `last_seen_at`) it came from. Edges from older collectors that don't annotate the field name the collector instead; the API field is never guessed.
3. **Target resolution refuses guesses.** A reference is matched by identity key, ARN, ID, or name within the same account, Region, and type. Zero matches means *not collected*. More than one means *ambiguous* and no edge. Both are listed in `coverage.unresolved_references`.
4. **Verified vs inferred.**
   - *Verified:* the identifier appears in the source's API record, or an exact DNS-name match between two API records. That covers CloudFront origin → ALB/S3/API, Route 53 alias → CloudFront/ALB, and Ingress hostname → ALB.
   - *Inferred:* the registered IP target → ENI match on private IP within the target group's VPC. IPs are reused over time, so this is dashed and can be hidden.
5. **No connectivity from shared infrastructure.** Application traversal never passes *through* VPCs, subnets, security groups, NACLs, route tables, gateways, subnet groups, clusters, task definitions, hosted zones, or WAF ACLs. Those appear as context only. In the mocked tests, an EC2 app and an ECS app in the same VPC and security group stay separate.
6. **Security ≠ traffic.** Security group and WAF associations are a separate `security` category, hidden by default. They are not evidence of traffic.
7. **Partial data is labelled, not hidden.** Runs with a failed page or per-item denials are `partial`. Stale marking is skipped for them, and the denied IAM actions are listed under `coverage.missing_permissions`.
8. **Account isolation.** Every query is limited to the accounts of the requested project in `accounts.yaml`. Edges into other accounts are reported as `out_of_scope` and never followed. Requests for a foreign account return 403, and node lookups return 404.

## Graph JSON (schema_version "1")

Defined in `app/graph/models.py`:
- `nodes[]`: id `<account>:<region>:<type>:<identity_key>`, `kind`, `parent_group`, `vpc_id`/`subnet_id`, stale flag, `last_seen_at`.
- `edges[]`: `relationship_type`, `category` (flow / membership / network / security / dependency), `confidence`, `evidence[]`, `attributes` (e.g. target port and health).
- `groups[]`: account / global / region / vpc / subnet. VPCs and subnets render as groups and carry `node_id` for detail lookup.
- `stats` (counts, truncation), `coverage`, and `assumptions`.

The JSON export is the input for repeatable rendering: the same document always produces the same Mermaid and SVG.

## Validation

- `tests/test_architecture_discovery_e2e.py` runs real collectors, runner, orchestrator, SQLite, and graph against mocked Boto3 responses. It covers ALB → ECS Fargate, ALB → EC2 (incl. EC2 → ENI → subnet → VPC), EKS managed node groups, private RDS (with no invented app → DB edge), and CloudFront → ALB. It also checks missing permissions (hard and per-item), identity mismatch, read-only calls, and diagram generation.
- `tests/test_graph_builder.py`: resolution, ambiguity, account isolation, the shared-SG/VPC non-link, WAF, exports, artifact permissions, and the HTTP API.
- `tests/test_discovery_collectors.py`, `tests/test_kubernetes_adapter.py`: collector pagination, soft failures, edges and evidence, global gating, registry, config compatibility.
- `frontend/cloudlens-ui/src/lib/graph.test.ts`: layout (real ELK), filtering, search, edge styles, file names.

## Limitations and not yet implemented

- **Traffic enrichment from VPC Flow Logs / ALB access logs is not implemented.** Every relationship reflects configuration only.
- No real AWS account has been scanned during development. Response handling is tested against mocked responses shaped like the documented Boto3 output.
- PostgreSQL behaviour has not been tested (all tests use SQLite).
- Not covered: API Gateway custom domain names, Route 53 alias targets for S3 website endpoints, Elastic Beanstalk, NLB → ALB chains via Route 53, Global Accelerator, Transit Gateway, VPC peering, PrivateLink consumers in other accounts.
- PNG export is available from the UI only; the server produces SVG.
- The explorer bundle is large (~530 kB gzipped, mostly the ELK layout engine), and it is loaded only when the Architecture tab opens.
- The API has no authentication (audit finding H1).
