#!/usr/bin/env python3
"""
make_mock_data.py — generate a deterministic MOCK raw snapshot.

Produces `mock/raw_snapshot.json` in exactly the same schema as collect.py, but
with synthetic data so the pipeline and dashboard can be exercised offline with
no GITHUB_TOKEN. It deliberately exercises every classification scenario and
the cohort boundaries:

  - default assignee keeps + hands to development
  - initial routing (default did nothing)
  - real triage handoff (default demonstrably worked it)
  - long triage handoff chain
  - resolved directly by triager
  - unassigned interval end
  - two intervals for the same triager (leave and return)
  - active (still-owned) issues for the aging report
  - unassigned-at-creation -> first_assignment
  - non-triage assignee at creation -> first_assignment (+DQ flag)
  - created just before / after the cohort window (excluded)
  - created in cohort, completed after cohort end (late completion)

Usage:  python make_mock_data.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

REPOS = [
    "AdguardTeam/AdguardForAndroid",
    "AdguardTeam/AdGuardVPNForAndroid",
    "AdguardTeam/AdguardForiOS",
    "AdguardTeam/AdGuardVPNForiOS",
    "TrustTunnel/TrustTunnelFlutterClient",
    "AdguardTeam/ContentBlocker",
    "AdguardTeam/AdguardForWindows",
]
DEFAULTS = {
    "AdguardTeam/AdguardForAndroid": "maxikuzmin",
    "AdguardTeam/AdGuardVPNForAndroid": "maxikuzmin",
    "AdguardTeam/AdguardForiOS": "maxikuzmin",
    "AdguardTeam/AdGuardVPNForiOS": "maxikuzmin",
    "TrustTunnel/TrustTunnelFlutterClient": "oksenina",
    "AdguardTeam/ContentBlocker": "maxikuzmin",
    "AdguardTeam/AdguardForWindows": "AlexandrPkhm",
}
TEAM = [
    "Swen90", "maxikuzmin", "ESurina", "oksenina",
    "AlexandrPkhm", "IaroslavKhvorostenko", "dmitriivqa39", "pakifev", "KolbasovAnton",
]
DEVS = ["dev-agent", "adguard-dev", "flutter-dev", "qa-bot"]

COLLECTED_AT = "2026-08-02T00:00:00Z"


def t(base: datetime, hours: float) -> str:
    return (base + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def iso_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def make_issue(
    repo: str,
    number: int,
    title: str,
    created: str,
    opener: str,
    events: List[Tuple[str, float, str, Optional[str], Optional[str]]],
    comments: List[Tuple[str, float, Optional[str]]],
    state: str = "open",
    closed_at: Optional[str] = None,
    opener_is_triager: bool = False,
) -> Dict[str, Any]:
    """Build one issue dict matching collect.py's normalized schema.

    events: list of (event_type, offset_hours, actor, target, label)
      event_type in {assigned, unassigned, closed, reopened, labeled, unlabeled}
    comments: list of (user, offset_hours) or (user, offset_hours, body).
      A 2-tuple is treated as body=None (backwards compatible with tests);
      a 3-tuple carries the comment body text, which the triage-quality
      workflow uses to quote unanswered follow-ups on closed issues.
    """
    idx = 1
    ev_list: List[Dict[str, Any]] = []
    assignee: Optional[str] = None
    for kind, off, actor, target, label in events:
        if kind == "assigned" and target:
            assignee = target
        elif kind == "unassigned" and target and target == assignee:
            assignee = None
        ev = {
            "id": idx,
            "created_at": t(iso_dt(created), off),
            "event": kind,
            "actor": actor,
            "assignee": target if kind in ("assigned", "unassigned") else None,
            "label": label if kind in ("labeled", "unlabeled") else None,
        }
        idx += 1
        ev_list.append(ev)
    ev_list.sort(key=lambda x: (x["created_at"], x["id"]))
    com_list = []
    for n, cmt in enumerate(comments, 1):
        u, off = cmt[0], cmt[1]
        body = cmt[2] if len(cmt) > 2 else None
        com_list.append(
            {
                "id": n,
                "created_at": t(iso_dt(created), off),
                "user": u,
                "body": body,
            }
        )
    com_list.sort(key=lambda x: x["created_at"])
    short = repo.split("/")[1]
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "state": state,
        "state_reason": None,
        "created_at": created,
        "closed_at": closed_at,
        "closed_by": None,
        "user": opener,
        "labels": [],
        "assignee": assignee,
        "assignees": [assignee] if assignee else [],
        "events": ev_list,
        "comments": com_list,
        "events_collected_at": COLLECTED_AT,
    }


def build() -> Dict[str, Any]:
    issues: Dict[str, List[Dict[str, Any]]] = {r: [] for r in REPOS}
    o1, o2, o3 = "reporter-alpha", "reporter-beta", "reporter-gamma"

    # Scenario 1: default keeps the issue, then hands to development.
    #   created -> maxikuzmin(default) [comments at +2h] -> dev at +6h
    issues[REPOS[0]].append(make_issue(
        REPOS[0], 101, "Crash on startup after update to 5.2",
        "2026-05-03T09:00:00Z", o1,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[0]], None),
            ("assigned", 6.0, "maxikuzmin", DEVS[1], None),
            ("unassigned", 6.0, None, DEFAULTS[REPOS[0]], None),
        ],
        [("maxikuzmin", 2.0, "Thanks, I can reproduce it. Handing over to the team.")], state="open",
    ))

    # Scenario 2: initial routing (default did nothing) -> ESurina -> dev
    issues[REPOS[0]].append(make_issue(
        REPOS[0], 102, "Add option to disable telemetry per-app",
        "2026-05-11T10:00:00Z", o2,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[0]], None),
            ("unassigned", 0.5, None, DEFAULTS[REPOS[0]], None),
            ("assigned", 0.5, "ESurina", "ESurina", None),
            ("assigned", 6.0, "ESurina", DEVS[0], None),
            ("unassigned", 6.0, None, "ESurina", None),
        ],
        [("ESurina", 1.0, "Could you describe the exact flow that triggers it?")], state="open",
    ))

    # Scenario 3: chain default -> Swen90 (routing) -> ESurina (handoff) -> dev
    issues[REPOS[1]].append(make_issue(
        REPOS[1], 201, "VPN connection drops on Android 15",
        "2026-05-21T08:00:00Z", o1,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[1]], None),
            ("unassigned", 1.0, None, DEFAULTS[REPOS[1]], None),
            ("assigned", 1.0, "Swen90", "Swen90", None),
            ("unassigned", 30.0, "Swen90", "Swen90", None),
            ("assigned", 30.0, "Swen90", "ESurina", None),
            ("assigned", 60.0, "ESurina", DEVS[0], None),
            ("unassigned", 60.0, None, "ESurina", None),
        ],
        [
            ("Swen90", 6.0, "Asking the reporter for a logcat capture."),
            ("ESurina", 35.0, "Reproduces on Android 15, low priority."),
        ], state="open",
    ))

    # Scenario 4: resolved directly by triager (default, with activity)
    issues[REPOS[1]].append(make_issue(
        REPOS[1], 202, "Question: does the iOS VPN block ads too?",
        "2026-05-28T12:00:00Z", "reporter-delta",
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[1]], None),
            ("closed", 4.0, "maxikuzmin", None, None),
        ],
        [("maxikuzmin", 1.0, "Yes — disable HTTPS filtering in the VPN settings.")], state="closed", closed_at="2026-05-28T16:00:00Z",
    ))

    # Scenario 5: unassigned end (default -> nobody)
    issues[REPOS[2]].append(make_issue(
        REPOS[2], 301, "Filter logs do not rotate correctly",
        "2026-06-02T09:00:00Z", o2,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[2]], None),
            ("unassigned", 12.0, None, DEFAULTS[REPOS[2]], None),
        ],
        [("maxikuzmin", 1.0)], state="open",
    ))

    # Scenario 6: two intervals for the same triager (default + return)
    issues[REPOS[2]].append(make_issue(
        REPOS[2], 302, "Ad blocking misses some XHR requests",
        "2026-06-10T07:00:00Z", o1,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[2]], None),
            ("assigned", 5.0, "maxikuzmin", DEVS[0], None),
            ("unassigned", 5.0, None, "maxikuzmin", None),
            ("assigned", 30.0, "maxikuzmin", "maxikuzmin", None),
            ("assigned", 48.0, "maxikuzmin", DEVS[2], None),
            ("unassigned", 48.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 2.0), ("maxikuzmin", 32.0)], state="open",
    ))

    # Scenario 7: active issue (still owned, aging)
    issues[REPOS[3]].append(make_issue(
        REPOS[3], 401, "App crashes when enabling Kill Switch",
        "2026-06-20T15:00:00Z", o3,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[3]], None),
        ],
        [("maxikuzmin", 1.5)], state="open",
    ))

    # Scenario 8: active issue, owned by Swen90 via routing, now aging >72h
    issues[REPOS[3]].append(make_issue(
        REPOS[3], 402, "Localization strings missing for nl-NL",
        "2026-07-05T11:00:00Z", o2,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[3]], None),
            ("unassigned", 0.1, None, DEFAULTS[REPOS[3]], None),
            ("assigned", 0.1, "Swen90", "Swen90", None),
        ],
        [], state="open",
    ))

    # Scenario 9: unassigned-at-creation -> first_assignment (oksenina on TrustTunnel)
    issues[REPOS[4]].append(make_issue(
        REPOS[4], 501, "Flutter client fails to build on Windows",
        "2026-07-12T09:00:00Z", "reporter-eps",
        [
            ("assigned", 2.0, "oksenina", "oksenina", None),
            ("assigned", 10.0, "oksenina", DEVS[3], None),
            ("unassigned", 10.0, None, "oksenina", None),
        ],
        [("oksenina", 3.0)], state="open",
    ))

    # Scenario 10: non-triage assignee at creation -> later triaged (DQ flag)
    issues[REPOS[4]].append(make_issue(
        REPOS[4], 502, "Push notifications not delivered in beta",
        "2026-07-18T14:00:00Z", o1,
        [
            ("assigned", 0.0, None, DEVS[3], None),
            ("assigned", 6.0, "oksenina", "oksenina", None),
            ("unassigned", 6.0, None, DEVS[3], None),
        ],
        [("oksenina", 7.0)], state="open",
    ))

    # Scenario 11: late completion (created in cohort, completed after end)
    issues[REPOS[0]].append(make_issue(
        REPOS[0], 103, "Battery drain while using VPN always-on",
        "2026-07-15T08:30:00Z", o3,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[0]], None),
            ("assigned", 400.0, "maxikuzmin", DEVS[1], None),  # ~Aug 1 in real time
            ("unassigned", 400.0, None, "maxikuzmin", None),
            ("closed", 400.5, "dev-agent", None, None),
        ],
        [("maxikuzmin", 3.0)], state="closed", closed_at="2026-08-01T01:00:00Z",
    ))

    # Scenario 12: initial routing that never resolves (active, <24h)
    issues[REPOS[1]].append(make_issue(
        REPOS[1], 203, "Icon rendering issue on foldable devices",
        "2026-08-01T22:00:00Z", o2,   # NOTE: created after cohort end -> excluded
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[1]], None),
        ],
        [("maxikuzmin", 0.5)], state="open",
    ))

    # Scenario 13: created before cohort -> excluded
    issues[REPOS[2]].append(make_issue(
        REPOS[2], 298, "Old issue from April",
        "2026-04-28T10:00:00Z", o1,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[2]], None),
            ("assigned", 5.0, "maxikuzmin", DEVS[0], None),
        ],
        [], state="open",
    ))

    # Scenario 14: active, exactly in the 24-48h bucket at collection time
    issues[REPOS[3]].append(make_issue(
        REPOS[3], 403, "DNS-over-HTTPS toggle no longer persists",
        "2026-07-31T12:00:00Z", o1,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[3]], None),
        ],
        [("maxikuzmin", 0.25)], state="open",
    ))

    # Scenario 15: spam-looking but quick closure with NO label -> resolved (valid)
    issues[REPOS[0]].append(make_issue(
        REPOS[0], 104, "How do I update the app?",
        "2026-06-19T13:00:00Z", "user-newb",
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[0]], None),
            ("closed", 1.0, "maxikuzmin", None, None),
            ("unassigned", 1.0, None, DEFAULTS[REPOS[0]], None),
        ],
        [("maxikuzmin", 0.5)], state="closed", closed_at="2026-06-19T14:00:00Z",
    ))

    # Scenario 16: closed then reopened (interval restarted after closure)
    issues[REPOS[4]].append(make_issue(
        REPOS[4], 503, "Token refresh throws 401 intermittently",
        "2026-07-08T07:00:00Z", o3,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[4]], None),
            ("closed", 20.0, "oksenina", None, None),
            ("unassigned", 20.0, None, "oksenina", None),
            ("reopened", 50.0, "reporter-beta", None, None),
            ("assigned", 50.0, "oksenina", "oksenina", None),
        ],
        [("oksenina", 2.0), ("oksenina", 22.0), ("oksenina", 52.0)],
        state="open", closed_at="2026-07-09T03:00:00Z",
    ))

    # Scenario 17: bot-created issue in the cohort window -> excluded from the
    # cohort entirely (kept in raw for audit, but not counted/analyzed) because
    # config.yaml lists adguard-octobuddy[bot] under exclude_creators.
    issues[REPOS[1]].append(make_issue(
        REPOS[1], 204, "Automated dependency notice (bot-created)",
        "2026-06-25T08:00:00Z", "adguard-octobuddy[bot]",
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[1]], None),
            ("closed", 4.0, "adguard-bot", None, None),
            ("unassigned", 4.0, None, DEFAULTS[REPOS[1]], None),
        ],
        [], state="closed", closed_at="2026-06-25T12:00:00Z",
    ))

    # Scenario 18: early-cohort issue (February bucket)
    issues[REPOS[0]].append(make_issue(
        REPOS[0], 105, "February crash on cold start",
        "2026-02-12T09:30:00Z", o1,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[0]], None),
            ("assigned", 20.0, "maxikuzmin", DEVS[1], None),
            ("unassigned", 20.0, None, "maxikuzmin", None),
        ],
        [("maxikuzmin", 2.0)], state="open",
    ))

    # Scenario 19: ContentBlocker — maxikuzmin routes to pakifev (initial routing)
    issues[REPOS[5]].append(make_issue(
        REPOS[5], 91, "Content blocker stops filtering after app update",
        "2026-03-05T11:00:00Z", o2,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[5]], None),
            ("unassigned", 1.0, None, "maxikuzmin", None),
            ("assigned", 1.0, "maxikuzmin", "pakifev", None),
            ("unassigned", 20.0, None, "pakifev", None),
            ("assigned", 20.0, "pakifev", "dev-agent", None),
        ],
        [("pakifev", 3.0)], state="open",
    ))

    # Scenario 20: AdguardForWindows with AlexandrPkhm as default -> resolved
    issues[REPOS[6]].append(make_issue(
        REPOS[6], 31, "Window position not restored after reboot",
        "2026-04-22T13:00:00Z", o3,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[6]], None),
            ("closed", 5.0, "AlexandrPkhm", None, None),
        ],
        [("AlexandrPkhm", 1.0)], state="closed", closed_at="2026-04-22T18:00:00Z",
    ))

    # Scenario 21: spam-labeled issue -> excluded entirely (exclude_labels: [spam])
    issues[REPOS[0]].append(make_issue(
        REPOS[0], 107, "Buy followers fast!!! (spam)",
        "2026-05-09T01:00:00Z", "spam-account",
        [
            ("labeled", 0.1, "adguard-bot", None, "spam"),
            ("closed", 1.0, "adguard-bot", None, None),
        ],
        [], state="closed", closed_at="2026-05-09T02:00:00Z",
    ))

    # Scenario 22: created by a triage team member -> excluded entirely
    # (exclude_team_created_issues: assumed already triaged -> passed to devs)
    issues[REPOS[5]].append(make_issue(
        REPOS[5], 92, "Internal task: bump DNS library (team-created)",
        "2026-04-10T09:00:00Z", "ESurina",
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[5]], None),
            ("unassigned", 1.0, None, DEFAULTS[REPOS[5]], None),
            ("assigned", 1.0, "ESurina", "dev-agent", None),
        ],
        [("ESurina", 0.5)], state="open",
    ))

    # Scenario 23: NON-ENGLISH TITLE — the triage-quality workflow should flag
    # this as a title the triager should have renamed to English during triage.
    # Russian/Cyrillic title, default keeps it and resolves it.
    issues[REPOS[1]].append(make_issue(
        REPOS[1], 205, "Вирус блокирует установку AdGuard",
        "2026-06-08T14:00:00Z", o2,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[1]], None),
            ("closed", 4.0, "maxikuzmin", None, None),
            ("unassigned", 4.0, None, DEFAULTS[REPOS[1]], None),
        ],
        [("maxikuzmin", 1.0, "Please tell me which site shows the warning.")],
        state="closed", closed_at="2026-06-08T18:00:00Z",
    ))

    # Scenario 24: CLOSED issue, then the user CONTINUES THE DIALOGUE after
    # closure and the triager never replies -> unanswered_after_close.
    issues[REPOS[6]].append(make_issue(
        REPOS[6], 32, "Settings window opens on the wrong monitor",
        "2026-05-15T10:00:00Z", o1,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[6]], None),
            ("closed", 5.0, "AlexandrPkhm", None, None),
            ("unassigned", 5.0, None, DEFAULTS[REPOS[6]], None),
        ],
        [
            ("AlexandrPkhm", 1.0, "Thanks for the report, fixed in the next beta."),
            (o1, 6.0, "The fix did not help, the window still opens on monitor 2!"),
        ],
        state="closed", closed_at="2026-05-15T15:00:00Z",
    ))

    # Scenario 25: CLOSED issue, user continues after closure BUT the triager
    # DID reply afterwards -> must NOT be flagged.
    issues[REPOS[0]].append(make_issue(
        REPOS[0], 108, "Battery drain after the latest update",
        "2026-06-11T09:00:00Z", o3,
        [
            ("assigned", 0.0, None, DEFAULTS[REPOS[0]], None),
            ("closed", 8.0, "maxikuzmin", None, None),
            ("unassigned", 8.0, None, DEFAULTS[REPOS[0]], None),
        ],
        [
            ("maxikuzmin", 1.0, "Could you attach a battery usage report?"),
            (o3, 2.0, "Attached, it drops about 20% overnight."),
            ("maxikuzmin", 3.0, "Looks like a known issue, forwarded to devs."),
            (o3, 9.0, "Any news on the battery problem?"),
            ("maxikuzmin", 10.0, "It will be fixed in the next release."),
        ],
        state="closed", closed_at="2026-06-11T17:00:00Z",
    ))

    # Scenario 26: CLOSED issue where the triager closed a user question
    # without EVER replying -> closed_without_reply (client continued dialogue
    # in a closed issue, triager never answered).
    issues[REPOS[4]].append(make_issue(
        REPOS[4], 504, "Notifications disappear after a few hours",
        "2026-06-22T13:00:00Z", o2,
        [
            ("assigned", 1.0, "oksenina", "oksenina", None),
            ("closed", 20.0, "oksenina", None, None),
            ("unassigned", 20.0, None, "oksenina", None),
        ],
        [
            (o2, 2.0, "Everything was fine before the last update, please help."),
        ],
        state="closed", closed_at="2026-06-23T09:00:00Z",
    ))

    snapshot = {
        "meta": {
            "collected_at": COLLECTED_AT,
            "source": "mock",
            "cohort_start": "2026-02-01T00:00:00+00:00",
            "cohort_end": "2026-08-01T00:00:00+00:00",
            "window_half_open": True,
        },
        "repos": {r: {"issues": sorted(issues[r], key=lambda x: x["number"])} for r in REPOS},
    }
    return snapshot


def main() -> int:
    os.makedirs("mock", exist_ok=True)
    path = os.path.join("mock", "raw_snapshot.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(build(), fh, indent=2, sort_keys=True)
    n = sum(len(r["issues"]) for r in build()["repos"].values())
    print(f"[mock] wrote {path} with {n} issues (deterministic).")
    print("       run:  python analyze.py --raw mock/raw_snapshot.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
