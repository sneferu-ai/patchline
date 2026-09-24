"""FR-065 coverage state machine: complete map verification (AC-030 support)."""
from __future__ import annotations

import pytest

from patchline.core.coverage import (
    INITIAL_STATUS,
    OPEN_CHANGE_STATUSES,
    STATUSES,
    TRANSITIONS,
    InvalidTransition,
    assert_transition,
    can_transition,
    is_open_change,
)

# The FR-065 table, transcribed verbatim from the spec.
EXPECTED_EDGES = {
    "not_scanned": {"scanning", "skipped_language"},
    "scanning": {"no_hits", "pattern_detected", "scan_failed", "skipped_language"},
    "no_hits": {"scanning", "pattern_detected"},
    "pattern_detected": {"remediation_pending", "scanning"},
    "remediation_pending": {"pr_open", "dispatch_failed", "scanning"},
    "pr_open": {"migrated", "migration_incomplete", "pr_rejected"},
    "pr_rejected": {"scanning", "remediation_pending"},
    "migrated": {"pattern_detected", "scanning"},
    "migration_incomplete": {"remediation_pending", "scanning", "migrated"},
    "scan_failed": {"scanning"},
    "dispatch_failed": {"pr_open", "remediation_pending", "scanning", "migrated"},
    "skipped_language": {"scanning"},
}


class TestTransitionMap:
    def test_all_twelve_statuses_present(self):
        assert set(TRANSITIONS.keys()) == set(STATUSES)
        assert len(STATUSES) == 12

    def test_edges_match_spec_verbatim(self):
        for status, targets in EXPECTED_EDGES.items():
            assert TRANSITIONS[status] == targets, f"{status}: {TRANSITIONS[status]} != {targets}"

    def test_forbidden_edges_absent(self):
        # FR-065 notes: these direct transitions must NOT exist.
        assert not can_transition("pr_open", "pattern_detected")
        assert not can_transition("remediation_pending", "pattern_detected")
        assert not can_transition("not_scanned", "migrated")
        assert not can_transition("not_scanned", "pr_open")
        # not_scanned is initial-only: nothing transitions INTO it.
        for status, targets in TRANSITIONS.items():
            assert "not_scanned" not in targets, f"{status} -> not_scanned must not exist"

    def test_initial_status(self):
        assert INITIAL_STATUS == "not_scanned"

    def test_invalid_transition_raises(self):
        with pytest.raises(InvalidTransition):
            assert_transition("pr_open", "pattern_detected")

    def test_no_trap_states(self):
        # Every status can reach at least one other state (no dead ends).
        for status, targets in TRANSITIONS.items():
            assert targets, f"{status} is a trap state"

    def test_open_change_set(self):
        assert OPEN_CHANGE_STATUSES == {
            "pattern_detected",
            "remediation_pending",
            "pr_open",
            "pr_rejected",
            "migration_incomplete",
            "dispatch_failed",
        }
        # FR-066: pr_rejected IS included in open-change counts.
        assert is_open_change("pr_rejected")
        assert not is_open_change("migrated")
        assert not is_open_change("no_hits")
