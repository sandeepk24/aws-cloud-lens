"""Resource collectors — inventory gathering over cloud connectors."""

from app.collectors.base import (
    BaseCollector,
    CollectionResult,
    CollectorRegistry,
    PaginatedCollector,
)
from app.collectors.compute import create_compute_collectors
from app.collectors.ecs import create_ecs_collectors
from app.collectors.eks import create_eks_collectors
from app.collectors.elbv2 import create_elbv2_collectors
from app.collectors.logs import create_logs_collectors
from app.collectors.network import create_network_collectors
from app.collectors.normalized import (
    CollectorError,
    NormalizedResource,
    ResourceEdge,
    ResourcePage,
    build_identity_key,
)
from app.collectors.orchestration import (
    CollectionOrchestrator,
    CollectionScope,
    OrchestrationResult,
    OrchestratorConfig,
    ScopeResult,
)
from app.collectors.rds import create_rds_collectors
from app.collectors.route53 import create_route53_collectors
from app.collectors.s3 import create_s3_collectors
from app.collectors.wafv2 import create_wafv2_collectors

__all__ = [
    "BaseCollector",
    "CollectionOrchestrator",
    "CollectionResult",
    "CollectionScope",
    "CollectorError",
    "CollectorRegistry",
    "NormalizedResource",
    "OrchestrationResult",
    "OrchestratorConfig",
    "PaginatedCollector",
    "ResourceEdge",
    "ResourcePage",
    "ScopeResult",
    "build_identity_key",
    "create_compute_collectors",
    "create_ecs_collectors",
    "create_eks_collectors",
    "create_elbv2_collectors",
    "create_logs_collectors",
    "create_network_collectors",
    "create_rds_collectors",
    "create_route53_collectors",
    "create_s3_collectors",
    "create_wafv2_collectors",
]
