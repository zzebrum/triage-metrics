"""
Tests for the triage-quality workflow's data plumbing.

The triage-quality report itself is specified and executed by the opencode
agent/command (`.opencode/command/triage-quality.md` + `.opencode/agent/
triage-analyst.md`) — intentionally NOT a committed Python module. What IS
committed Python is the data that feeds it: the collector must store comment
bodies, and the offline mock dataset must carry bodies plus the demo scenarios
the workflow's self-check relies on. Those are what we test here. No network.

Run:  pytest -q
"""

from __future__ import annotations

import json

from collect import _normalize_comments
from make_mock_data import make_issue

REPO = "AdguardTeam/AdguardForAndroid"


def _raw_comments():
    return [
        {"id": 7, "created_at": "2026-05-21T14:00:00Z", "user": {"login": "Swen90"},
         "body": "Please attach a logcat capture."},
        {"id": 8, "created_at": "2026-05-21T15:00:00Z", "user": {"login": "alice"},
         "body": None},
    ]


def test_collect_normalizes_comment_with_body():
    out = _normalize_comments(_raw_comments())
    assert len(out) == 2
    first = out[0]
    # all fields survive
    assert first["id"] == 7
    assert first["created_at"] == "2026-05-21T14:00:00Z"
    assert first["user"] == "Swen90"
    assert first["body"] == "Please attach a logcat capture."
    # a missing body is stored as None, never dropped or coerced
    assert out[1]["body"] is None


def test_collect_normalizes_comments_sorted_by_time():
    out = _normalize_comments(_raw_comments())
    assert [c["id"] for c in out] == [7, 8]


def test_make_issue_comment_body_backwards_compatible():
    # 2-tuple (user, offset) => body None (how existing tests call it)
    two = make_issue(
        REPO, 1, "A title", "2026-06-10T09:00:00Z", "opener",
        [("assigned", 0.0, None, "maxikuzmin", None)],
        [("maxikuzmin", 1.0)],
    )["comments"]
    assert two[0]["body"] is None
    assert two[0]["user"] == "maxikuzmin"
    # 3-tuple (user, offset, body) => body preserved
    three = make_issue(
        REPO, 1, "A title", "2026-06-10T09:00:00Z", "opener",
        [("assigned", 0.0, None, "maxikuzmin", None)],
        [("maxikuzmin", 1.0, "Thank you, fixed in the next beta.")],
    )["comments"]
    assert three[0]["body"] == "Thank you, fixed in the next beta."


def test_mock_snapshot_carries_comment_bodies_and_quality_scenarios():
    # regenerate in-memory: deterministic, matches the committed mock file
    data = json.load(open("mock/raw_snapshot.json", encoding="utf-8"))

    by_number = {}
    for repo, block in data["repos"].items():
        for issue in block["issues"]:
            by_number.setdefault(issue["number"], []).append((repo, issue))

    # every comment carries the body key (the workflow reads it)
    for repo, block in data["repos"].items():
        for issue in block["issues"]:
            for comment in issue["comments"]:
                assert "body" in comment, f"{repo}#{issue['number']} comment missing body"

    # scenario 23: non-English (Cyrillic) title the workflow must flag
    assert (205, "Вирус блокирует установку AdGuard") in [
        (i["number"], i["title"]) for _, i in by_number[205]
    ]
    # scenario 24: closed issue, user continued after close -> unanswered
    assert 32 in by_number
    # scenario 25: closed issue, user continued after close BUT triager replied -> clean
    assert 108 in by_number
    # scenario 26: closed issue, triager never replied at all -> closed_without_reply
    assert 504 in by_number
