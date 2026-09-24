"""FR-043 engine adapter interface + mock + Sneferu implementations.

The adapter is the SOLE contact point with the engine (K6): workflow names
are configuration values injected via :class:`WorkflowConfig` at construction;
no workflow name appears in any method signature.

``Pattern`` is an opaque JSON-serializable dict defined by the engine; product
code never parses pattern internals. All ``Provenance`` typed fields are
Optional and provisional (validated by AC-026); unrecognized engine fields
land in ``Provenance.extra``.

Module level is stdlib-only; httpx is imported lazily inside the Sneferu
adapter so the mock path (dev/test/demo) works without HTTP dependencies.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .. import config

log = logging.getLogger("patchline.engine")

FIXTURE_VERSION = "1.0"


class EngineError(Exception):
    """Any engine-side failure (network, schema, timeout)."""


# ---------------------------------------------------------------------------
# Typed payloads (FR-043 contract)


@dataclass
class EntryInput:
    prior_api_shape: str
    target_contract: str
    language: str


@dataclass
class FeedbackSignal:
    occurrence_snippet: str
    reason: str
    created_at: str


# Pattern is an opaque JSON-serializable dict defined by the engine.
Pattern = Dict[str, Any]


@dataclass
class PatternSet:
    patterns: List[Pattern]
    engine_run_id: str
    language: str


@dataclass
class ScanContext:
    repo_full_name: str
    branch: str
    archive_path: str
    language: str


@dataclass
class OccurrenceData:
    file_path: str
    line_start: int
    line_end: int
    snippet: str
    confidence: Optional[str] = None


@dataclass
class ScanResult:
    occurrences: List[OccurrenceData]
    files_examined: int
    engine_run_id: str


@dataclass
class RemediationInput:
    entry: EntryInput
    occurrences: List[OccurrenceData]
    repo_context: ScanContext


@dataclass
class Provenance:
    quality_score: Optional[float] = None
    certificate_class: Optional[str] = None
    disagreement_record: Optional[str] = None
    models: List[str] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def any_typed_present(self) -> bool:
        return bool(
            self.quality_score is not None
            or self.certificate_class
            or self.disagreement_record
            or self.models
        )

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "quality_score": self.quality_score,
            "certificate_class": self.certificate_class,
            "disagreement_record": self.disagreement_record,
            "models": list(self.models),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_json_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["Provenance"]:
        if data is None:
            return None
        known = {"quality_score", "certificate_class", "disagreement_record", "models", "extra"}
        extra = dict(data.get("extra") or {})
        for key, value in data.items():
            if key not in known:
                extra[key] = value
        models = data.get("models") or []
        return cls(
            quality_score=data.get("quality_score"),
            certificate_class=data.get("certificate_class"),
            disagreement_record=data.get("disagreement_record"),
            models=list(models) if isinstance(models, list) else [str(models)],
            extra=extra,
        )


@dataclass
class DiffWithProvenance:
    diff: str
    provenance: Optional[Provenance]
    engine_run_id: str


@dataclass
class WorkflowConfig:
    """Maps operation types to engine workflow names (K6 resolution)."""

    derive: str = "derive_patterns"
    scan: str = "scan_repository"
    remediate: str = "generate_remediation"

    @classmethod
    def from_env(cls) -> "WorkflowConfig":
        settings = config.Settings.from_env()
        return cls(derive=settings.workflow_derive, scan=settings.workflow_scan, remediate=settings.workflow_remediate)


class EngineAdapter(Protocol):
    """FR-043: the sole engine contact surface."""

    def get_version(self) -> Optional[str]:
        """Return the engine version string, or None if unavailable."""
        ...

    def derive_patterns(self, entry: EntryInput, feedback: Optional[List[FeedbackSignal]] = None) -> PatternSet:
        """Derive searchable code patterns from changelog entry text.

        feedback defaults to None; implementations coalesce to [] internally.
        Raises EngineError.
        """
        ...

    def scan_repository(self, context: ScanContext, patterns: PatternSet) -> ScanResult:
        """Scan a repository branch archive for occurrences. Raises EngineError."""
        ...

    def generate_remediation(self, input: RemediationInput) -> DiffWithProvenance:
        """Generate a remediation diff + provenance. Raises EngineError.

        The adapter MUST return a clean unified diff (only ---, +++, @@,
        context, +, - lines); engine-specific metadata is stripped before
        returning and provenance is returned in the typed field.
        """
        ...


# ---------------------------------------------------------------------------
# Mock adapter (FR-045): deterministic, fixture-keyed.


def sanitize_repo_key(full_name: str) -> str:
    """Fixture filename for a repository: lowercase, / → _, non-alnum stripped."""
    import re

    key = full_name.strip().lower().replace("/", "_")
    return re.sub(r"[^a-z0-9_]", "", key)


def entry_text_hash(entry: EntryInput) -> str:
    digest = hashlib.sha256(
        (entry.prior_api_shape + entry.target_contract + entry.language).encode("utf-8")
    ).hexdigest()
    return digest[:16]


class MockEngineAdapter:
    """Deterministic fixture-keyed engine for development, tests, and demos.

    The mock IGNORES feedback flags by design (FR-045): fixture determinism
    is the fundamental design principle of the mock.
    """

    def __init__(self, fixture_dir: Optional[str] = None):
        self.fixture_dir = fixture_dir or default_engine_fixture_dir()
        self._default_fixture = self._load_default_fixture()

    # -- fixture plumbing ---------------------------------------------------

    def _fixture_path(self, name: str) -> str:
        return os.path.join(self.fixture_dir, f"{name}.json")

    def _load_json(self, name: str) -> Optional[Dict[str, Any]]:
        path = self._fixture_path(name)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("engine fixture %s unreadable (%s); using default fixture", path, exc)
            return None
        version = str(data.get("fixture_version", ""))
        if version != FIXTURE_VERSION:
            log.warning(
                "engine fixture %s has fixture_version=%r (expected %r); using default fixture",
                path,
                version,
                FIXTURE_VERSION,
            )
            return None
        return data

    def _load_default_fixture(self) -> Dict[str, Any]:
        data = self._load_json("default")
        if data is not None:
            return data
        # Built-in default: 2 occurrences, a simple diff, full provenance.
        return {
            "fixture_version": FIXTURE_VERSION,
            "patterns": [{"kind": "symbol_rename", "old": "get_user", "new": "get_user_by_id"}],
            "occurrences": [
                {
                    "file_path": "src/example.py",
                    "line_start": 1,
                    "line_end": 1,
                    "snippet": "user = get_user(user_id)",
                    "confidence": "high",
                },
                {
                    "file_path": "src/example.py",
                    "line_start": 1,
                    "line_end": 1,
                    "snippet": "user = get_user(user_id)",
                    "confidence": "medium",
                },
            ],
            # Self-consistent with the built-in mock archive
            # ({"src/example.py": "user = get_user(user_id)\n"}): the diff
            # applies cleanly and yields valid Python.
            "diff": (
                "--- a/src/example.py\n"
                "+++ b/src/example.py\n"
                "@@ -1,1 +1,1 @@\n"
                "-user = get_user(user_id)\n"
                "+user = get_user_by_id(user_id)\n"
            ),
            "provenance": {
                "quality_score": 0.87,
                "certificate_class": "converged",
                "disagreement_record": "https://sneferu.example/runs/mock-run/disagreements",
                "models": ["mock-model-a", "mock-model-b"],
                "extra": {},
            },
        }

    def _repo_fixture(self, repo_full_name: str) -> Dict[str, Any]:
        data = self._load_json(sanitize_repo_key(repo_full_name))
        return data if data is not None else self._default_fixture

    def _entry_fixture(self, entry: EntryInput) -> Dict[str, Any]:
        data = self._load_json(f"entry_{entry_text_hash(entry)}")
        return data if data is not None else self._default_fixture

    # -- EngineAdapter ------------------------------------------------------

    def get_version(self) -> Optional[str]:
        return f"mock-{FIXTURE_VERSION}"

    def derive_patterns(self, entry: EntryInput, feedback: Optional[List[FeedbackSignal]] = None) -> PatternSet:
        feedback = feedback or []  # accepted; intentionally ignored (FR-045)
        fixture = self._entry_fixture(entry)
        patterns = fixture.get("patterns") or []
        return PatternSet(
            patterns=[dict(p) for p in patterns],
            engine_run_id=f"mock-derive-{entry_text_hash(entry)[:8]}",
            language=entry.language,
        )

    def scan_repository(self, context: ScanContext, patterns: PatternSet) -> ScanResult:
        fixture = self._repo_fixture(context.repo_full_name)
        occurrences = [OccurrenceData(**self._occurrence_kwargs(o)) for o in fixture.get("occurrences") or []]
        return ScanResult(
            occurrences=occurrences,
            files_examined=int(fixture.get("files_examined", max(len(occurrences), 1) * 7)),
            engine_run_id=f"mock-scan-{sanitize_repo_key(context.repo_full_name)[:12]}",
        )

    def generate_remediation(self, input: RemediationInput) -> DiffWithProvenance:
        fixture = self._repo_fixture(input.repo_context.repo_full_name)
        diff = fixture.get("diff") or self._default_fixture.get("diff") or ""
        provenance = Provenance.from_json_dict(fixture.get("provenance"))
        return DiffWithProvenance(
            diff=diff,
            provenance=provenance,
            engine_run_id=f"mock-remediate-{sanitize_repo_key(input.repo_context.repo_full_name)[:12]}",
        )

    @staticmethod
    def _occurrence_kwargs(raw: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "file_path": str(raw.get("file_path", "unknown.py")),
            "line_start": int(raw.get("line_start", 1)),
            "line_end": int(raw.get("line_end", raw.get("line_start", 1))),
            "snippet": str(raw.get("snippet", ""))[:500],
            "confidence": raw.get("confidence"),
        }


def default_engine_fixture_dir() -> str:
    return os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "tests", "fixtures", "engine")
    )


# ---------------------------------------------------------------------------
# Sneferu adapter (CP-5 production mode). httpx is imported lazily.


class SneferuEngineAdapter:
    """Calls the configured Sneferu endpoint; workflow names from WorkflowConfig."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        service_token: Optional[str] = None,
        workflow_config: Optional[WorkflowConfig] = None,
        timeout_s: float = 60.0,
        transport: Optional[Any] = None,
    ):
        self.base_url = (base_url or config.sneferu_base_url() or "").rstrip("/")
        self.service_token = service_token if service_token is not None else config.sneferu_service_token()
        self.workflows = workflow_config or WorkflowConfig.from_env()
        self.timeout_s = timeout_s
        self.transport = transport  # test seam: httpx.MockTransport for AC-026
        if not self.base_url:
            raise EngineError("SNEFERU_BASE_URL is required when ENGINE_MODE=sneferu")

    # -- transport ----------------------------------------------------------

    def _client(self):
        try:
            import httpx  # lazy: dev/test/demo installs may not have httpx
        except ImportError as exc:  # pragma: no cover
            raise EngineError("httpx is required for the sneferu engine adapter") from exc
        headers = {"Accept": "application/json"}
        if self.service_token:
            headers["Authorization"] = f"Bearer {self.service_token}"
        return httpx.Client(base_url=self.base_url, headers=headers, timeout=self.timeout_s, transport=self.transport)

    def _post(self, workflow: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with self._client() as client:
                resp = client.post(f"/workflows/{workflow}/run", json=payload)
                resp.raise_for_status()
                data = resp.json()
        except EngineError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized at the boundary
            raise EngineError(f"sneferu workflow {workflow!r} failed: {exc}") from exc
        if not isinstance(data, dict):
            raise EngineError(f"sneferu workflow {workflow!r} returned a non-object response")
        return data

    # -- EngineAdapter ------------------------------------------------------

    def get_version(self) -> Optional[str]:
        """None when unavailable — P10 then displays 'Engine version: unavailable'."""
        try:
            with self._client() as client:
                resp = client.get("/version")
                if resp.status_code != 200:
                    return None
                data = resp.json()
            if isinstance(data, dict):
                version = data.get("version")
                return str(version) if version else None
            return None
        except EngineError:
            return None
        except Exception:  # noqa: BLE001
            return None

    def ping(self) -> bool:
        """AC-008: /healthz?deep=1 engine reachability is an actual HTTP ping.

        Only a 2xx from the version endpoint counts. A 404 means the server at
        SNEFERU_BASE_URL is up but does not serve this adapter's API (today's
        Sneferu engine answers 404 here), so every engine call would fail and
        the deep health check must not report "healthy".
        """
        try:
            with self._client() as client:
                resp = client.get("/version")
                return 200 <= resp.status_code < 300
        except Exception:  # noqa: BLE001
            return False

    def derive_patterns(self, entry: EntryInput, feedback: Optional[List[FeedbackSignal]] = None) -> PatternSet:
        feedback = feedback or []
        data = self._post(
            self.workflows.derive,
            {
                "entry": {
                    "prior_api_shape": entry.prior_api_shape,
                    "target_contract": entry.target_contract,
                    "language": entry.language,
                },
                # A2: forwarded as request context; exact engine shape is a
                # future validation task.
                "feedback": [vars(f) for f in feedback],
            },
        )
        patterns = data.get("patterns")
        if not isinstance(patterns, list):
            raise EngineError("sneferu derive response missing 'patterns' list")
        return PatternSet(
            patterns=patterns,
            engine_run_id=str(data.get("engine_run_id") or data.get("run_id") or ""),
            language=str(data.get("language") or entry.language),
        )

    def scan_repository(self, context: ScanContext, patterns: PatternSet) -> ScanResult:
        data = self._post(
            self.workflows.scan,
            {
                "context": {
                    "repo_full_name": context.repo_full_name,
                    "branch": context.branch,
                    "archive_path": context.archive_path,
                    "language": context.language,
                },
                "patterns": patterns.patterns,
            },
        )
        occurrences = [
            OccurrenceData(
                file_path=str(o.get("file_path", "")),
                line_start=int(o.get("line_start", 1)),
                line_end=int(o.get("line_end", o.get("line_start", 1))),
                snippet=str(o.get("snippet", ""))[:500],
                confidence=o.get("confidence"),
            )
            for o in data.get("occurrences") or []
        ]
        return ScanResult(
            occurrences=occurrences,
            files_examined=int(data.get("files_examined") or 0),
            engine_run_id=str(data.get("engine_run_id") or data.get("run_id") or ""),
        )

    def generate_remediation(self, input: RemediationInput) -> DiffWithProvenance:
        data = self._post(
            self.workflows.remediate,
            {
                "entry": {
                    "prior_api_shape": input.entry.prior_api_shape,
                    "target_contract": input.entry.target_contract,
                    "language": input.entry.language,
                },
                "occurrences": [vars(o) for o in input.occurrences],
                "repo_context": {
                    "repo_full_name": input.repo_context.repo_full_name,
                    "branch": input.repo_context.branch,
                    "archive_path": input.repo_context.archive_path,
                    "language": input.repo_context.language,
                },
            },
        )
        raw_diff = data.get("diff")
        if not isinstance(raw_diff, str) or not raw_diff.strip():
            raise EngineError("sneferu remediate response missing 'diff'")
        provenance = Provenance.from_json_dict(data.get("provenance"))
        return DiffWithProvenance(
            diff=strip_diff_metadata(raw_diff),
            provenance=provenance,
            engine_run_id=str(data.get("engine_run_id") or data.get("run_id") or ""),
        )


def strip_diff_metadata(diff_text: str) -> str:
    """Adapter duty (FR-043/046): return a clean unified diff — only
    ---/+++/@@/context/+/- lines, with tab-separated header suffixes removed."""
    from ..core.diffhash import canonical_diff

    return canonical_diff(diff_text)


# ---------------------------------------------------------------------------
# Factory (FR-030): the sole construction point.


def create_adapter(
    mode: Optional[str] = None,
    fixture_dir: Optional[str] = None,
    workflow_config: Optional[WorkflowConfig] = None,
) -> EngineAdapter:
    """Build the configured adapter. Demo mode forces the mock (FR-060c)."""
    mode = (mode or config.engine_mode() or "mock").lower()
    if config.demo_mode():
        mode = "mock"
    if mode == "mock":
        return MockEngineAdapter(fixture_dir=fixture_dir)
    if mode == "sneferu":
        return SneferuEngineAdapter(workflow_config=workflow_config)
    raise EngineError(f"unknown ENGINE_MODE: {mode!r}")
