"""FR-070 GitHub retry policy tests (pure helpers)."""
from __future__ import annotations

import time

from patchline.adapters.scm.github import (
    compute_backoff,
    retry_after_from_headers,
    should_retry_status,
)


class TestShouldRetry:
    def test_retryable(self):
        assert should_retry_status(429)
        assert should_retry_status(500)
        assert should_retry_status(503)

    def test_not_retryable(self):
        assert not should_retry_status(400)
        assert not should_retry_status(401)
        assert not should_retry_status(403)
        assert not should_retry_status(404)
        assert not should_retry_status(422)
        assert not should_retry_status(200)


class TestBackoff:
    def test_retry_after_wins(self):
        assert compute_backoff(1, retry_after=17.0) == 17.0

    def test_exponential_growth(self):
        # Base 1s ×2 with ±20% jitter.
        for _ in range(20):
            assert 0.8 <= compute_backoff(1) <= 1.2
            assert 1.6 <= compute_backoff(2) <= 2.4
            assert 3.2 <= compute_backoff(3) <= 4.8

    def test_capped_at_60(self):
        for _ in range(10):
            assert compute_backoff(12) <= 60.0

    def test_headers(self):
        assert retry_after_from_headers({"Retry-After": "5"}) == 5.0
        future = str(int(time.time()) + 30)
        value = retry_after_from_headers({"X-RateLimit-Reset": future})
        assert value is not None and 0 < value <= 31
        assert retry_after_from_headers({}) is None
        assert retry_after_from_headers({"Retry-After": "not-a-number"}) is None
