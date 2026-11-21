"""EKS inventory collectors (AWS control plane only — no kubectl)."""

from app.collectors.eks.collectors import (
    EksClusterCollector,
    EksFargateProfileCollector,
    EksNodeGroupCollector,
    create_eks_collectors,
)

__all__ = [
    "EksClusterCollector",
    "EksFargateProfileCollector",
    "EksNodeGroupCollector",
    "create_eks_collectors",
]
