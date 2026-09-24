"""GitHub API client (httpx-based): App JWT, installation tokens, archive
downloads, and the Git Data API sequence used for PR dispatch (FR-021/056/061).

Structural safety contract (FR-022/FR-071, verified by AC-029):
- This client implements NO endpoint that finalizes or combines pull requests.
  Merging is always a human act performed on GitHub; the product contains no
  code path for it. Do not add one.
- Ref updates are restricted to ``patchline/*`` branches; attempting to move
  any other ref raises ValueError.

Retry policy (FR-070): retry on 429 and 5xx; never on other 4xx; respect
``Retry-After`` / ``X-RateLimit-Reset``; exponential backoff 1s→60s, ×2,
±20% jitter; max 5 retries. Shared between web and worker processes.

Module level avoids importing httpx so the module is importable in minimal
environments; httpx is required only when a client is constructed.
"""
from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ... import config

log = logging.getLogger("patchline.github")

GITHUB_API_BASE = "https://api.github.com"
ARCHIVE_MAX_BYTES = 500 * 1024 * 1024  # FR-055: 500MB
ARCHIVE_TIMEOUT_S = 120.0  # FR-055
INSTALLATION_TOKEN_TTL_MINUTES = 50  # FR-056
MAX_RETRIES = 5  # FR-070


class GitHubError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RateLimitError(GitHubError):
    """HTTP 429 (or secondary rate limit). The worker re-enqueues with
    next_attempt_at so one rate-limited repository never blocks the queue."""


class FastForwardError(GitHubError):
    """PATCH /git/refs rejected a non-fast-forward update (HTTP 422)."""


class PermissionDeniedError(GitHubError):
    """403/404 on a write — surfaces the diff-export fallback (FR-067)."""


class ArchiveTooLarge(Exception):
    """Archive exceeded the 500MB cap; caller attempts the shallow-clone
    fallback (FR-072)."""


# ---------------------------------------------------------------------------
# Retry policy (pure helpers — unit-testable without httpx)


def should_retry_status(status: int) -> bool:
    """FR-070: 429 and 5xx are retryable; other 4xx are not."""
    return status == 429 or 500 <= status < 600


def compute_backoff(attempt: int, retry_after: Optional[float] = None, jitter: float = 0.2) -> float:
    """Exponential backoff: initial 1s, max 60s, ×2, ±20% jitter.

    ``retry_after`` (from the Retry-After header) wins when present.
    """
    if retry_after is not None and retry_after > 0:
        return float(retry_after)
    base = min(60.0, 1.0 * (2 ** max(attempt - 1, 0)))
    spread = base * jitter
    return min(60.0, max(0.0, base + random.uniform(-spread, spread)))


def retry_after_from_headers(headers: Dict[str, str]) -> Optional[float]:
    if not headers:
        return None
    lowered = {k.lower(): v for k, v in headers.items()}
    raw = lowered.get("retry-after")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    reset = lowered.get("x-ratelimit-reset")
    if reset:
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return None


def build_retry_transport():
    """FR-070 custom httpx transport, shared by web and worker processes."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise GitHubError("httpx is required for the GitHub client") from exc

    class RetryTransport(httpx.HTTPTransport):
        def handle_request(self, request):  # noqa: ANN001
            last_exc = None
            for attempt in range(1, MAX_RETRIES + 2):
                try:
                    response = super().handle_request(request)
                except Exception as exc:  # transport-level failure: retryable
                    last_exc = exc
                    if attempt > MAX_RETRIES:
                        raise
                    time.sleep(compute_backoff(attempt))
                    continue
                if not should_retry_status(response.status_code) or attempt > MAX_RETRIES:
                    return response
                wait = compute_backoff(attempt, retry_after_from_headers(dict(response.headers)))
                log.warning(
                    "github %s %s -> %s; retry %s/%s in %.1fs",
                    request.method,
                    request.url.path,
                    response.status_code,
                    attempt,
                    MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
            if last_exc is not None:
                raise last_exc
            return response  # unreachable; defensive

    return RetryTransport


# ---------------------------------------------------------------------------
# GitHub App JWT (FR-061)


def create_app_jwt(app_id: Optional[str] = None, private_key_pem: Optional[str] = None) -> str:
    """RS256 JWT: iat=now, exp=now+10min, iss=GITHUB_APP_ID (AC-037)."""
    try:
        import jwt  # PyJWT; lazy so minimal installs can import this module
    except ImportError as exc:  # pragma: no cover
        raise GitHubError("PyJWT is required to mint GitHub App JWTs") from exc
    app_id = app_id or os.environ.get("GITHUB_APP_ID")
    pem = private_key_pem or config.github_app_private_key_pem()
    if not app_id:
        raise GitHubError("GITHUB_APP_ID is not configured")
    if not pem:
        raise GitHubError("GitHub App private key is not configured (FR-056)")
    now = datetime.now(timezone.utc)
    payload = {
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "iss": str(app_id),
    }
    return jwt.encode(payload, pem, algorithm="RS256")


# ---------------------------------------------------------------------------
# Installation token cache (FR-056): in-memory, 50-minute TTL, never persisted.


class InstallationTokenCache:
    def __init__(self, ttl_minutes: int = INSTALLATION_TOKEN_TTL_MINUTES):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._tokens: Dict[int, Tuple[str, datetime]] = {}

    def get(self, installation_id: int) -> Optional[str]:
        entry = self._tokens.get(installation_id)
        if not entry:
            return None
        token, expires_at = entry
        if datetime.now(timezone.utc) >= expires_at:
            self._tokens.pop(installation_id, None)
            return None
        return token

    def put(self, installation_id: int, token: str, expires_at: Optional[datetime] = None) -> str:
        self._tokens[installation_id] = (
            token,
            expires_at or (datetime.now(timezone.utc) + self.ttl),
        )
        return token


# ---------------------------------------------------------------------------
# The client


class GitHubClient:
    """httpx-based client for OAuth exchange, app-level and repo-level calls."""

    def __init__(
        self,
        base_url: str = GITHUB_API_BASE,
        app_jwt: Optional[str] = None,
        installation_token: Optional[str] = None,
        client: Any = None,
        token_cache: Optional[InstallationTokenCache] = None,
    ):
        try:
            import httpx  # noqa: F401 - presence check only
        except ImportError as exc:  # pragma: no cover
            raise GitHubError("httpx is required for the GitHub client") from exc
        self.base_url = base_url.rstrip("/")
        self.app_jwt = app_jwt
        self.installation_token = installation_token
        self.token_cache = token_cache or InstallationTokenCache()
        if client is not None:
            self._client = client
        else:
            import httpx

            self._client = httpx.Client(
                base_url=self.base_url,
                transport=build_retry_transport()(),
                timeout=httpx.Timeout(60.0, read=60.0),
                headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
            )

    # -- plumbing -----------------------------------------------------------

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass

    def _auth_headers(self, token: Optional[str], use_jwt: bool) -> Dict[str, str]:
        if use_jwt:
            jwt_token = self.app_jwt or create_app_jwt()
            return {"Authorization": f"Bearer {jwt_token}"}
        effective = token or self.installation_token
        if effective:
            return {"Authorization": f"Bearer {effective}"}
        return {}

    def _request(
        self,
        method: str,
        path: str,
        token: Optional[str] = None,
        use_jwt: bool = False,
        expected: Tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._auth_headers(token, use_jwt))
        try:
            resp = self._client.request(method, path, headers=headers, **kwargs)
        except GitHubError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GitHubError(f"github {method} {path} transport failure: {exc}") from exc
        if resp.status_code == 429:
            raise RateLimitError(
                f"github {method} {path} rate limited",
                status=429,
                retry_after=retry_after_from_headers(dict(resp.headers)),
            )
        if resp.status_code == 422 and method == "PATCH" and "/git/refs/" in path:
            raise FastForwardError(
                "branch ref is not fast-forwardable",
                status=422,
            )
        if resp.status_code in (401, 403, 404) and method in ("POST", "PATCH"):
            raise PermissionDeniedError(
                f"github {method} {path} denied with {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )
        if resp.status_code not in expected:
            raise GitHubError(
                f"github {method} {path} -> {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.content

    # -- OAuth (FR-001) -------------------------------------------------------

    def exchange_oauth_code(self, client_id: str, client_secret: str, code: str) -> Dict[str, Any]:
        resp = self._client.post(
            "https://github.com/login/oauth/access_token",
            data={"client_id": client_id, "client_secret": client_secret, "code": code},
            headers={"Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise GitHubError(f"oauth token exchange failed with {resp.status_code}", status=resp.status_code)
        data = resp.json()
        if "error" in data:
            raise GitHubError(f"oauth token exchange error: {data.get('error_description') or data['error']}")
        return data

    def get_authenticated_user(self, oauth_token: str) -> Dict[str, Any]:
        return self._request("GET", "/user", token=oauth_token)

    # -- App-level (JWT) ------------------------------------------------------

    def list_installations(self) -> List[Dict[str, Any]]:
        return self._request("GET", "/app/installations", use_jwt=True) or []

    def mint_installation_token(self, installation_id: int) -> str:
        cached = self.token_cache.get(installation_id)
        if cached:
            return cached
        data = self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            use_jwt=True,
            expected=(200, 201),
            json={},
        )
        token = data.get("token")
        if not token:
            raise GitHubError("installation token response missing 'token'")
        expires_raw = data.get("expires_at")
        expires_at = None
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(str(expires_raw).replace("Z", "+00:00"))
            except ValueError:
                expires_at = None
        return self.token_cache.put(installation_id, token, expires_at)

    def list_installation_repositories(self, installation_id: int) -> List[Dict[str, Any]]:
        token = self.mint_installation_token(installation_id)
        data = self._request("GET", "/installation/repositories", token=token)
        return (data or {}).get("repositories", [])

    # -- Repository metadata ----------------------------------------------------

    def get_repo(self, full_name: str, token: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{full_name}", token=token)

    def branch_exists(self, full_name: str, branch: str, token: Optional[str] = None) -> bool:
        """FR-040 existence confirmation; 404 → False."""
        try:
            self._request("GET", f"/repos/{full_name}/branches/{branch}", token=token)
            return True
        except GitHubError as exc:
            if exc.status == 404:
                return False
            raise

    def get_branch_ref(self, full_name: str, branch: str, token: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{full_name}/git/refs/heads/{branch}", token=token)

    # -- Archive download (FR-055) --------------------------------------------

    def download_archive(self, full_name: str, ref: str, dest_path: str, token: Optional[str] = None) -> str:
        """Stream the branch archive to ``dest_path`` with the 500MB cap and a
        120s timeout; raises ArchiveTooLarge so the caller can shallow-clone."""
        headers = self._auth_headers(token, use_jwt=False)
        try:
            with self._client.stream(
                "GET",
                f"/repos/{full_name}/zipball/{ref}",
                headers=headers,
                timeout=ARCHIVE_TIMEOUT_S,
                follow_redirects=True,
            ) as resp:
                if resp.status_code == 429:
                    raise RateLimitError("archive download rate limited", status=429)
                if resp.status_code != 200:
                    raise GitHubError(f"archive download -> {resp.status_code}", status=resp.status_code)
                written = 0
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 256):
                        written += len(chunk)
                        if written > ARCHIVE_MAX_BYTES:
                            fh.close()
                            try:
                                os.unlink(dest_path)
                            except OSError:
                                pass
                            raise ArchiveTooLarge(f"archive exceeds {ARCHIVE_MAX_BYTES} bytes")
                        fh.write(chunk)
        except (RateLimitError, ArchiveTooLarge, GitHubError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise GitHubError(f"archive download failed: {exc}") from exc
        return dest_path

    # -- Git Data API sequence (FR-021) ---------------------------------------

    def create_ref(self, full_name: str, ref: str, sha: str, token: Optional[str] = None) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{full_name}/git/refs",
            token=token,
            expected=(200, 201),
            json={"ref": ref, "sha": sha},
        )

    def create_blob(self, full_name: str, content_b64: str, token: Optional[str] = None) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{full_name}/git/blobs",
            token=token,
            expected=(200, 201),
            json={"content": content_b64, "encoding": "base64"},
        )

    def create_tree(self, full_name: str, base_tree: str, tree_items: List[Dict[str, Any]], token: Optional[str] = None) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{full_name}/git/trees",
            token=token,
            expected=(200, 201),
            json={"base_tree": base_tree, "tree": tree_items},
        )

    def create_commit(self, full_name: str, message: str, tree_sha: str, parent_shas: List[str], token: Optional[str] = None) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{full_name}/git/commits",
            token=token,
            expected=(200, 201),
            json={"message": message, "tree": tree_sha, "parents": parent_shas},
        )

    def update_patchline_ref(self, full_name: str, branch: str, sha: str, token: Optional[str] = None) -> Dict[str, Any]:
        """Fast-forward-only ref update, restricted to patchline/* refs (FR-071)."""
        if not branch.startswith("patchline/"):
            raise ValueError("ref updates are restricted to patchline/* branches")
        return self._request(
            "PATCH",
            f"/repos/{full_name}/git/refs/heads/{branch}",
            token=token,
            json={"sha": sha, "force": False},
        )

    def create_pull_request(self, full_name: str, head: str, base: str, title: str, body: str, token: Optional[str] = None) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{full_name}/pulls",
            token=token,
            expected=(200, 201),
            json={"head": head, "base": base, "title": title, "body": body, "maintainer_can_modify": True},
        )

    def get_pull_request(self, full_name: str, number: int, token: Optional[str] = None) -> Dict[str, Any]:
        return self._request("GET", f"/repos/{full_name}/pulls/{number}", token=token)

    def list_pull_requests(self, full_name: str, state: str = "all", token: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._request("GET", f"/repos/{full_name}/pulls", token=token, params={"state": state, "per_page": 100}) or []

    def compare(self, full_name: str, base: str, head: str, token: Optional[str] = None) -> Dict[str, Any]:
        """GET /repos/{o}/{r}/compare/{base}...{head} — branch reuse on retry (FR-021)."""
        return self._request("GET", f"/repos/{full_name}/compare/{base}...{head}", token=token)

    # FR-022/FR-071: there is intentionally no method here that finalizes a
    # pull request. Any attempt to call one fails with AttributeError (AC-029).
