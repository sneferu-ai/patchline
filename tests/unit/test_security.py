"""Security primitive tests (FR-005/031/038/040/050)."""
from __future__ import annotations

from patchline.core.security import (
    generate_csrf_token,
    github_webhook_signature,
    notification_signature,
    safe_redirect_target,
    validate_branch_names,
    verify_csrf_token,
    verify_github_signature,
)


class TestWebhookHMAC:
    def test_sign_and_verify(self):
        body = b'{"action":"created"}'
        header = github_webhook_signature("wh-secret", body)
        assert header.startswith("sha256=")
        assert verify_github_signature("wh-secret", body, header)

    def test_reject_wrong_secret(self):
        body = b"payload"
        header = github_webhook_signature("secret-a", body)
        assert not verify_github_signature("secret-b", body, header)

    def test_reject_missing(self):
        assert not verify_github_signature("secret", b"body", None)
        assert not verify_github_signature("", b"body", "sha256=abc")


class TestNotificationSignature:
    def test_format(self):
        assert notification_signature("s", b"{}").startswith("sha256=")


class TestCSRF:
    def test_round_trip(self):
        token = generate_csrf_token("session-secret", "cookie-value")
        assert verify_csrf_token(token, token)

    def test_rejects_other(self):
        token = generate_csrf_token("session-secret", "cookie-value")
        assert not verify_csrf_token(token, token + "x")
        assert not verify_csrf_token(None, token)
        assert not verify_csrf_token(token, None)


class TestRedirectAllowlist:
    def test_allowed_paths(self):
        assert safe_redirect_target("/") == "/"
        assert safe_redirect_target("/entries/abc-123") == "/entries/abc-123"
        assert safe_redirect_target("/entries/e1/repos/r_2/review") == "/entries/e1/repos/r_2/review"
        assert safe_redirect_target("/entries/e1/coverage") == "/entries/e1/coverage"
        assert safe_redirect_target("/repositories/r_2/coverage") == "/repositories/r_2/coverage"
        assert safe_redirect_target("/jobs") == "/jobs"
        assert safe_redirect_target("/settings") == "/settings"
        assert safe_redirect_target("/connections/setup") == "/connections/setup"
        assert safe_redirect_target("/pull-requests") == "/pull-requests"
        assert safe_redirect_target("/repositories") == "/repositories"

    def test_protocol_relative_rejected(self):
        assert safe_redirect_target("//evil.example/x") == "/"

    def test_absolute_url_rejected(self):
        assert safe_redirect_target("https://evil.example/") == "/"
        assert safe_redirect_target("http://evil.example/repositories") == "/"

    def test_unknown_path_falls_back(self):
        assert safe_redirect_target("/admin/secret") == "/"
        assert safe_redirect_target("") == "/"
        assert safe_redirect_target(None) == "/"


class TestBranchValidation:
    def test_valid(self):
        assert validate_branch_names(["main", "release-1.0"]) == []

    def test_empty_rejected(self):
        errors = validate_branch_names(["main", ""])
        assert any("empty" in e for e in errors)

    def test_control_chars_rejected(self):
        errors = validate_branch_names(["bad\x01name"])
        assert any("control" in e for e in errors)

    def test_max_ten(self):
        errors = validate_branch_names([f"b{i}" for i in range(11)])
        assert any("10" in e for e in errors)
