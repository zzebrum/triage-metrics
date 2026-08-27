"""
Unit tests for the classification logic and duration calculations.

Covers the 11 classification scenarios from the spec, the cohort / late-
completion rules, and basic metric helpers. No network access — everything is
synthetic.

Run:  pytest -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analyze import (
    Recorder,
    assignee_at_creation,
    build_assignee_timeline,
    compute_metrics,
    duration_hours,
    is_bot_actor,
    is_excluded_by_label,
    is_excluded_by_repo_era,
    is_excluded_issue,
    is_in_cohort,
    last_period_window,
    percentile,
    reconstruct_issue_intervals,
    resolve_default_assignee,
    stable_interval_id,
    median,
)
from make_mock_data import make_issue

REPO = "AdguardTeam/AdguardForAndroid"
REPO_TT = "TrustTunnel/TrustTunnelFlutterClient"
TEAM = ["Swen90", "maxikuzmin", "ESurina", "oksenina"]
COLLECTED = "2026-08-02T00:00:00Z"


def base_config(defaults=None, spam_labels=None, start="2026-02-01", end="2026-08-01"):
    cfg = {
        "repositories": [REPO, REPO_TT],
        "triage_team": TEAM,
        "cohort": {"start_date": start, "end_date": end, "include_aug_1": False},
        "default_assignee_history": defaults or {
            REPO: [{"effective_from": "2026-02-01", "assignee": "maxikuzmin"}],
            REPO_TT: [{"effective_from": "2026-02-01", "assignee": "oksenina"}],
        },
        "spam_labels": spam_labels or [],
        "bot_actors": ["adguard-bot", "adguard-octobuddy[bot]"],
        "cut_triage_after_dev_handoff": True,
        "default_assign_window_seconds": 300,
        "exclude_team_created_issues": False,
        "api": {},
    }
    return cfg


def run(repo, issue, config, spam_labels=None, collected=COLLECTED):
    cfg = dict(config)
    cfg["spam_labels"] = spam_labels if spam_labels is not None else config.get("spam_labels", [])
    rec = Recorder(cfg, collected)
    intervals, final = reconstruct_issue_intervals(repo, issue, cfg, rec)
    return intervals, final, rec


def first_day(hour=9):
    return "2026-06-10T09:00:00Z"


# ---------------------------------------------------------------------------
# Test 1 — default assignee keeps the issue, then hand to development
# ---------------------------------------------------------------------------


def test_1_default_keeps_then_dev():
    issue = make_issue(
        REPO, 1, "keeps", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("assigned", 6.0, "maxikuzmin", "dev-agent", None),
            ("unassigned", 6.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 1.0)],
    )
    intervals, final, _ = run(REPO, issue, base_config())
    assert len(intervals) == 1
    itv = intervals[0]
    assert itv["triage_username"] == "maxikuzmin"
    assert itv["start_type"] == "accepted_from_default"
    assert itv["ownership_start"] == "2026-06-10T09:00:00Z"
    assert itv["ownership_end"] == "2026-06-10T15:00:00Z"
    assert itv["ownership_end_reason"] == "handoff_to_non_triage"
    assert itv["issue_outcome"] == "handed_off_to_development"
    assert itv["next_assignee"] == "dev-agent"
    assert itv["previous_assignee"] is None
    assert final == "handed_off_to_development"


# ---------------------------------------------------------------------------
# Test 2 — initial routing (default did nothing) -> Bob -> dev
# ---------------------------------------------------------------------------


def test_2_initial_routing():
    issue = make_issue(
        REPO, 2, "routing", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 1.0, None, "maxikuzmin", None),
            ("assigned", 1.0, "ESurina", "ESurina", None),
            ("unassigned", 6.0, None, "ESurina", None),
            ("assigned", 6.0, "ESurina", "dev-agent", None),
        ],
        [("ESurina", 2.0)],
    )
    intervals, final, _ = run(REPO, issue, base_config())
    # Alice (maxikuzmin) must NOT have an ownership interval
    assert len(intervals) == 1
    itv = intervals[0]
    assert itv["triage_username"] == "ESurina"
    assert itv["start_type"] == "initial_routing"
    assert itv["transition_type"] == "initial_routing"
    assert itv["originated_from_initial_routing"] is True
    assert itv["previous_assignee"] == "maxikuzmin"
    assert itv["ownership_start"] == "2026-06-10T10:00:00Z"
    assert itv["issue_outcome"] == "handed_off_to_development"
    assert final == "handed_off_to_development"


# ---------------------------------------------------------------------------
# Test 3 — actual triage handoff chain (default -> Bob -> Carol -> dev)
# ---------------------------------------------------------------------------


def test_3_triage_handoff_chain():
    issue = make_issue(
        REPO, 3, "chain", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 1.0, None, "maxikuzmin", None),
            ("assigned", 1.0, "ESurina", "ESurina", None),   # Bob
            ("unassigned", 30.0, "ESurina", "ESurina", None),
            ("assigned", 30.0, "ESurina", "Swen90", None),  # Carol
            ("unassigned", 60.0, "Swen90", "Swen90", None),
            ("assigned", 60.0, "Swen90", "dev-agent", None),
        ],
        [("ESurina", 2.0), ("Swen90", 32.0)],
    )
    intervals, final, _ = run(REPO, issue, base_config())
    # default (maxikuzmin) never owns -> only Bob and Carol intervals
    assert len(intervals) == 2
    bob, carol = intervals[0], intervals[1]
    assert bob["triage_username"] == "ESurina"
    assert bob["start_type"] == "initial_routing"
    assert bob["ownership_end_reason"] == "handoff_to_triage"
    assert bob["issue_outcome"] == "handoff_to_triage"
    assert bob["next_assignee"] == "Swen90"
    assert carol["triage_username"] == "Swen90"
    assert carol["start_type"] == "triage_handoff"
    assert carol["originated_from_triage_handoff"] is True
    assert carol["previous_assignee"] == "ESurina"
    assert carol["ownership_end_reason"] == "handoff_to_non_triage"
    assert carol["issue_outcome"] == "handed_off_to_development"
    assert final == "handed_off_to_development"


# ---------------------------------------------------------------------------
# Test 4 — triager resolves the issue directly
# ---------------------------------------------------------------------------


def test_4_triager_resolves():
    issue = make_issue(
        REPO, 4, "question", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("closed", 4.0, "maxikuzmin", None, None),
            ("unassigned", 4.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 1.0)], state="closed", closed_at="2026-06-10T13:00:00Z",
    )
    intervals, final, _ = run(REPO, issue, base_config())
    assert len(intervals) == 1
    itv = intervals[0]
    assert itv["issue_outcome"] == "resolved_by_triager"
    # still part of workload / cycle time, but NOT time-to-development-handoff
    assert itv["ownership_duration_hours"] == pytest.approx(4.0)
    assert itv["issue_outcome"] != "handed_off_to_development"
    assert final == "resolved_by_triager"


# ---------------------------------------------------------------------------
# Test 5 — spam/invalid (explicit label signal only)
# ---------------------------------------------------------------------------


def test_5_spam_label():
    issue = make_issue(
        REPO, 5, "spammy", first_day(), "spammer",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("labeled", 0.5, "maxikuzmin", None, "spam"),
            ("closed", 1.0, "maxikuzmin", None, None),
            ("unassigned", 1.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 0.25)], state="closed", closed_at="2026-06-10T10:00:00Z",
    )
    intervals, final, _ = run(REPO, issue, base_config(), spam_labels=["spam"])
    assert len(intervals) == 1
    itv = intervals[0]
    # work stays in the raw dataset
    assert itv["issue_outcome"] == "spam_or_invalid"
    assert itv["ownership_end_reason"] == "issue_closed"
    assert final == "spam_or_invalid"
    # excluded from development handoff metric
    assert itv["issue_outcome"] != "handed_off_to_development"


# ---------------------------------------------------------------------------
# Test 6 — fast normal question is NOT spam without a signal
# ---------------------------------------------------------------------------


def test_6_fast_question_not_spam():
    issue = make_issue(
        REPO, 6, "how do I upgrade?", first_day(), "user",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("closed", 1.0, "maxikuzmin", None, None),
            ("unassigned", 1.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 0.5)], state="closed", closed_at="2026-06-10T10:00:00Z",
    )
    intervals, final, _ = run(REPO, issue, base_config(), spam_labels=["spam"])
    assert len(intervals) == 1
    # quick closure alone is not spam
    assert intervals[0]["issue_outcome"] == "resolved_by_triager"
    assert final == "resolved_by_triager"


# ---------------------------------------------------------------------------
# Test 7 — unassignment ends the interval
# ---------------------------------------------------------------------------


def test_7_unassigned():
    issue = make_issue(
        REPO, 7, "unassign", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 12.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 1.0)],
    )
    intervals, final, _ = run(REPO, issue, base_config())
    assert len(intervals) == 1
    assert intervals[0]["ownership_end_reason"] == "unassigned"
    assert intervals[0]["issue_outcome"] == "unassigned"
    assert intervals[0]["next_assignee"] is None
    assert final == "unassigned"


# ---------------------------------------------------------------------------
# Test 8 — two separate intervals for the same triager
# ---------------------------------------------------------------------------


def test_8_multiple_intervals():
    issue = make_issue(
        REPO, 8, "back and forth", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("assigned", 5.0, "maxikuzmin", "dev-agent", None),
            ("unassigned", 5.0, None, "maxikuzmin", None),
            ("assigned", 30.0, "dev-agent", "maxikuzmin", None),
            ("unassigned", 48.0, None, "maxikuzmin", None),
            ("assigned", 48.0, "maxikuzmin", "dev-agent", None),
        ],
        [("maxikuzmin", 2.0), ("maxikuzmin", 32.0)],
    )
    # Default (cut_triage_after_dev_handoff=true): the second re-engagement after
    # the first dev handoff is a verification return -> cut, not a new interval.
    cfg = base_config()
    intervals, final, rec = run(REPO, issue, cfg)
    assert len(intervals) == 1
    assert intervals[0]["triage_username"] == "maxikuzmin"
    assert intervals[0]["issue_outcome"] == "handed_off_to_development"
    assert rec.issue_meta[f"{REPO}#{issue['number']}"]["post_dev_returns"] == 1

    # With the cut disabled the original spec behaviour holds: two intervals.
    cfg["cut_triage_after_dev_handoff"] = False
    intervals2, final2, _ = run(REPO, issue, cfg)
    assert len(intervals2) == 2
    assert all(i["triage_username"] == "maxikuzmin" for i in intervals2)
    assert [i["issue_outcome"] for i in intervals2] == [
        "handed_off_to_development",
        "handed_off_to_development",
    ]
    # two distinct stable IDs
    assert intervals2[0]["interval_id"] != intervals2[1]["interval_id"]


# ---------------------------------------------------------------------------
# Test 9 — active issue
# ---------------------------------------------------------------------------


def test_9_active():
    issue = make_issue(
        REPO, 9, "active", "2026-07-15T09:00:00Z", "reporter",
        [("assigned", 0.0, None, "maxikuzmin", None)],
        [("maxikuzmin", 1.0)],
    )
    intervals, final, _ = run(REPO, issue, base_config())
    assert len(intervals) == 1
    itv = intervals[0]
    assert itv["issue_outcome"] == "still_active"
    assert itv["ownership_end"] is None
    assert itv["ownership_duration_hours"] is None
    assert final == "still_active"
    # not part of completed cycle-time inputs
    # (compute_metrics only uses completed, non-spam intervals for cycle time)
    completed_vals = [
        i["ownership_duration_hours"]
        for i in intervals
        if i["ownership_end"] and i["issue_outcome"] != "spam_or_invalid"
    ]
    assert completed_vals == []


# ---------------------------------------------------------------------------
# Test 10 — changing default assignee (per repository history)
# ---------------------------------------------------------------------------


def test_10_changing_default():
    cfg = base_config(
        defaults={
            REPO: [
                {"effective_from": "2026-05-01", "assignee": "maxikuzmin"},
                {"effective_from": "2026-07-01", "assignee": "oksenina"},
            ]
        }
    )

    # before the change -> maxikuzmin
    assert resolve_default_assignee(cfg, REPO, datetime(2026, 6, 15, tzinfo=timezone.utc)) == (
        "maxikuzmin", "manually_configured",
    )
    # after the change -> oksenina
    assert resolve_default_assignee(cfg, REPO, datetime(2026, 7, 15, tzinfo=timezone.utc)) == (
        "oksenina", "manually_configured",
    )
    # before the first effective_from -> unknown + Data Quality flag
    assert resolve_default_assignee(cfg, REPO, datetime(2026, 4, 20, tzinfo=timezone.utc)) == (
        None, "unknown",
    )

    issue_a = make_issue(
        REPO, 10, "created in june", "2026-06-15T09:00:00Z", "r",
        [("assigned", 0.0, None, "maxikuzmin", None), ("unassigned", 2.0, None, "maxikuzmin", None)],
        [], state="open",
    )
    intervals_a, _, _ = run(REPO, issue_a, cfg)
    assert intervals_a[0]["default_assignee_at_creation"] == "maxikuzmin"

    issue_b = make_issue(
        REPO, 10, "created in july", "2026-07-15T09:00:00Z", "r",
        [("assigned", 0.0, None, "oksenina", None), ("unassigned", 2.0, None, "oksenina", None)],
        [], state="open",
    )
    intervals_b, _, _ = run(REPO, issue_b, cfg)
    assert intervals_b[0]["default_assignee_at_creation"] == "oksenina"

    issue_c = make_issue(
        REPO, 10, "created in april", "2026-04-20T09:00:00Z", "r",
        [("assigned", 0.0, None, "maxikuzmin", None), ("unassigned", 2.0, None, "maxikuzmin", None)],
        [], state="open",
    )
    intervals_c, _, rec = run(REPO, issue_c, cfg)
    assert intervals_c[0]["default_assignee_at_creation"] is None
    assert intervals_c[0]["default_assignee_resolution_method"] == "unknown"
    assert any(f["category"] == "default_assignee_unknown" for f in rec.dq)


# ---------------------------------------------------------------------------
# Test 11 — routing vs handoff decision is deterministic (activity-based)
# ---------------------------------------------------------------------------


def test_11_deterministic_routing_rule():
    # (a) default did NOTHING observable -> initial routing (no default interval)
    silent = make_issue(
        REPO, 11, "silent default", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 1.0, None, "maxikuzmin", None),
            ("assigned", 1.0, "ESurina", "ESurina", None),
        ],
        [],
    )
    intervals_s, _, _ = run(REPO, silent, base_config())
    assert len(intervals_s) == 1
    assert intervals_s[0]["start_type"] == "initial_routing"

    # (b) default commented BEFORE the transfer -> real handoff (default interval exists)
    active = make_issue(
        REPO, 11, "active default", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 10.0, None, "maxikuzmin", None),
            ("assigned", 10.0, "maxikuzmin", "ESurina", None),
        ],
        [("maxikuzmin", 2.0)],
    )
    intervals_a, _, _ = run(REPO, active, base_config())
    assert len(intervals_a) == 2
    assert intervals_a[0]["triage_username"] == "maxikuzmin"
    assert intervals_a[0]["ownership_end_reason"] == "handoff_to_triage"
    assert intervals_a[1]["triage_username"] == "ESurina"
    assert intervals_a[1]["start_type"] == "triage_handoff"
    assert intervals_a[1]["transition_type"] == "triage_handoff"

    # (c) evaluation order matters: activity AFTER the handoff is not counted as
    #     "the default worked it" — the handoff stays initial routing
    late_comment = make_issue(
        REPO, 11, "late comment", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 1.0, None, "maxikuzmin", None),
            ("assigned", 1.0, "maxikuzmin", "ESurina", None),
        ],
        [("maxikuzmin", 5.0)],  # comment AFTER the handoff
    )
    intervals_l, _, _ = run(REPO, late_comment, base_config())
    assert len(intervals_l) == 1
    assert intervals_l[0]["start_type"] == "initial_routing"

    # NOTE: with complete data the rule never yields `unknown`; `unknown` is
    # reserved for genuinely incomplete event/comment history (collection
    # failure), which the collect layer flags in Data Quality.


# ---------------------------------------------------------------------------
# Cohort and late-completion rules
# ---------------------------------------------------------------------------


def test_cohort_late_completion():
    # created in cohort (July), completed after cohort end (Aug 20) -> included,
    # full duration, cohort month July, completion month August. NOT truncated.
    cfg = base_config()
    issue = make_issue(
        REPO, 12, "late", "2026-07-15T09:00:00Z", "r",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("assigned", 744.0, "maxikuzmin", "dev-agent", None),  # +31 days -> Aug 15
            ("unassigned", 744.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 1.0)],
    )
    intervals, final, _ = run(REPO, issue, cfg)
    assert final == "handed_off_to_development"
    itv = intervals[0]
    assert itv["issue_creation_month"] == "2026-07"
    assert itv["ownership_completion_month"] == "2026-08"
    # duration spans the full 31 days, not cut at Aug 1
    assert itv["ownership_duration_hours"] == pytest.approx(744.0)


def test_cohort_active_after_end():
    issue = make_issue(
        REPO, 13, "still open", "2026-07-15T09:00:00Z", "r",
        [("assigned", 0.0, None, "maxikuzmin", None)],
        [("maxikuzmin", 1.0)],
    )
    intervals, final, _ = run(REPO, issue, base_config())
    assert final == "still_active"
    assert intervals[0]["ownership_end"] is None
    assert intervals[0]["issue_outcome"] == "still_active"


def test_cohort_boundaries():
    cfg = base_config()
    start = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert is_in_cohort(datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc), start, end) is False
    assert is_in_cohort(datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc), start, end) is True
    assert is_in_cohort(datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc), start, end) is True
    assert is_in_cohort(datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc), start, end) is False


# ---------------------------------------------------------------------------
# helpers & durations
# ---------------------------------------------------------------------------


def test_duration_hours():
    s = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    e = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)
    assert duration_hours(s, e) == pytest.approx(12.5)
    assert duration_hours(e, e) == 0.0


def test_percentile_and_median():
    vals = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert pytest.approx(median(vals)) == 3.0
    assert pytest.approx(percentile(vals, 75)) == 4.0
    assert pytest.approx(percentile(vals, 90)) == 100.0
    assert percentile([], 75) is None
    assert percentile([5.0], 50) == 5.0


def test_stable_interval_id_deterministic_and_start_anchored():
    a1 = stable_interval_id(REPO, 1, "maxikuzmin", "2026-06-10T09:00:00Z")
    a2 = stable_interval_id(REPO, 1, "maxikuzmin", "2026-06-10T09:00:00Z")
    b = stable_interval_id(REPO, 1, "maxikuzmin", "2026-06-10T10:00:00Z")
    assert a1 == a2
    assert a1 != b
    # end timestamp is intentionally not part of the ID
    assert a1 == stable_interval_id(REPO, 1, "maxikuzmin", "2026-06-10T09:00:00Z")


def test_build_assignee_timeline_collapses_same_second():
    events = [
        {"id": 1, "created_at": "2026-06-01T10:00:00Z", "event": "assigned", "assignee": "alice", "actor": None},
        {"id": 2, "created_at": "2026-06-01T10:00:00Z", "event": "unassigned", "assignee": "alice", "actor": None},
        {"id": 3, "created_at": "2026-06-01T10:00:00Z", "event": "assigned", "assignee": "bob", "actor": None},
    ]
    created = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    trans = build_assignee_timeline(events, created)
    # no phantom None in between: alice -> bob directly
    assert ("2026-06-01T10:00:00Z", "bob") in trans
    assert assignee_at_creation(trans, created) == "bob"


def test_exclude_creators_helper():
    excluded = {"adguard-octobuddy[bot]", "adguard-bot"}
    assert is_excluded_issue({"user": "adguard-octobuddy[bot]"}, excluded) is True
    assert is_excluded_issue({"user": "AdGuard-Bot"}, excluded) is True  # case-insensitive
    assert is_excluded_issue({"user": "reporter"}, excluded) is False
    assert is_excluded_issue({"user": "adguard-octobuddy[bot]"}, set()) is False  # no rule = keep


def test_run_excludes_bot_issues_from_cohort(tmp_path):
    import json

    import analyze

    cfg = base_config()
    cfg["exclude_creators"] = ["adguard-octobuddy[bot]", "adguard-bot"]
    bot = make_issue(
        REPO, 40, "bot issue", "2026-06-10T09:00:00Z", "adguard-octobuddy[bot]",
        [("assigned", 0.0, None, "maxikuzmin", None), ("closed", 2.0, "maxikuzmin", None, None)],
        [], state="closed", closed_at="2026-06-10T11:00:00Z",
    )
    human = make_issue(
        REPO, 41, "human issue", "2026-06-11T09:00:00Z", "reporter",
        [("assigned", 0.0, None, "maxikuzmin", None), ("closed", 3.0, "maxikuzmin", None, None)],
        [("maxikuzmin", 1.0)], state="closed", closed_at="2026-06-11T12:00:00Z",
    )
    raw = {"meta": {"collected_at": COLLECTED}, "repos": {REPO: {"issues": [bot, human]}}}
    out = tmp_path / "out"
    out.mkdir()
    template = tmp_path / "template.html"
    template.write_text("<script>const DATA = /*__EMBEDDED_DATA__*/;</script>")
    metrics = analyze.run(cfg, raw, str(out), str(template), str(out / "dash.html"))

    iss = json.loads((out / "issue_summary.json").read_text())
    assert len(iss) == 1 and iss[0]["issue_number"] == 41
    intervals = json.loads((out / "triage_ownership.json").read_text())["intervals"]
    assert len(intervals) == 1  # only the human issue's interval
    dq = json.loads((out / "data_quality.json").read_text())
    assert all(d["issue_number"] != 40 for d in dq)  # bot issue not even flagged
    assert metrics["summary"]["issues_in_cohort"] == 1
    assert metrics["summary"]["excluded_by_creator"] == 1


def test_exclude_labels_helper():
    assert is_excluded_by_label({"labels": ["spam"], "events": []}, ["spam"]) is True
    assert is_excluded_by_label({"labels": ["Spam"], "events": []}, ["spam"]) is True  # case-insens
    # applied via event even after removal from current labels
    assert is_excluded_by_label(
        {"labels": ["bug"], "events": [{"event": "labeled", "label": "spam"}]}, ["spam"]) is True
    assert is_excluded_by_label(
        {"labels": ["bug"], "events": [{"event": "labeled", "label": "enhancement"}]}, ["spam"]) is False
    assert is_excluded_by_label({"labels": ["spam"], "events": []}, []) is False  # no rule = keep


def test_run_excludes_spam_labeled_issues(tmp_path):
    import json

    import analyze

    cfg = base_config()
    cfg["exclude_labels"] = ["spam"]
    spam = make_issue(
        REPO, 50, "spammy", "2026-06-10T09:00:00Z", "spammer",
        [
            ("labeled", 0.1, "adguard-bot", None, "spam"),
            ("closed", 1.0, "adguard-bot", None, None),
        ],
        [], state="closed", closed_at="2026-06-10T10:00:00Z",
    )
    human = make_issue(
        REPO, 51, "human", "2026-06-11T09:00:00Z", "reporter",
        [("assigned", 0.0, None, "maxikuzmin", None), ("closed", 3.0, "maxikuzmin", None, None)],
        [("maxikuzmin", 1.0)], state="closed", closed_at="2026-06-11T12:00:00Z",
    )
    raw = {"meta": {"collected_at": COLLECTED}, "repos": {REPO: {"issues": [spam, human]}}}
    out = tmp_path / "out"
    out.mkdir()
    template = tmp_path / "template.html"
    template.write_text("<script>const DATA = /*__EMBEDDED_DATA__*/;</script>")
    metrics = analyze.run(cfg, raw, str(out), str(template), str(out / "dash.html"))

    iss = json.loads((out / "issue_summary.json").read_text())
    assert len(iss) == 1 and iss[0]["issue_number"] == 51
    intervals = json.loads((out / "triage_ownership.json").read_text())["intervals"]
    assert len(intervals) == 1
    dq = json.loads((out / "data_quality.json").read_text())
    assert all(d["issue_number"] != 50 for d in dq)
    assert metrics["summary"]["excluded_by_label"] == 1
    assert metrics["summary"]["issues_in_cohort"] == 1


def test_exclude_repo_era_helper():
    mapping = {"TrustTunnel/TrustTunnelFlutterClient": "2026-07-01"}
    early = datetime(2026, 6, 15, tzinfo=timezone.utc)
    late = datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert is_excluded_by_repo_era(REPO_TT, early, mapping) is True
    assert is_excluded_by_repo_era(REPO_TT, late, mapping) is False
    assert is_excluded_by_repo_era(REPO, early, mapping) is False  # repo not mapped
    assert is_excluded_by_repo_era(REPO, early, {}) is False


def test_is_bot_actor():
    cfg = base_config()
    assert is_bot_actor("adguard-bot", cfg) is True
    assert is_bot_actor("AdGuard-Bot", cfg) is True        # case-insensitive
    assert is_bot_actor("adguard-octobuddy[bot]", cfg) is True
    assert is_bot_actor("some-ci[bot]", cfg) is True        # [bot] suffix auto-detected
    assert is_bot_actor("Swen90", cfg) is False
    assert is_bot_actor(None, cfg) is False


def test_bot_reassignment_recorded_and_flagged():
    """Vacation/coverage reroute: a BOT moves an issue between two triagers.
    The interval structure is kept (still handoff_to_triage / triage_handoff),
    but the transfer actor is recorded and a bot_reassignment DQ flag is raised,
    so it is auditable as a possible bulk reroute, not a real handoff."""
    cfg = base_config()
    issue = make_issue(
        REPO, 70, "bot reroute", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),                  # default, no activity
            ("unassigned", 1.0, "maxikuzmin", "maxikuzmin", None),
            ("assigned", 1.0, "ESurina", "ESurina", None),                # routing -> ESurina
            ("assigned", 72.0, "adguard-bot", "Swen90", None),            # BOT: assign Swen90
            ("unassigned", 72.0, "adguard-bot", "ESurina", None),         # BOT: unassign ESurina
            ("assigned", 80.0, "Swen90", "dev-agent", None),              # -> dev
        ],
        [("ESurina", 3.0)],
    )
    intervals, final, rec = run(REPO, issue, cfg)
    assert len(intervals) == 2
    esurina, swen90 = intervals[0], intervals[1]
    assert esurina["triage_username"] == "ESurina"
    assert esurina["start_type"] == "initial_routing"
    assert esurina["ownership_end_reason"] == "handoff_to_triage"
    assert esurina["ownership_end_actor"] == "adguard-bot"
    assert swen90["triage_username"] == "Swen90"
    assert swen90["start_type"] == "triage_handoff"
    assert swen90["ownership_start_actor"] == "adguard-bot"
    assert swen90["issue_outcome"] == "handed_off_to_development"
    assert final == "handed_off_to_development"
    # bot-performed end is flagged on ESurina's (ending) interval row ...
    assert "reassigned_by_bot:adguard-bot" in esurina["data_quality_flags"]
    # ... but NOT on Swen90's (receiving) row, and there is one DQ record
    assert "reassigned_by_bot" not in swen90["data_quality_flags"]
    assert sum(1 for f in rec.dq if f["category"] == "bot_reassignment") == 1


def test_exclude_team_created_issues(tmp_path):
    import json

    import analyze

    # rule ON: team-member-created issue is excluded (assumed already triaged -> dev)
    cfg = base_config()
    cfg["exclude_team_created_issues"] = True
    team_issue = make_issue(
        REPO, 90, "team-created", "2026-06-05T09:00:00Z", "ESurina",
        [("assigned", 0.0, None, "maxikuzmin", None), ("unassigned", 2.0, None, "maxikuzmin", None)],
        [], state="open",
    )
    human = make_issue(
        REPO, 91, "human", "2026-06-06T09:00:00Z", "reporter",
        [("assigned", 0.0, None, "maxikuzmin", None), ("closed", 3.0, "maxikuzmin", None, None)],
        [("maxikuzmin", 1.0)], state="closed", closed_at="2026-06-06T12:00:00Z",
    )
    raw = {"meta": {"collected_at": COLLECTED}, "repos": {REPO: {"issues": [team_issue, human]}}}
    out = tmp_path / "out"
    out.mkdir()
    template = tmp_path / "template.html"
    template.write_text("<script>const DATA = /*__EMBEDDED_DATA__*/;</script>")
    metrics = analyze.run(cfg, raw, str(out), str(template), str(out / "dash.html"))
    iss = json.loads((out / "issue_summary.json").read_text())
    assert [i["issue_number"] for i in iss] == [91]
    assert metrics["summary"]["issues_in_cohort"] == 1
    assert metrics["summary"]["excluded_by_team"] == 1

    # rule OFF: the same team-created issue stays in the cohort
    cfg["exclude_team_created_issues"] = False
    out2 = tmp_path / "out2"
    out2.mkdir()
    metrics2 = analyze.run(cfg, raw, str(out2), str(template), str(out2 / "dash.html"))
    iss2 = json.loads((out2 / "issue_summary.json").read_text())
    assert {i["issue_number"] for i in iss2} == {90, 91}
    assert metrics2["summary"]["excluded_by_team"] == 0


def test_assignee_at_creation_near_creation_window():
    # GitHub records the default-assign auto-assign ~7s AFTER creation
    created = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    evts = [
        {"id": 1, "created_at": "2026-06-01T10:00:07Z", "event": "assigned", "assignee": "maxikuzmin", "actor": None},
    ]
    trans = build_assignee_timeline(evts, created)
    assert assignee_at_creation(trans, created, window_seconds=300) == "maxikuzmin"
    assert assignee_at_creation(trans, created, window_seconds=1) is None  # outside tiny window


def test_activity_ignores_pure_assignment_events():
    """A default holder whose ONLY observed events are assignment mechanics did
    NOT('t work the issue: passing it on is initial routing, not a handoff."""
    cfg = base_config()
    issue = make_issue(
        REPO, 71, "routed by default", first_day(), "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 1.0, "maxikuzmin", "maxikuzmin", None),
            ("assigned", 1.0, "ESurina", "ESurina", None),
        ],
        [],  # no comments/labels by maxikuzmin anywhere
    )
    intervals, final, rec = run(REPO, issue, cfg)
    # default gets NO interval; ESurina = initial_routing
    assert len(intervals) == 1
    assert intervals[0]["triage_username"] == "ESurina"
    assert intervals[0]["start_type"] == "initial_routing"
    assert intervals[0]["previous_assignee"] == "maxikuzmin"


def test_cut_after_dev_handoff(tmp_path):
    """Once handed off to dev, later returns to a triager ('ready to check') do
    NOT open new triage intervals; they are only counted as post_dev_returns.

    Mirrors AdguardForWindows #5869:
      default -> KolbasovAnton (routes, no activity) -> dev  [real triage->dev]
      dev -> dev (churn)
      dev -> KolbasovAnton ("Ready for QA") -> dev             [cut]
      (reopen) dev -> KolbasovAnton -> dev -> closed           [cut]
    """
    cfg = base_config()
    issue = make_issue(
        REPO, 72, "5869-like", "2026-02-27T13:03:41Z", "reporter",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),                      # default
            ("unassigned", 100.0, "maxikuzmin", "maxikuzmin", None),
            ("assigned", 100.0, "ESurina", "ESurina", None),                  # routing -> ESurina
            ("unassigned", 124.0, "ESurina", "ESurina", None),
            ("assigned", 124.0, "dev-agent", "dev-agent", None),              # -> dev  (triage done)
            ("assigned", 1300.0, "dev-agent2", "dev-agent2", None),           # dev->dev churn
            ("unassigned", 1300.0, "dev-agent", "dev-agent", None),
            ("assigned", 1380.0, "ESurina", "ESurina", None),                 # READY TO CHECK return
            ("unassigned", 1420.0, "ESurina", "ESurina", None),
            ("assigned", 1420.0, "dev-agent2", "dev-agent2", None),
        ],
        [("ESurina", 10.0)],
    )
    # note: we don't use reopen in this minimal test; assert the single interval
    intervals, final, rec = run(REPO, issue, cfg)
    assert final == "handed_off_to_development"
    assert len(intervals) == 1
    itv = intervals[0]
    assert itv["triage_username"] == "ESurina"
    assert itv["start_type"] == "initial_routing"
    assert itv["ownership_end_reason"] == "handoff_to_non_triage"
    assert itv["issue_outcome"] == "handed_off_to_development"
    assert rec.issue_meta[f"{REPO}#{issue['number']}"]["post_dev_returns"] == 1
    # the verification return is NOT flagged as non_triage_to_triage
    assert all(d["category"] != "non_triage_to_triage" for d in rec.dq)

    # with the cut DISABLED the return opens a first_assignment interval again
    cfg["cut_triage_after_dev_handoff"] = False
    intervals2, _, rec2 = run(REPO, issue, cfg)
    assert len(intervals2) == 2
    assert intervals2[1]["start_type"] == "first_assignment"
    assert rec2.issue_meta[f"{REPO}#{issue['number']}"]["post_dev_returns"] == 0


def test_metrics_basic():
    cfg = base_config()
    issue1 = make_issue(
        REPO, 20, "c1", "2026-05-10T00:00:00Z", "r",
        [("assigned", 0.0, None, "maxikuzmin", None), ("closed", 3.0, "maxikuzmin", None, None)],
        [("maxikuzmin", 1.0)], state="closed", closed_at="2026-05-10T03:00:00Z",
    )
    # a dev handoff issue
    issue2 = make_issue(
        REPO, 21, "c2", "2026-05-20T00:00:00Z", "r",
        [
            ("assigned", 0.0, None, "maxikuzmin", None),
            ("unassigned", 6.0, None, "maxikuzmin", None),
            ("assigned", 6.0, "maxikuzmin", "dev-agent", None),
        ],
        [("maxikuzmin", 1.0)],
    )
    intervals, final1, rec1 = run(REPO, issue1, cfg)
    intervals2, final2, rec2 = run(REPO, issue2, cfg)
    all_intervals = intervals + intervals2
    active = [i for i in all_intervals if i["issue_outcome"] == "still_active"]
    meta = {"collected_at": COLLECTED}
    from analyze import build_issue_summary
    summary = [
        build_issue_summary(intervals, REPO, issue1, final1, cfg),
        build_issue_summary(intervals2, REPO, issue2, final2, cfg),
    ]
    m = compute_metrics(all_intervals, active, summary, meta)
    # issue1: resolved (completed via closure), issue2: handed to dev (completed)
    assert m["summary"]["completed_intervals"] == 2
    assert m["summary"]["resolved_by_triager"] == 1
    assert m["summary"]["handed_off_to_development"] == 1
    assert m["cycle_time"]["maxikuzmin"]["2026-05"]["median_h"] == pytest.approx(4.5)
    assert m["dev_handoff_time"]["maxikuzmin"]["2026-05"]["count"] == 1
    # issue-level time-to-dev from FIRST triage assignment (issue2: created 00:00, dev at 06:00)
    assert m["dev_handoff_from_first_assignment"]["count"] == 1
    assert m["dev_handoff_from_first_assignment"]["median_h"] == pytest.approx(6.0)
    # cycle time median over both completed non-spam values (3.0, 6.0) = 4.5
    assert m["cycle_time"]["maxikuzmin"]["2026-05"]["avg_h"] == pytest.approx(4.5)


# ---------------------------------------------------------------------------
# Monthly-run mechanism: last_period_window + snapshot-window precedence
# ---------------------------------------------------------------------------


def test_last_period_window_single_month():
    """months_back=1 -> the previous FULL calendar month, never the current one."""
    utc = timezone.utc
    # running mid/late month in August -> July 2026, half-open at month boundaries
    s, e = last_period_window(datetime(2026, 8, 27, 15, 30, tzinfo=utc), 1)
    assert s == datetime(2026, 7, 1, tzinfo=utc)
    assert e == datetime(2026, 8, 1, tzinfo=utc)
    # running on the 1st of August -> still July (the last *completed* month)
    s, e = last_period_window(datetime(2026, 8, 1, 0, 0, tzinfo=utc), 1)
    assert s == datetime(2026, 7, 1, tzinfo=utc)
    assert e == datetime(2026, 8, 1, tzinfo=utc)
    # no partial months: Aug 27 gives July, not a rolling 30 days
    s, e = last_period_window(datetime(2026, 8, 27, tzinfo=utc), 1)
    assert is_in_cohort(datetime(2026, 7, 31, 23, 59, 59, tzinfo=utc), s, e) is True
    assert is_in_cohort(datetime(2026, 8, 1, 0, 0, tzinfo=utc), s, e) is False


def test_last_period_window_year_rollover():
    utc = timezone.utc
    s, e = last_period_window(datetime(2026, 1, 15, tzinfo=utc), 1)
    assert s == datetime(2025, 12, 1, tzinfo=utc)
    assert e == datetime(2026, 1, 1, tzinfo=utc)
    s, e = last_period_window(datetime(2026, 3, 15, tzinfo=utc), 3)
    assert s == datetime(2025, 12, 1, tzinfo=utc)
    assert e == datetime(2026, 3, 1, tzinfo=utc)


def test_last_period_window_matches_existing_cohort():
    """months_back=6 run in August reproduces the README cohort Feb..Jul 2026."""
    utc = timezone.utc
    s, e = last_period_window(datetime(2026, 8, 27, tzinfo=utc), 6)
    assert s == datetime(2026, 2, 1, tzinfo=utc)
    assert e == datetime(2026, 8, 1, tzinfo=utc)


def test_last_period_window_rejects_invalid():
    utc = timezone.utc
    with pytest.raises(ValueError):
        last_period_window(datetime(2026, 8, 1, tzinfo=utc), 0)


def test_collect_effective_cohort_last_mode():
    """--cohort last (via config or CLI) computes the previous full months."""
    import collect

    cfg = base_config()
    cfg["cohort"] = {"start_date": "2026-02-01", "end_date": "2026-08-01", "mode": "last"}
    start, end, extra = collect._effective_cohort(cfg, as_of="2026-08-27")
    assert start.isoformat() == "2026-07-01T00:00:00+00:00"
    assert end.isoformat() == "2026-08-01T00:00:00+00:00"
    assert extra["cohort_mode"] == "last"
    assert extra["cohort_months_back"] == 1

    # CLI --months-back / --as-of override config values
    start, end, extra = collect._effective_cohort(cfg, months_back=6, as_of="2026-08-01")
    assert start.isoformat() == "2026-02-01T00:00:00+00:00"
    assert end.isoformat() == "2026-08-01T00:00:00+00:00"
    assert extra["cohort_months_back"] == 6


def test_collect_effective_cohort_config_defaults_to_explicit_dates():
    import collect

    cfg = base_config()  # cohort has no mode -> explicit config dates win
    start, end, extra = collect._effective_cohort(cfg)
    assert start.isoformat() == "2026-02-01T00:00:00+00:00"
    assert end.isoformat() == "2026-08-01T00:00:00+00:00"
    assert extra["cohort_mode"] == "config"

    # CLI --cohort last overrides a config-mode=config
    start, end, extra = collect._effective_cohort(cfg, mode="last", as_of="2026-08-27")
    assert start.isoformat() == "2026-07-01T00:00:00+00:00"
    assert extra["cohort_mode"] == "last"


def test_run_prefers_snapshot_window_over_config(tmp_path):
    """analyze.py must analyze exactly the window the collector recorded, even
    when config.yaml still holds an older default cohort (the monthly case)."""
    import json

    import analyze

    cfg = base_config()  # config cohort = Feb..Aug 2026
    make = lambda num, day, month: make_issue(
        REPO, num, f"issue-{num}", f"2026-{month:02d}-{day:02d}T09:00:00Z", "reporter",
        [("assigned", 0.0, None, "maxikuzmin", None), ("closed", 2.0, "maxikuzmin", None, None)],
        [("maxikuzmin", 0.5)], state="closed", closed_at=f"2026-{month:02d}-{day:02d}T11:00:00Z",
    )
    issues = [make(1, 15, 6), make(2, 15, 7), make(3, 15, 8)]  # June, July, August
    raw = {
        "meta": {
            "collected_at": COLLECTED,
            # the collector's window: only July (e.g. --cohort last run in August)
            "cohort_start": "2026-07-01T00:00:00+00:00",
            "cohort_end": "2026-08-01T00:00:00+00:00",
            "cohort_mode": "last",
        },
        "repos": {REPO: {"issues": issues}},
    }
    out = tmp_path / "out"
    out.mkdir()
    template = tmp_path / "template.html"
    template.write_text("<script>const DATA = /*__EMBEDDED_DATA__*/;</script>")
    metrics = analyze.run(cfg, raw, str(out), str(template), str(out / "dash.html"))

    iss = json.loads((out / "issue_summary.json").read_text())
    assert [i["issue_number"] for i in iss] == [2]  # only the July issue
    assert metrics["meta"]["cohort_source"] == "snapshot"
    assert metrics["summary"]["issues_in_cohort"] == 1

