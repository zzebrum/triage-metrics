#!/usr/bin/env python3
"""
analyze.py — GitHub Triage Metrics: historical reconstruction, classification,
metrics, data-quality report, and local HTML dashboard generation.

This module contains the *pure* classification logic as importable functions so
that tests/ can drive them with synthetic data (no network). The CLI reads a raw
snapshot (from collect.py or make_mock_data.py), reconstructs one row per triage
ownership interval, and writes:

    data/processed/triage_ownership.json   # the normalized dataset (audit source)
    data/processed/issue_summary.json      # one row per cohort issue
    data/processed/metrics.json            # documented aggregates
    data/processed/data_quality.json       # flagged records
    dashboard/dashboard.html               # self-contained, opens directly

All timestamps are UTC. Durations are decimal hours.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# Small utilities (pure)
# ---------------------------------------------------------------------------


def parse_ts(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    if not isinstance(s, str):
        s = str(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # configuration dates like "2026-05-01" are documented as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    s = dt.isoformat(timespec="seconds")
    if dt.utcoffset() == timedelta(0):
        s = s.replace("+00:00", "Z")
    return s


def ym(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m") if dt else None


def last_period_window(as_of: datetime, months_back: int = 1) -> Tuple[datetime, datetime]:
    """Half-open window [start, end) covering the `months_back` full calendar
    months that immediately precede the month containing `as_of`.

    This is the "previous full period" used by a once-a-month run: run during
    month M you cover months [M-months_back, M). E.g. months_back=1 run on
    2026-08-27 returns exactly July 2026 —
        2026-07-01T00:00:00Z <= created_at < 2026-08-01T00:00:00Z —
    and never includes any of the current (still-open) month. With months_back=6
    run on 2026-08-01 you get the current six-month cohort back (Feb..Jul).

    `as_of` is expected to be timezone-aware (UTC); the tzinfo is preserved.
    """
    if months_back < 1:
        raise ValueError(f"months_back must be >= 1, got {months_back}")
    end = as_of.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = end.month - months_back
    year = end.year
    while month <= 0:
        month += 12
        year -= 1
    start = end.replace(year=year, month=month)
    return start, end


def is_in_cohort(created: datetime, start_dt: datetime, end_dt: datetime) -> bool:
    """Half-open window [start, end)."""
    return start_dt <= created < end_dt


def duration_hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


def stable_interval_id(repo: str, number: int, username: str, start_iso: str) -> str:
    """Start-anchored stable ID.

    The ID deliberately does NOT include the end timestamp: on incremental runs
    an active interval becomes completed in place (the end is a mutable field),
    so its ID never changes and no duplicate rows are produced.
    """
    raw = f"{repo}:{number}:{username}:{start_iso}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def percentile(values: List[float], p: float) -> Optional[float]:
    """Nearest-rank percentile (documented, deterministic, no deps)."""
    if not values:
        return None
    s = sorted(values)
    rank = max(1, int((p / 100.0) * len(s) + 0.5))
    rank = min(rank, len(s))
    return s[rank - 1]


def median(values: List[float]) -> Optional[float]:
    return statistics.median(values) if values else None


# ---------------------------------------------------------------------------
# Default assignee resolution
# ---------------------------------------------------------------------------


def resolve_default_assignee(config: Dict[str, Any], repo: str, created: datetime) -> Tuple[Optional[str], str]:
    """
    Return (assignee, method).

    method ∈ {manually_configured, historically_known, inferred, unknown}
    v1 only ever produces 'manually_configured' (from default_assignee_history)
    or 'unknown' (no entry effective at creation). 'historically_known' and
    'inferred' are reserved for a future API-backed reconstruction.
    """
    history = (config.get("default_assignee_history") or {}).get(repo) or []
    best = None
    for entry in sorted(history, key=lambda e: str(e["effective_from"])):
        ef = parse_ts(str(entry["effective_from"]))
        if ef is not None and ef <= created:
            best = entry["assignee"]
    if best:
        return best, "manually_configured"
    if history:
        return None, "unknown"  # before first entry (or gap)
    return None, "unknown"  # no history configured


# ---------------------------------------------------------------------------
# Assignee timeline reconstruction (from events)
# ---------------------------------------------------------------------------


def build_assignee_timeline(events: List[Dict[str, Any]], created: datetime) -> List[Tuple[str, Optional[str]]]:
    """
    Deterministic reconstruction of assignee changes.

    Events with the same timestamp are applied together in event-ID order so
    that rapid reassignments collapse into a single net transition (an
    'unassigned(A)' + 'assigned(B)' pair in the same second must NOT create a
    phantom zero-length unassigned gap).

    Returns an ordered list of (timestamp_iso, assignee_or_None) transitions.
    The assignee at creation is the value of the first transition whose
    timestamp equals the creation timestamp (GitHub emits an 'assigned' event
    at creation when the issue is created with an assignee), otherwise None.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for e in events:
        if e.get("event") in ("assigned", "unassigned") and e.get("created_at"):
            grouped.setdefault(e["created_at"], []).append(e)
    state: Optional[str] = None
    steps: List[Tuple[str, Optional[str]]] = []
    for ts in sorted(grouped):
        for e in sorted(grouped[ts], key=lambda x: x.get("id") or 0):
            if e["event"] == "assigned":
                state = e.get("assignee")
            else:  # unassigned
                removed = e.get("assignee")
                if state and (removed is None or removed == state):
                    state = None
        steps.append((ts, state))
    transitions: List[Tuple[str, Optional[str]]] = []
    prev: Optional[str] = "___SENTINEL___"
    for ts, s in steps:
        if s != prev:
            transitions.append((ts, s))
            prev = s
    return transitions


def assignee_at_creation(transitions: List[Tuple[str, Optional[str]]], created: datetime, window_seconds: int = 300) -> Optional[str]:
    """Assignee effectively holding the issue at creation.

    GitHub records the default-assignee auto-assignment a few seconds AFTER
    issue creation (e.g. +7s), not at the exact creation timestamp. We treat the
    first assignment as "at creation" if it happens within `window_seconds`, so
    the default assignee is recognized as accepted_from_default instead of a
    later first_assignment.
    """
    for ts, s in transitions:
        t = parse_ts(ts)
        if t is None:
            continue
        delta = (t - created).total_seconds()
        if -60 <= delta <= window_seconds:
            return s
        return None
    return None


def is_triage_member(config: Dict[str, Any], username: Optional[str]) -> bool:
    if not username:
        return False
    team = {str(u).casefold() for u in config.get("triage_team", [])}
    return username.casefold() in team


def has_observable_activity(issue: Dict[str, Any], username: str, before: datetime) -> bool:
    """
    Did `username` record any observable action strictly before `before`?

    Signals: authored an issue comment earlier, authored/created the issue, or
    appears as actor on a NON-assignment event (label changes, etc.) earlier.
    Pure `assigned`/`unassigned` events do NOT count: they record assignment
    mechanics (the initial default-assign, self-assigns), not evidence that the
    triager actually worked the issue. This keeps 'initial routing' (default did
    nothing) distinguishable from a real 'triage_handoff'.
    """
    if not username:
        return False
    target = username.casefold()
    if (issue.get("user") or "").casefold() == target:
        return True
    for c in issue.get("comments", []):
        t = parse_ts(c.get("created_at"))
        if t is not None and t < before and (c.get("user") or "").casefold() == target:
            return True
    for e in issue.get("events", []):
        if e.get("event") in ("assigned", "unassigned"):
            continue  # assignment mechanics, not triage work
        t = parse_ts(e.get("created_at"))
        if t is not None and t < before and (e.get("actor") or "").casefold() == target:
            return True
    return False


def labels_at(issue: Dict[str, Any], at: datetime) -> set:
    """Set of labels applied at-or-before `at` and not yet removed."""
    applied: set = set()
    removed: set = set()
    for e in issue.get("events", []):
        if e.get("event") not in ("labeled", "unlabeled"):
            continue
        t = parse_ts(e.get("created_at"))
        if t is None:
            continue
        if t > at:
            break
        name = e.get("label")
        if name is None:
            continue
        if e["event"] == "labeled":
            applied.add(name)
        else:
            removed.add(name)
    return {n for n in applied if n not in removed}


# ---------------------------------------------------------------------------
# Ownership interval reconstruction
# ---------------------------------------------------------------------------


def _finalize_interval(
    recorder,
    repo: str,
    issue: Dict[str, Any],
    config: Dict[str, Any],
    owner: str,
    start: datetime,
    start_type: str,
    incoming_transition: str,
    prev_assignee: Optional[str],
    end: Optional[datetime],
    end_reason: Optional[str],
    outcome: Optional[str],
    next_assignee: Optional[str],
    completion_month: Optional[str],
    flags: List[str],
    intervals_out: List[Dict[str, Any]],
    start_actor: Optional[str] = None,
    end_actor: Optional[str] = None,
) -> None:
    default_assignee, default_method = resolve_default_assignee(config, repo, parse_ts(issue["created_at"]))
    started = start_type == "initial_routing"
    handed = start_type == "triage_handoff"
    row = {
        "interval_id": stable_interval_id(repo, issue["number"], owner, iso(start)),
        "repository": repo,
        "issue_number": issue["number"],
        "issue_url": issue.get("html_url"),
        "issue_title": issue.get("title"),
        "issue_created_at": issue.get("created_at"),
        "issue_creation_month": ym(parse_ts(issue["created_at"])),
        "default_assignee_at_creation": default_assignee,
        "default_assignee_resolution_method": default_method,
        "triage_username": owner,
        "previous_assignee": prev_assignee,
        "next_assignee": next_assignee,
        "ownership_start_actor": start_actor,
        "ownership_end_actor": end_actor,
        "ownership_start": iso(start),
        "ownership_end": iso(end),
        "ownership_duration_hours": duration_hours(start, end) if end else None,
        "ownership_duration_days": (duration_hours(start, end) / 24.0) if end else None,
        "ownership_completion_month": ym(end) if end else None,
        "start_type": start_type,
        "ownership_end_reason": end_reason,
        "issue_outcome": outcome,
        "transition_type": incoming_transition,
        "originated_from_initial_routing": started,
        "originated_from_triage_handoff": handed,
        "data_collected_at": recorder.collected_at,
        "data_quality_flags": flags,
    }
    intervals_out.append(row)


class Recorder:
    def __init__(self, config: Dict[str, Any], collected_at: str):
        self.config = config
        self.collected_at = collected_at
        self.dq: List[Dict[str, Any]] = []
        # issue-level extra observables (e.g. post-dev-handoff returns)
        self.issue_meta: Dict[str, Dict[str, Any]] = {}

    def flag(self, repo: str, issue: Dict[str, Any], category: str, detail: str, interval_id: Optional[str] = None) -> None:
        self.dq.append(
            {
                "repository": repo,
                "issue_number": issue.get("number"),
                "issue_url": issue.get("html_url"),
                "issue_title": issue.get("title"),
                "interval_id": interval_id,
                "category": category,
                "detail": detail,
                "data_collected_at": self.collected_at,
            }
        )


def reconstruct_issue_intervals(
    repo: str,
    issue: Dict[str, Any],
    config: Dict[str, Any],
    recorder: Recorder,
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Reconstruct all triage ownership intervals for one issue. Returns
    (intervals, final_issue_outcome). Deterministic.
    """
    intervals: List[Dict[str, Any]] = []
    created = parse_ts(issue["created_at"])
    closed_at = parse_ts(issue.get("closed_at"))
    team = set(str(u).casefold() for u in config.get("triage_team", []))
    default_assignee, default_method = resolve_default_assignee(config, repo, created)
    spam_labels = set(str(s).casefold() for s in config.get("spam_labels") or [])

    transitions = build_assignee_timeline(issue.get("events") or [], created)
    window_seconds = int(config.get("default_assign_window_seconds", 300))
    created_assignee = assignee_at_creation(transitions, created, window_seconds=window_seconds)
    cut_after_dev = bool(config.get("cut_triage_after_dev_handoff"))
    creator = issue.get("user")

    # ---- build a single ordered stream of (time, rank, seq, kind, data) ----
    stream: List[Tuple[datetime, int, int, str, Any]] = []
    for ts, new_assignee in transitions:
        t = parse_ts(ts)
        if t is None:
            continue
        stream.append((t, 2, -1, "assignee", new_assignee))
    if closed_at:
        stream.append((closed_at, 0, -1, "closed", None))
    for e in issue.get("events", []):
        if e.get("event") == "reopened":
            t = parse_ts(e.get("created_at"))
            if t:
                stream.append((t, 3, e.get("id") or 0, "reopened", None))
    stream.sort(key=lambda x: (x[0], x[1], x[2]))

    # Who performed each assignment/unassignment (actors are frequently non-null
    # in this org's data; used to flag automation-performed reassignments).
    actor_at: Dict[str, str] = {}
    for e in issue.get("events", []):
        if e.get("event") in ("assigned", "unassigned") and e.get("created_at") and e.get("actor"):
            actor_at[e["created_at"]] = e["actor"]  # events are id-sorted: last actor wins per ts

    current_assignee: Optional[str] = created_assignee
    active_owner: Optional[str] = None
    active_start: Optional[datetime] = None
    active_start_type: Optional[str] = None
    active_incoming: Optional[str] = None
    active_prev: Optional[str] = None
    active_start_actor: Optional[str] = None
    active_flags: List[str] = []

    # The candidate for the "default did not really take ownership" rule can
    # only be the interval that began at creation with accepted_from_default.
    default_initial: bool = True  # only the first interval can be initial routing

    if current_assignee and current_assignee.casefold() in team:
        if current_assignee.casefold() == (default_assignee or "").casefold():
            active_start_type = "accepted_from_default"
            active_incoming = "accepted_from_default"
        else:
            active_start_type = "first_assignment"
            active_incoming = "first_assignment"
        active_owner = current_assignee
        active_start = created
        active_prev = None
        active_start_actor = actor_at.get(iso(created))

    last_ended: Optional[str] = None
    closed_since: Optional[datetime] = None
    # Once the issue has been handed to a non-triage (dev) user, the triage
    # ownership phase is over. Later returns to a triager ("ready to check",
    # QA verification, etc.) do NOT open new triage intervals.
    reached_dev: bool = False
    post_dev_returns: int = 0

    def start_interval(owner: str, t: datetime, st: str, inc: str, prev: Optional[str], flags) -> None:
        nonlocal active_owner, active_start, active_start_type, active_incoming, active_prev, active_start_actor, active_flags
        start_actor = actor_at.get(iso(t))
        if is_bot_actor(start_actor, config) and st in ("first_assignment", "initial_routing"):
            recorder.flag(
                repo, issue, "bot_reassignment",
                f"ownership interval for '{owner}' started by bot '{start_actor}' (start_type={st}); "
                f"flagged for review (possible automation/coverage assignment)",
                interval_id=stable_interval_id(repo, issue["number"], owner, iso(t)),
            )
        active_owner, active_start, active_start_type, active_incoming, active_prev, active_start_actor, active_flags = (
            owner, t, st, inc, prev, start_actor, list(flags)
        )

    def end_interval(t: datetime, reason: str, outcome: Optional[str], next_a: Optional[str], extra_flags=None) -> None:
        nonlocal active_owner, active_start, active_start_type, active_incoming, active_prev, active_start_actor, active_flags, last_ended
        if active_owner is None:
            return
        end_actor = actor_at.get(iso(t))
        flags = active_flags + (extra_flags or [])
        if (
            is_bot_actor(end_actor, config)
            and reason in ("handoff_to_triage", "unassigned", "handoff_to_non_triage")
        ):
            first_flag = [f"reassigned_by_bot:{end_actor}"]
            if first_flag[0] not in flags:
                flags = flags + first_flag
            recorder.flag(
                repo, issue, "bot_reassignment",
                f"ownership transition ended by bot '{end_actor}' (end_reason={reason}); "
                f"issue moved from '{active_owner}' to '{next_a or 'unassigned'}'; kept as-is in metrics "
                f"but flagged for review (possible bulk/vacation coverage reroute, not necessarily a real handoff)",
                interval_id=stable_interval_id(repo, issue["number"], active_owner, iso(active_start)),
            )
        _finalize_interval(
            recorder, repo, issue, config, active_owner, active_start, active_start_type,
            active_incoming, active_prev, t, reason, outcome, next_a, ym(t), flags, intervals,
            start_actor=active_start_actor, end_actor=end_actor,
        )
        last_ended = active_owner
        active_owner, active_start, active_start_type, active_incoming, active_prev, active_start_actor, active_flags = (
            None, None, None, None, None, None, []
        )

    def cancel_interval() -> None:
        """Drop an interval entirely (initial routing: default never owned)."""
        nonlocal active_owner, active_start, active_start_type, active_incoming, active_prev, active_start_actor, active_flags
        active_owner, active_start, active_start_type, active_incoming, active_prev, active_start_actor, active_flags = (
            None, None, None, None, None, None, []
        )

    def _spam_at(t: datetime) -> bool:
        return bool(spam_labels and (labels_at(issue, t) & spam_labels))

    for t, _rank, seq, kind, data in stream:
        if kind == "closed":
            if active_owner is not None:
                outcome = "spam_or_invalid" if _spam_at(t) else "resolved_by_triager"
                end_interval(t, "issue_closed", outcome, None)
            closed_since = t
            continue
        if kind == "reopened":
            closed_since = None
            if current_assignee and current_assignee.casefold() in team and active_owner is None:
                if cut_after_dev and reached_dev:
                    # returned after development handoff: verification, not triage
                    post_dev_returns += 1
                    continue
                start_interval(current_assignee, t, "first_assignment", "issue_closed", current_assignee, [])
                recorder.flag(
                    repo, issue, "issue_reopened",
                    f"issue closed then reopened at {iso(t)}; a new ownership interval "
                    f"was started for reassigned triager {current_assignee}",
                )
            continue
        # kind == assignee
        if closed_since is not None:
            # reassignments while the issue is closed do not open an interval
            current_assignee = data
            continue
        new_assignee = data
        if new_assignee == current_assignee:
            # no-op reassignment to the same user
            continue
        prev = current_assignee
        if new_assignee is None:
            if active_owner is not None and (prev or "").casefold() == active_owner.casefold():
                end_interval(t, "unassigned", "unassigned", None)
            current_assignee = None
            continue
        # new_assignee is a user
        if active_owner is None:
            if new_assignee.casefold() in team:
                if cut_after_dev and reached_dev:
                    # issue already left triage for development; this is a
                    # verification/QA return, not new triage ownership
                    post_dev_returns += 1
                else:
                    if (prev or "").casefold() == (default_assignee or "").casefold() and prev is not None:
                        # unusual: default never had an ownership interval (e.g. was
                        # unassigned at creation) — treat as initial routing
                        start_interval(new_assignee, t, "initial_routing", "initial_routing", prev, [])
                    elif prev is None:
                        start_interval(new_assignee, t, "first_assignment", "unassigned", None, [])
                    else:
                        start_interval(new_assignee, t, "first_assignment", "handoff_to_non_triage", prev, [])
                        recorder.flag(
                            repo, issue, "non_triage_to_triage",
                            f"issue passed from non-triage assignee '{prev}' to triager '{new_assignee}'",
                        )
            # new is non-triage -> nothing to record
            current_assignee = new_assignee
            continue
        # active interval exists
        if (active_owner or "").casefold() == new_assignee.casefold():
            continue  # reassigned to the same triager
        if new_assignee.casefold() in team:
            # triager -> triager
            sender = active_owner
            if (
                default_initial
                and active_start_type == "accepted_from_default"
                and (sender or "").casefold() == (default_assignee or "").casefold()
            ):
                # Candidate for initial routing vs real handoff
                acted = has_observable_activity(issue, sender, t)
                if acted:
                    end_interval(t, "handoff_to_triage", "handoff_to_triage", new_assignee)
                    start_interval(new_assignee, t, "triage_handoff", "triage_handoff", sender, [])
                else:
                    cancel_interval()
                    start_interval(new_assignee, t, "initial_routing", "initial_routing", sender, [])
                    # no DQ flag: initial routing is the well-defined outcome
                default_initial = False
            else:
                end_interval(t, "handoff_to_triage", "handoff_to_triage", new_assignee)
                start_interval(new_assignee, t, "triage_handoff", "triage_handoff", sender, [])
            default_initial = False
        else:
            # triager -> non-triage (development)
            reached_dev = True
            outcome = "spam_or_invalid" if _spam_at(t) else "handed_off_to_development"
            end_interval(t, "handoff_to_non_triage", outcome, new_assignee)
        current_assignee = new_assignee

    # ---- tail handling ----
    if active_owner is not None:
        if closed_since is not None or issue.get("state") == "closed":
            end_at = closed_since or parse_ts(issue.get("collected_at")) or created
            outcome = "spam_or_invalid" if _spam_at(end_at) else "resolved_by_triager"
            end_interval(end_at, "issue_closed", outcome, None)
        else:
            # still active
            _finalize_interval(
                recorder, repo, issue, config, active_owner, active_start, active_start_type,
                active_incoming, active_prev,
                None, None, "still_active", None, None, active_flags, intervals,
                start_actor=active_start_actor,
            )

    # ---- final issue outcome ----
    if intervals and intervals[-1].get("issue_outcome") in (
        "handed_off_to_development", "resolved_by_triager", "spam_or_invalid", "unassigned"
    ):
        final_outcome = intervals[-1]["issue_outcome"]
    elif intervals and intervals[-1].get("issue_outcome") == "still_active":
        final_outcome = "still_active"
    else:
        final_outcome = "unknown"

    if not intervals:
        recorder.flag(
            repo, issue, "no_triage_interval",
            "cohort issue has no triage ownership interval (never assigned to a triage team member)",
        )

    recorder.issue_meta.setdefault(f"{repo}#{issue['number']}", {})["post_dev_returns"] = post_dev_returns

    if issue.get("assignees") and len([a for a in issue["assignees"]]) > 1:
        recorder.flag(
            repo, issue, "multi_assignee",
            "issue has multiple simultaneous assignees; only the primary assignee timeline was tracked",
        )

    if not default_assignee:
        recorder.flag(
            repo, issue, "default_assignee_unknown",
            "no default assignee entry effective at creation time; default_assignee_at_creation is unknown",
        )

    return intervals, final_outcome


# ---------------------------------------------------------------------------
# Metrics (documented aggregates, also recomputed client-side in the dashboard)
# ---------------------------------------------------------------------------


def _valid_cycle(values: List[float]) -> Dict[str, Optional[float]]:
    return {
        "avg_h": round(statistics.mean(values), 3) if values else None,
        "median_h": median(values),
        "p75_h": percentile(values, 75),
        "p90_h": percentile(values, 90),
        "count": len(values),
    }


def compute_metrics(intervals: List[Dict[str, Any]], active_intervals: List[Dict[str, Any]], issue_summary: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
    triagers = sorted({i["triage_username"] for i in intervals})
    months = sorted({i["issue_creation_month"] for i in intervals})

    workload: Dict[str, Dict[str, Dict[str, int]]] = {}
    for tr in triagers:
        workload[tr] = {}
        for m in months:
            work_set = [i for i in intervals if i["triage_username"] == tr and i["issue_creation_month"] == m]
            workload[tr][m] = {
                "handled": len(work_set),
                "completed": sum(1 for i in work_set if i["ownership_end"]),
                "resolved_by_triager": sum(1 for i in work_set if i["issue_outcome"] == "resolved_by_triager"),
                "handed_off_to_development": sum(1 for i in work_set if i["issue_outcome"] == "handed_off_to_development"),
                "spam_or_invalid": sum(1 for i in work_set if i["issue_outcome"] == "spam_or_invalid"),
                "handoffs_received": sum(1 for i in work_set if i["start_type"] == "triage_handoff"),
                "handoffs_made": sum(1 for i in work_set if i["ownership_end_reason"] == "handoff_to_triage"),
            }

    # cycle time: completed, non-spam
    cycle_time: Dict[str, Dict[str, dict]] = {}
    for tr in triagers:
        cycle_time[tr] = {}
        for m in months:
            vals = [
                i["ownership_duration_hours"]
                for i in intervals
                if i["triage_username"] == tr
                and i["issue_creation_month"] == m
                and i["ownership_end"]
                and i["issue_outcome"] != "spam_or_invalid"
                and i["ownership_duration_hours"] is not None
            ]
            cycle_time[tr][m] = _valid_cycle(vals)

    # time to development handoff (per-interval triager ownership)
    dev: Dict[str, Dict[str, dict]] = {}
    for tr in triagers:
        dev[tr] = {}
        for m in months:
            vals = [
                i["ownership_duration_hours"]
                for i in intervals
                if i["triage_username"] == tr
                and i["issue_creation_month"] == m
                and i["issue_outcome"] == "handed_off_to_development"
                and i["ownership_duration_hours"] is not None
            ]
            dev[tr][m] = _valid_cycle(vals)

    # ISSUE-LEVEL time to development handoff: from the FIRST triage assignment
    # (earliest triage-interval start) until the first handoff to a dev user.
    # This is the full triage lead time, covering multi-triager chains (e.g.
    # routing + hard-issue handoffs) rather than only the last triager's span.
    by_issue: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)
    for i in intervals:
        by_issue[(i["repository"], i["issue_number"])].append(i)
    dev_first: List[float] = []
    for rows in by_issue.values():
        dev_ends: List[datetime] = []
        for r in rows:
            t = parse_ts(r["ownership_end"])
            if t is not None and r["issue_outcome"] == "handed_off_to_development":
                dev_ends.append(t)
        starts = [t for t in (parse_ts(r["ownership_start"]) for r in rows) if t is not None]
        if not dev_ends or not starts:
            continue
        first = min(starts)
        dev_at = min(dev_ends)
        if dev_at >= first:
            dev_first.append(duration_hours(first, dev_at))
    dev_first_metric = _valid_cycle(dev_first)

    outcomes: Dict[str, int] = {}
    for i in intervals:
        outcomes[i["issue_outcome"]] = outcomes.get(i["issue_outcome"], 0) + 1

    active_by_triager: Dict[str, int] = {}
    active_buckets: Dict[str, int] = {"<24h": 0, "24-48h": 0, "48-72h": 0, "72h+": 0}
    collected = parse_ts(meta["collected_at"])
    for a in active_intervals:
        owner = a["triage_username"]
        active_by_triager[owner] = active_by_triager.get(owner, 0) + 1
        st = parse_ts(a["ownership_start"])
        hours = duration_hours(st, collected)
        bucket = "<24h" if hours < 24 else ("24-48h" if hours < 48 else ("48-72h" if hours < 72 else "72h+"))
        active_buckets[bucket] += 1

    sample_buckets = {m: m for m in months}  # informational: creation months present

    return {
        "meta": meta,
        "summary": {
            "issues_in_cohort": len(issue_summary),
            "ownership_intervals": len(intervals),
            "completed_intervals": sum(1 for i in intervals if i["ownership_end"]),
            "active_intervals": len(active_intervals),
            "handed_off_to_development": sum(1 for i in intervals if i["issue_outcome"] == "handed_off_to_development"),
            "resolved_by_triager": sum(1 for i in intervals if i["issue_outcome"] == "resolved_by_triager"),
            "spam_or_invalid": sum(1 for i in intervals if i["issue_outcome"] == "spam_or_invalid"),
            "older_than_72h": active_buckets.get("72h+", 0),
            "older_than_48h": active_buckets.get("48-72h", 0) + active_buckets.get("72h+", 0),
            "excluded_issues": meta.get("excluded_issues", 0),
            "excluded_by_creator": meta.get("excluded_by_creator", 0),
            "excluded_by_label": meta.get("excluded_by_label", 0),
            "excluded_by_repo": meta.get("excluded_by_repo", 0),
            "excluded_by_team": meta.get("excluded_by_team", 0),
            "months": months,
            "sample_buckets": sample_buckets,
        },
        "workload": workload,
        "cycle_time": cycle_time,
        "dev_handoff_time": dev,
        "dev_handoff_from_first_assignment": dev_first_metric,
        "outcomes": outcomes,
        "aging": {
            "by_triager": active_by_triager,
            "by_age_bucket": active_buckets,
        },
    }


def build_issue_summary(intervals_by_issue, repo, issue, final_outcome, config, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    default_assignee, method = resolve_default_assignee(config, repo, parse_ts(issue["created_at"]))
    active = any(i.get("issue_outcome") == "still_active" for i in intervals_by_issue)
    extra = extra or {}
    return {
        "repository": repo,
        "issue_number": issue["number"],
        "issue_url": issue.get("html_url"),
        "issue_title": issue.get("title"),
        "created_at": issue.get("created_at"),
        "creation_month": ym(parse_ts(issue["created_at"])),
        "default_assignee_at_creation": default_assignee,
        "default_assignee_method": method,
        "current_assignee": issue.get("assignee"),
        "current_state": issue.get("state"),
        "final_outcome": final_outcome,
        "n_ownership_intervals": len(intervals_by_issue),
        "is_active": active,
        "post_dev_returns": extra.get("post_dev_returns", 0),
    }


# ---------------------------------------------------------------------------
# Pipeline driver
# ---------------------------------------------------------------------------


def is_excluded_issue(issue: Dict[str, Any], excluded_creators) -> bool:
    """True if the issue's creator is in the configured exclude (bot) list."""
    return bool(excluded_creators) and str(issue.get("user") or "").casefold() in excluded_creators


def is_excluded_by_label(issue: Dict[str, Any], excluded_labels) -> bool:
    """True if any configured exclude label was applied to the issue.

    Signal = the label is present in the issue's current labels OR was applied
    via a `labeled` event (even if later removed).
    """
    if not excluded_labels:
        return False
    wanted = {str(l).casefold() for l in excluded_labels}
    current = {str(l).casefold() for l in (issue.get("labels") or [])}
    if current & wanted:
        return True
    for e in issue.get("events", []):
        if e.get("event") == "labeled" and e.get("label") and str(e["label"]).casefold() in wanted:
            return True
    return False


def is_excluded_by_repo_era(repo: str, created: datetime, exclude_repo_before) -> bool:
    """True if the issue belongs to a repo-era we exclude (e.g. a previous
    default-owner's period) — created before the configured cutoff in that repo."""
    cutoff = (exclude_repo_before or {}).get(repo)
    if not cutoff:
        return False
    return created < parse_ts(str(cutoff))


def is_bot_actor(login: Optional[str], config: Dict[str, Any]) -> bool:
    """True if the actor of an event looks like a bot.

    Detected when the login ends with '[bot]' (GitHub convention) or is listed
    in config `bot_actors`. Used to flag automation-performed reassignments so
    they are auditable (e.g. a bulk coverage/vacation reroute) instead of being
    indistinguishable from a genuine triage handoff.
    """
    if not login:
        return False
    l = str(login).casefold()
    if l.endswith("[bot]"):
        return True
    return l in {str(b).casefold() for b in (config.get("bot_actors") or [])}


def run(config: Dict[str, Any], raw: Dict[str, Any], out_dir: str, dashboard_template: str, dashboard_out: str) -> Dict[str, Any]:
    cohort = config["cohort"]
    # The collector records the EXACT window it actually fetched in the snapshot
    # meta (collect.py --cohort last writes the computed last period there). That
    # authoritative window must win over a stale config.yaml default so analysis
    # always matches collection. Fall back to config only for snapshots that do
    # not carry the window (legacy files / test fixtures).
    raw_meta = raw.get("meta", {}) or {}
    raw_start = raw_meta.get("cohort_start")
    raw_end = raw_meta.get("cohort_end")
    if raw_start and raw_end:
        start_dt = parse_ts(str(raw_start))
        end_dt = parse_ts(str(raw_end))
        cohort_source = "snapshot"
    else:
        start_dt = parse_ts(str(cohort["start_date"]))
        end_dt = parse_ts(str(cohort["end_date"]))
        if config.get("cohort", {}).get("include_aug_1"):
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
        cohort_source = "config"

    collected_at = raw.get("meta", {}).get("collected_at", datetime.now(timezone.utc).isoformat())
    recorder = Recorder(config, collected_at)

    excluded_creators = {str(u).casefold() for u in (config.get("exclude_creators") or [])}
    excluded_labels = [str(l).casefold() for l in (config.get("exclude_labels") or [])]
    exclude_repo_before = config.get("exclude_repo_before") or {}
    exclude_team_created = bool(config.get("exclude_team_created_issues"))
    team_set = {str(u).casefold() for u in config.get("triage_team", [])}
    excluded_by_creator = 0
    excluded_by_label = 0
    excluded_by_repo = 0
    excluded_by_team = 0

    all_intervals: List[Dict[str, Any]] = []
    issue_summary: List[Dict[str, Any]] = []
    for repo, block in raw.get("repos", {}).items():
        for issue in block.get("issues", []):
            created = parse_ts(issue.get("created_at"))
            if created is None or not is_in_cohort(created, start_dt, end_dt):
                continue
            if exclude_team_created and str(issue.get("user") or "").casefold() in team_set:
                # created by a triage team member: assumed already triaged and
                # passed straight to developers -> excluded from the cohort
                excluded_by_team += 1
                continue
            if is_excluded_by_repo_era(repo, created, exclude_repo_before):
                # previous owner's era in this repo: excluded from the cohort
                excluded_by_repo += 1
                continue
            if is_excluded_issue(issue, excluded_creators):
                # bot/automation-created issue: kept in raw for auditability but
                # excluded from the cohort from the start (no intervals, no DQ)
                excluded_by_creator += 1
                continue
            if is_excluded_by_label(issue, excluded_labels):
                # spam/invalid-labeled issue: excluded from the cohort entirely
                excluded_by_label += 1
                continue
            intervals, final_outcome = reconstruct_issue_intervals(repo, issue, config, recorder)
            all_intervals.extend(intervals)
            extra = recorder.issue_meta.get(f"{repo}#{issue['number']}", {})
            issue_summary.append(build_issue_summary(intervals, repo, issue, final_outcome, config, extra))

    active_intervals = [i for i in all_intervals if i["issue_outcome"] == "still_active"]
    meta = {
        "collected_at": collected_at,
        "cohort_start": iso(start_dt),
        "cohort_end": iso(end_dt),
        "cohort_source": cohort_source,
        "excluded_issues": excluded_by_creator + excluded_by_label + excluded_by_repo + excluded_by_team,
        "excluded_by_creator": excluded_by_creator,
        "excluded_by_label": excluded_by_label,
        "excluded_by_repo": excluded_by_repo,
        "excluded_by_team": excluded_by_team,
        "excluded_creators": sorted(excluded_creators),
        "excluded_labels": sorted(excluded_labels),
        "exclude_repo_before": {r: str(v) for r, v in exclude_repo_before.items()},
    }
    metrics = compute_metrics(all_intervals, active_intervals, issue_summary, meta)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "triage_ownership.json"), "w", encoding="utf-8") as fh:
        json.dump({"meta": metrics["meta"], "intervals": all_intervals}, fh, indent=2)
    with open(os.path.join(out_dir, "issue_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(issue_summary, fh, indent=2)
    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)
    with open(os.path.join(out_dir, "data_quality.json"), "w", encoding="utf-8") as fh:
        json.dump(recorder.dq, fh, indent=2)

    embedded = {
        "meta": metrics["meta"],
        "config": {
            "repositories": config["repositories"],
            "triage_team": config["triage_team"],
        },
        "intervals": all_intervals,
        "issue_summary": issue_summary,
        "data_quality": recorder.dq,
        "metrics": metrics,
    }
    with open(dashboard_template, "r", encoding="utf-8") as fh:
        template = fh.read()
    marker = "/*__EMBEDDED_DATA__*/"
    if marker not in template:
        raise SystemExit(f"dashboard template missing marker {marker!r}")
    rendered = template.replace(marker, json.dumps(embedded))
    with open(dashboard_out, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    print(f"[analyze] wrote processed data -> {out_dir}")
    print(f"[analyze] wrote dashboard       -> {dashboard_out}")
    print(f"[analyze] issues={len(issue_summary)} intervals={len(all_intervals)} "
          f"active={len(active_intervals)} dq_flags={len(recorder.dq)}")
    return metrics


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub Triage Metrics — analyze + dashboard")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--raw", default="data/raw/raw_snapshot.json",
                    help="raw snapshot (collect.py output, or mock/raw_snapshot.json)")
    ap.add_argument("--out-dir", default="data/processed")
    ap.add_argument("--template", default="dashboard/template.html")
    ap.add_argument("--dashboard-out", default="dashboard/dashboard.html")
    args = ap.parse_args()

    config = load_config(args.config)
    if not os.path.exists(args.raw):
        print(f"raw snapshot not found: {args.raw}\n"
              "  - fetch it with:  python collect.py   (needs GITHUB_TOKEN)\n"
              "  - or demo offline: python make_mock_data.py && python analyze.py --raw mock/raw_snapshot.json")
        return 1
    with open(args.raw, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    run(config, raw, args.out_dir, args.template, args.dashboard_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
