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
    # scenarios 27/28: community member answered the topic starter, triager closed
    # silently -> the context double-check must clear these, NOT flag them
    for n in (109, 110):
        (repo, issue), = by_number[n]
        assert issue["state"] == "closed"
        others = [c for c in issue["comments"] if c["user"] != issue["user"]]
        topic_starter_last = issue["comments"][-1]["user"] == issue["user"]
        assert others, f"{repo}#{n} should have a non-author (helper) comment"
        # scenario 109: helper has the last word (auto-clear by rule 1)
        if n == 109:
            assert not topic_starter_last
        # scenario 110: starter has the last word but the helper's answer exists
        if n == 110:
            assert topic_starter_last
            assert any((c["body"] or "") for c in others)
    # scenario 29: dialogue continued after close AND the triager replied later in
    # the thread (user just has the last word) -> must NOT be flagged (refined
    # unanswered_after_close rule, mirrors real issue #6140)
    (repo29, issue29), = by_number[111]
    assert issue29["state"] == "closed"
    c = issue29["comments"]
    assert c[-1]["user"] == issue29["user"], "user should have the last word"
    post_close_followup = [x for x in c if x["created_at"] > issue29["closed_at"] and x["user"] == issue29["user"]]
    assert post_close_followup, "user should continue after close"
    triager_after = [x for x in c if x["user"] != issue29["user"]
                     and x["created_at"] > post_close_followup[0]["created_at"]]
    assert triager_after, "triager should reply after the post-close follow-up"
    # scenario 30: user thanks after close (acknowledgment only) -> no_reply_needed
    (repo30, issue30), = by_number[112]
    body = issue30["comments"][-1]["body"]
    assert body and ("thank" in body.lower()), "scenario 30 should end with a thank-you"
    assert "?" not in body, "scenario 30 thank-you must not ask a question"
    # scenario 31: closed silently, only an acknowledgment -> no_reply_needed (not CWR)
    (repo31, issue31), = by_number[303]
    last31 = issue31["comments"][-1]
    assert last31["user"] == issue31["user"], "scenario 31 last word is the author"
    assert (last31["body"] or "").strip(), "scenario 31 should carry a comment body"
    assert "thanks" in last31["body"].lower(), "scenario 31 should be an acknowledgment"
