"""AWS Lambda function inventory collectors (configuration metadata only)."""

from app.collectors.lambda_functions.collectors import LambdaFunctionCollector

__all__ = ["LambdaFunctionCollector"]
