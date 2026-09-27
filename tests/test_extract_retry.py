"""Retry behaviour: retry transient failures a bounded number of times, never retry permanent ones."""
import logging

import pytest
import requests

from pipeline.extract import NonRetryableAPIError, RetryableAPIError, request_page

LOG = logging.getLogger("test")


class FakeResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, *args, **kwargs):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def call(session, sleeps, max_retries=3):
    return request_page(session, "http://x", 1, 50, max_retries=max_retries, base_seconds=1, timeout=1,
                        retryable_status={429, 500, 502, 503, 504}, logger=LOG, sleep=sleeps.append)


def test_transient_500_is_retried_then_succeeds():
    sleeps = []
    s = FakeSession([FakeResponse(500), FakeResponse(200, {"data": [1]})])
    assert call(s, sleeps) == {"data": [1]}
    assert s.calls == 2 and sleeps == [1]                     # one backoff wait


def test_429_honours_retry_after_from_server():
    sleeps = []
    s = FakeSession([FakeResponse(429, {"retry_after_seconds": 7}), FakeResponse(200, {"ok": True})])
    call(s, sleeps)
    assert sleeps == [7.0]


def test_exponential_backoff_and_bounded_retries():
    sleeps = []
    s = FakeSession([FakeResponse(503)] * 3)
    with pytest.raises(RetryableAPIError):
        call(s, sleeps, max_retries=3)
    assert s.calls == 3 and sleeps == [1, 2]                  # 1s, 2s, then give up (never forever)


def test_permanent_error_is_not_retried():
    sleeps = []
    s = FakeSession([FakeResponse(401)])
    with pytest.raises(NonRetryableAPIError):
        call(s, sleeps)
    assert s.calls == 1 and sleeps == []


def test_connection_error_is_retried_then_fails_clearly():
    sleeps = []
    s = FakeSession([requests.ConnectionError("down")] * 2)
    with pytest.raises(RetryableAPIError, match="replay-raw"):
        call(s, sleeps, max_retries=2)
