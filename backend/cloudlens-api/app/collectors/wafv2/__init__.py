"""WAFv2 Web ACL inventory collectors."""

from app.collectors.wafv2.collectors import (
    Wafv2CloudFrontWebAclCollector,
    Wafv2RegionalWebAclCollector,
    create_wafv2_collectors,
)

__all__ = [
    "Wafv2CloudFrontWebAclCollector",
    "Wafv2RegionalWebAclCollector",
    "create_wafv2_collectors",
]
