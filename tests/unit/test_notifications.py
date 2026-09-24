"""FR-038/FR-069 notification payload tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from patchline.core import notifications


class TestPairing:
    def test_both_unset_ok(self, clean_env):
        assert notifications.pairing_status() == "ok"
        assert not notifications.notifications_enabled()

    def test_both_set_enabled(self, clean_env, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://receiver.example/hook")
        monkeypatch.setenv("NOTIFICATION_SIGNING_SECRET", "secret")
        assert notifications.pairing_status() == "ok"
        assert notifications.notifications_enabled()

    def test_exactly_one_set_unpaired(self, clean_env, monkeypatch):
        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://receiver.example/hook")
        assert notifications.pairing_status() == "unpaired"
        assert not notifications.notifications_enabled()


class TestPayload:
    def test_shape(self):
        payload = notifications.build_payload(
            "scan_complete",
            instance_id="inst-1",
            entry_id=3,
            repository_name="acme/widgets",
            status="2 occurrences",
        )
        assert payload["event_type"] == "scan_complete"
        assert payload["instance_id"] == "inst-1"
        assert payload["entry_id"] == 3
        assert payload["repository_name"] == "acme/widgets"
        assert payload["delivery_id"]
        assert payload["timestamp"]
        # next_attempt_at only on engine_failure; regression_type only on regression.
        assert "next_attempt_at" not in payload
        assert "regression_type" not in payload

    def test_unknown_event_rejected(self):
        with pytest.raises(ValueError):
            notifications.build_payload("not_an_event", instance_id="x")

    def test_engine_failure_carries_next_attempt(self):
        payload = notifications.build_payload(
            "engine_failure",
            instance_id="x",
            next_attempt_at=notifications.engine_failure_next_attempt_at(2),
        )
        assert "next_attempt_at" in payload

    def test_regression_requires_type(self):
        with pytest.raises(ValueError):
            notifications.build_payload("regression_detected", instance_id="x")
        payload = notifications.build_payload(
            "regression_detected", instance_id="x", regression_type="reintroduction"
        )
        assert payload["regression_type"] == "reintroduction"

    def test_backoff_bounds(self):
        first = notifications.engine_failure_next_attempt_at(1)
        fifth = notifications.engine_failure_next_attempt_at(5)
        now = datetime.now(timezone.utc)
        assert 30 < (first - now).total_seconds() < 90  # ~60s ±20%
        assert (fifth - now).total_seconds() <= 3600 + 1  # capped at max


class TestDelivery:
    def test_never_unsigned(self, clean_env, monkeypatch):
        """FR-069: without BOTH url and secret, delivery is a loud no-op."""
        monkeypatch.setenv("NOTIFICATION_WEBHOOK_URL", "https://receiver.example/hook")
        payload = notifications.build_payload("scan_complete", instance_id="x")
        assert notifications.deliver(payload) is False
