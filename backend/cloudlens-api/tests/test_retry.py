"""Tests for AWS retry / backoff helpers."""

from botocore.exceptions import ClientError, EndpointConnectionError

from app.connectors.aws.retry import call_with_backoff, is_retryable_aws_error


def _client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "boom"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "TestOp",
    )


def test_is_retryable_for_throttling_and_connection() -> None:
    assert is_retryable_aws_error(_client_error("Throttling"))
    assert is_retryable_aws_error(_client_error("InternalError", status=500))
    assert is_retryable_aws_error(EndpointConnectionError(endpoint_url="https://example"))
    assert not is_retryable_aws_error(_client_error("AccessDenied", status=403))


def test_call_with_backoff_retries_then_succeeds() -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def operation() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _client_error("Throttling")
        return "ok"

    result = call_with_backoff(
        operation,
        max_attempts=5,
        base_delay_seconds=1.0,
        sleep=sleeps.append,
        rng=lambda: 0.5,
    )
    assert result == "ok"
    assert attempts["n"] == 3
    assert len(sleeps) == 2
    assert sleeps[0] == 0.5  # 1.0 * 0.5
    assert sleeps[1] == 1.0  # min(8, 2.0) * 0.5
