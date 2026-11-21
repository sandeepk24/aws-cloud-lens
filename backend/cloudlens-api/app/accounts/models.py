"""Pydantic v2 validation models for external accounts YAML.

All AWS account IDs, regions, role ARNs, profile names, and collector names
must come from configuration — never from hardcoded Python defaults.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

ALIAS_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$"
ACCOUNT_ID_PATTERN = r"^[0-9]{12}$"
ROLE_ARN_PATTERN = r"^arn:[^:]+:iam::[0-9]{12}:role/.+$"


class ProfileAuth(BaseModel):
    """Authenticate with a named AWS shared-config / SSO profile."""

    type: Literal["profile"] = "profile"
    profile: str = Field(min_length=1)


class AssumeRoleAuth(BaseModel):
    """Authenticate by assuming an IAM role via STS."""

    type: Literal["assume_role"] = "assume_role"
    role_arn: str = Field(min_length=20, pattern=ROLE_ARN_PATTERN)
    external_id: str | None = Field(default=None, min_length=1)
    session_name: str | None = Field(default=None, min_length=2, max_length=64)
    source_profile: str | None = Field(default=None, min_length=1)
    duration_seconds: int | None = Field(default=None, ge=900, le=43200)
    sts_region: str | None = Field(default=None, min_length=1)


AuthConfig = Annotated[ProfileAuth | AssumeRoleAuth, Field(discriminator="type")]


class CollectorsConfig(BaseModel):
    """Collectors enabled for an account."""

    enabled: list[str] = Field(default_factory=list)

    @field_validator("enabled")
    @classmethod
    def _non_empty_names(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("collector names must be non-empty strings")
        return cleaned


class KubernetesClusterConfig(BaseModel):
    """Optional read-only Kubernetes workload discovery for one EKS cluster."""

    eks_cluster_name: str = Field(min_length=1)
    region: str = Field(min_length=1)
    kubeconfig_context: str = Field(min_length=1)
    kubeconfig_path: str | None = Field(default=None, min_length=1)


class KubernetesConfig(BaseModel):
    clusters: list[KubernetesClusterConfig] = Field(default_factory=list)


class AccountConfig(BaseModel):
    """Single AWS account target within a project."""

    alias: str = Field(min_length=1, pattern=ALIAS_PATTERN)
    account_id: str = Field(pattern=ACCOUNT_ID_PATTERN)
    regions: list[str] = Field(min_length=1)
    collectors: CollectorsConfig
    auth: AuthConfig
    kubernetes: KubernetesConfig = Field(default_factory=KubernetesConfig)

    @field_validator("regions")
    @classmethod
    def _regions_unique_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [region.strip() for region in value]
        if any(not region for region in cleaned):
            raise ValueError("regions must be non-empty strings")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("regions must be unique within an account")
        return cleaned


class ProjectConfig(BaseModel):
    """Logical project grouping one or more AWS accounts."""

    alias: str = Field(min_length=1, pattern=ALIAS_PATTERN)
    description: str | None = None
    accounts: list[AccountConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_account_aliases(self) -> Self:
        aliases = [account.alias for account in self.accounts]
        if len(aliases) != len(set(aliases)):
            raise ValueError(
                f"duplicate account aliases within project '{self.alias}'"
            )
        return self


class AwsApiConfig(BaseModel):
    """AWS API service names used by connectors (from config, not Python)."""

    sts_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for STS (AssumeRole / GetCallerIdentity).",
    )
    ec2_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for EC2 network inventory APIs.",
    )
    elbv2_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for Elastic Load Balancing v2 APIs.",
    )
    ecs_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for ECS inventory APIs.",
    )
    eks_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for EKS inventory APIs (control plane only).",
    )
    rds_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for RDS/Aurora inventory APIs.",
    )
    s3_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for S3 inventory APIs.",
    )
    wafv2_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for WAFv2 inventory APIs.",
    )
    route53_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for Route 53 inventory APIs.",
    )
    logs_service_name: str = Field(
        min_length=1,
        description="Boto3 service name for CloudWatch Logs inventory APIs.",
    )
    global_control_region: str = Field(
        min_length=1,
        description=(
            "AWS region used for global control-plane APIs "
            "(Route 53, CloudFront-scope WAFv2)."
        ),
    )
    s3_default_region: str = Field(
        min_length=1,
        description=(
            "Region assumed when S3 GetBucketLocation returns an empty "
            "LocationConstraint."
        ),
    )
    lambda_service_name: str | None = Field(
        default=None,
        min_length=1,
        description="Boto3 service name for Lambda (required for lambda.* collectors).",
    )
    cloudfront_service_name: str | None = Field(
        default=None,
        min_length=1,
        description="Boto3 service name for CloudFront (required for cloudfront.*).",
    )
    apigateway_service_name: str | None = Field(
        default=None,
        min_length=1,
        description="Boto3 service name for API Gateway REST APIs (apigateway.*).",
    )
    apigatewayv2_service_name: str | None = Field(
        default=None,
        min_length=1,
        description="Boto3 service name for API Gateway HTTP/WebSocket APIs (apigatewayv2.*).",
    )
    s3_location_constraint_map: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional aliases from S3 LocationConstraint values to region codes "
            "(e.g. EU → eu-west-1)."
        ),
    )


class AccountsConfig(BaseModel):
    """Root document for CLOUDLENS_CONFIG_PATH YAML."""

    version: Literal["1"] = "1"
    aws_api: AwsApiConfig
    projects: list[ProjectConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_project_aliases(self) -> Self:
        aliases = [project.alias for project in self.projects]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate project aliases in accounts configuration")
        return self

    def find_account(
        self, project_alias: str, account_alias: str
    ) -> tuple[ProjectConfig, AccountConfig]:
        for project in self.projects:
            if project.alias != project_alias:
                continue
            for account in project.accounts:
                if account.alias == account_alias:
                    return project, account
        raise KeyError(
            f"account '{account_alias}' not found in project '{project_alias}'"
        )

    def iter_accounts(self) -> list[tuple[ProjectConfig, AccountConfig]]:
        return [
            (project, account)
            for project in self.projects
            for account in project.accounts
        ]
