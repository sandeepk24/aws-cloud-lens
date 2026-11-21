"""CloudWatch Logs inventory collectors (metadata only)."""

from app.collectors.logs.collectors import (
    CloudWatchLogGroupCollector,
    create_logs_collectors,
)

__all__ = ["CloudWatchLogGroupCollector", "create_logs_collectors"]
