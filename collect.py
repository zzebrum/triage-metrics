#!/usr/bin/env python3
"""
collect.py — GitHub Triage Metrics: raw data collector (READ-ONLY).

Fetches, for every configured repository, the issues created inside the cohort
window together with their full event history (assignee changes, labels,
closure) and their comments. Output is a normalized "raw snapshot" JSON that
analyze.py consumes.

IMPORTANT
---------
This tool ONLY issues GET requests against the GitHub REST API. It never
creates, edits, closes, assigns, comments on, or otherwise mutates anything.

Auth
----
Set the environment variable GITHUB_TOKEN to a read-only personal access token
(fine for public repos too — it raises the API rate limit from 60 to 5000
requests/hour). If GITHUB_TOKEN is absent the collector aborts with
instructions, because unauthenticated rate limits (60/h) are too low for full
issue+event+comment history; use `python make_mock_data.py` to demo offline.

Usage
-----
    export GITHUB_TOKEN=ghp_...            # keep secrets out of git
    python collect.py [--config config.yaml] [--out-dir data/raw]
    python collect.py --cohort last --months-back 1          # once a month: previous full month
    python collect.py --cohort last --as-of 2026-08-01       # deterministic / backfill reference date
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: pip install -r requirements.txt")

# Reuse the pure window math from analyze.py (no network, no side effects).
from analyze import last_period_window


# ---------------------------------------------------------------------------
# Rate-limit handling
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    pass


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value) -> datetime:
    """Parse config timestamps. YAML may hand us a date/string; treat as UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sleep_backoff(base: float, attempt: int) -> None:
    # exponential backoff, capped; never hammers the API
    time.sleep(min(base * (2 ** attempt), 30))


def _throttle(base: Dict[str, Any]) -> None:
    """Small inter-request delay (GitHub's secondary rate-limit / abuse
    detection fires on BURSTS of fast consecutive calls — the collector used to
    sleep only between repos, not between pages/issues, which is exactly what
    tripped it from fast CI runners). Configurable via api.request_delay_seconds
    (default 0.2s); 0 disables."""
    delay = float(base.get("request_delay_seconds", 0.2))
    if delay > 0:
        time.sleep(delay)


def _wait_for_limit(resp: requests.Response, request_timeout: int) -> float:
    """How many seconds to sleep for a 403 before retrying.

    GitHub returns 403 for two very different cases:

      * PRIMARY rate limit — X-RateLimit-Remaining=0. The token is genuinely
        exhausted; wait until X-RateLimit-Reset (this is the only case where a
        long sleep is warranted).

      * SECONDARY limit / abuse detection — returned against bursty clients
        (especially GitHub-hosted runners, whose shared egress IPs get
        throttled by api.github.com). X-RateLimit-Remaining is still high and a
        `Retry-After` header says how long to wait. Sleeping until
        X-RateLimit-Reset here is wrong (it can be ~an hour away) and would
        stall the whole run. Always use a short, bounded wait when the token is
        not actually exhausted.
    """
    remaining = resp.headers.get("X-RateLimit-Remaining", "?")
    if str(remaining) == "0":
        reset_ts = int(resp.headers.get("X-RateLimit-Reset", "0"))
        if reset_ts:
            return max(reset_ts - int(time.time()), 0) + 1
        return 60  # exhausted with no reset header — conservative
    # secondary/abuse: short bounded waits so the run always keeps moving
    retry_after = resp.headers.get("Retry-After")
    if retry_after is not None:
        try:
            wait = float(retry_after)
        except (TypeError, ValueError):
            wait = 10
        return min(max(wait + 1, 1), 60)
    return 10


def api_get(session: requests.Session, url: str, base, **kwargs) -> Dict[str, Any]:
    """GET one page; honors rate limits with retries. Read-only."""
    attempts = 0
    while True:
        attempts += 1
        resp = session.get(url, timeout=base["request_timeout_seconds"], **kwargs)
        if resp.status_code == 200:
            _throttle(base)
            return resp.json()
        if resp.status_code == 403:
            # primary rate limit OR secondary/abuse detection OR token-permission
            remaining = resp.headers.get("X-RateLimit-Remaining", "?")
            wait = _wait_for_limit(resp, base["request_timeout_seconds"])
            # Surface GitHub's reason so permission/scope 403s are diagnosable
            # (a quota 403 would show remaining=0; a scope 403 keeps remaining
            # high and needs a different token, not a sleep).
            reason = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    reason = str(body.get("message", ""))[:160]
            except Exception:
                pass
            # Only consume the retry budget for rate-limit, not auth failure.
            if attempts > base["max_retries"]:
                raise ApiError(
                    f"rate limited / forbidden after {attempts} tries "
                    f"(remaining={remaining}){('; ' + reason) if reason else ''} — abort"
                )
            print(f"   [rate-limit] remaining={remaining}; retry in {wait}s{('; ' + reason) if reason else ''}",
                  file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            if attempts > base["max_retries"]:
                raise ApiError(f"GitHub error {resp.status_code} on {url}")
            _sleep_backoff(base["backoff_seconds"], attempts)
            continue
        if resp.status_code in (404, 410):
            raise ApiError(f"Not found (404/410): {url}")
        raise ApiError(f"Unexpected status {resp.status_code} for {url}")
    # unreachable


def get_paginated(session: requests.Session, url: str, base: Dict[str, Any]) -> List[Dict[str, Any]]:
    """GET a paginated collection, following `per_page` pages. Read-only."""
    items: List[Dict[str, Any]] = []
    page = 1
    n = base["per_page"]
    while True:
        sep = "&" if "?" in url else "?"
        page_url = f"{url}{sep}per_page={n}&page={page}"
        data = api_get(session, page_url, base)
        if not isinstance(data, list):
            raise ApiError(f"expected a JSON array from {page_url}, got {type(data)}")
        if not data:
            break
        items.extend(data)
        if len(data) < n:
            break
        page += 1
    return items


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _login(obj: Optional[Dict[str, Any]]) -> Optional[str]:
    if not obj:
        return None
    v = obj.get("login") or obj.get("name")
    return v if isinstance(v, str) and v else None


def _normalize_events(raw_events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only events relevant to triage reconstruction, deduplicated by id."""
    relevant = {"assigned", "unassigned", "labeled", "unlabeled", "closed", "reopened"}
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for e in raw_events:
        if e.get("event") not in relevant:
            continue
        eid = e.get("id")
        if eid is not None and eid in seen:
            continue
        if eid is not None:
            seen.add(eid)
        out.append(
            {
                "id": e.get("id"),
                "created_at": e.get("created_at"),
                "event": e.get("event"),
                "actor": _login(e.get("actor")) or _login(e.get("actor")),
                "assignee": _login(e.get("assignee")) or _login(e.get("assignees")),
                "label": (e.get("label") or {}).get("name"),
                "commit_id": e.get("commit_id"),
            }
        )
    out.sort(key=lambda x: (x["created_at"] or "", x["id"] or 0))
    return out


def _normalize_comments(raw_comments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize an issue's comments, including their BODY text.

    The body is what lets the triage-quality workflow (`.opencode/command/
    triage-quality.md`) tell whether a follow-up on a closed issue is a real
    unanswered question (and quote it in the report) rather than a bot ping or
    a bare acknowledgement. Older snapshots collected without `body` stay
    readable: the field is simply absent/None there.
    """
    out = []
    for c in raw_comments:
        body = c.get("body")
        out.append(
            {
                "id": c.get("id"),
                "created_at": c.get("created_at"),
                "user": _login(c.get("user")),
                "body": body if isinstance(body, str) else None,
            }
        )
    out.sort(key=lambda x: (x["created_at"] or "", x["id"] or 0))
    return out


def _normalize_issue(i: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "number": i.get("number"),
        "title": i.get("title"),
        "html_url": i.get("html_url"),
        "state": i.get("state"),
        "state_reason": i.get("state_reason"),
        "created_at": i.get("created_at"),
        "closed_at": i.get("closed_at"),
        "closed_by": _login(i.get("closed_by")),
        "user": _login(i.get("user")),
        "labels": [l.get("name") for l in (i.get("labels") or []) if isinstance(l, dict)],
        # primary assignee + full assignee list (multi-assignee is flagged later)
        "assignee": _login(i.get("assignee")),
        "assignees": [_login(a) for a in (i.get("assignees") or []) if _login(a)],
    }


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _effective_cohort(
    config: Dict[str, Any],
    mode: Optional[str] = None,
    months_back: Optional[int] = None,
    as_of: Optional[str] = None,
    since: Optional[str] = None,
) -> Tuple[datetime, datetime, Dict[str, Any]]:
    """Resolve the cohort window actually collected, plus audit metadata.

    Returns (start, end, cohort_meta_extra).

    mode ∈ {"config", "last", "since"} — an explicit CLI value wins over
    `cohort.mode` in config.yaml (default "config" = explicit start/end dates).

    mode == "last" is the plain once-a-month mechanism: the window is the
    previous `months_back` FULL calendar months (month boundaries, half-open),
    computed relative to `as_of` (an optional YYYY-MM-DD reference; defaults to
    now). Use it for quick/small collection (e.g. the 3-repo smoke config).

    mode == "since" makes the dataset CUMULATIVE: start stays fixed at the
    anchor date (CLI `--since`, else `cohort.since_date` in config) and the
    end advances to the first day of the month containing `as_of` (default
    now), so each monthly run ADDS the new period to the previous months
    instead of replacing them. This is the default monthly mode: the dashboard
    keeps the full history and only the end boundary moves forward.

    `include_aug_1` (config) is honored uniformly: when true the end boundary is
    extended to the last second of the boundary day.
    """
    cohort = config.get("cohort") or {}
    if mode is None:
        mode = cohort.get("mode", "config")
    if mode == "last":
        if months_back is None:
            months_back = int(cohort.get("months_back", 1))
        reference = parse_utc(as_of) if as_of else datetime.now(timezone.utc)
        start, end = last_period_window(reference, int(months_back))
        extra: Dict[str, Any] = {
            "cohort_mode": "last",
            "cohort_months_back": int(months_back),
            "cohort_as_of_ref": reference.isoformat(timespec="seconds"),
        }
    elif mode == "since":
        anchor = parse_utc(since) if since else parse_utc(cohort.get("since_date"))
        reference = parse_utc(as_of) if as_of else datetime.now(timezone.utc)
        end = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = anchor
        extra: Dict[str, Any] = {
            "cohort_mode": "since",
            "cohort_since": anchor.isoformat(timespec="seconds"),
            "cohort_end_ref": reference.isoformat(timespec="seconds"),
        }
    else:
        start = parse_utc(cohort["start_date"])
        end = parse_utc(cohort["end_date"])
        extra = {"cohort_mode": "config"}
    if cohort.get("include_aug_1"):
        end = end.replace(hour=23, minute=59, second=59)
    return start, end, extra


def collect(
    config: Dict[str, Any],
    out_dir: str,
    session: requests.Session,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    cohort_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    api = config.get("api", {})
    if start is None or end is None:
        # direct/legacy call without an explicit window: derive from config
        start, end, cohort_meta = _effective_cohort(config)
    cohort_meta = cohort_meta or {}

    base_url = api.get("base_url", "https://api.github.com").rstrip("/")
    since = start.isoformat(timespec="seconds").replace("+00:00", "Z")

    snapshot: Dict[str, Any] = {
        "meta": {
            "collected_at": _utcnow_iso(),
            "source": "github",
            "cohort_start": start.isoformat(timespec="seconds"),
            "cohort_end": end.isoformat(timespec="seconds"),
            "window_half_open": True,
            **cohort_meta,
        },
        "repos": {},
    }

    for repo in config["repositories"]:
        # owner/repo slug is normalized to lowercase for lookups but the
        # canonical case used for URLs is preserved from configuration.
        print(f"[collect] {repo} ...", file=sys.stderr)
        owner, name = repo.split("/", 1)
        issues_url = f"{base_url}/repos/{owner}/{name}/issues"
        raw_issues = get_paginated(
            session, f"{issues_url}?state=all&since={since}", api
        )
        repo_block: Dict[str, Any] = {"issues": []}
        for i in raw_issues:
            created_raw = i.get("created_at")
            if not created_raw:
                continue
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if not (start <= created < end):
                continue
            number = i.get("number")
            # events (assignee / label / close history)
            events = get_paginated(session, f"{issues_url}/{number}/events", api)
            # comments (used only for "did the triager take observable action?")
            comments = get_paginated(session, f"{issues_url}/{number}/comments", api)
            issue = _normalize_issue(i)
            issue["events"] = _normalize_events(events)
            issue["comments"] = _normalize_comments(comments)
            issue["events_collected_at"] = _utcnow_iso()
            repo_block["issues"].append(issue)
            if issue["assignees"] and len(issue["assignees"]) > 1:
                # multi-assignee: analyzer will flag; keep note here
                pass
        # stable ordering by number
        repo_block["issues"].sort(key=lambda x: x["number"] or 0)
        snapshot["repos"][repo] = repo_block
        _sleep_backoff(api.get("backoff_seconds", 0.5), 0)

    return snapshot


def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub Triage Metrics — read-only collector")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out-dir", default="data/raw")
    ap.add_argument(
        "--cohort", default=None, choices=["config", "last", "since"],
        help="which cohort window to collect. 'config' = the explicit "
             "cohort.start_date/end_date in config.yaml (default). 'last' = the "
             "previous full calendar month(s). 'since' = CUMULATIVE from the fixed "
             "anchor (cohort.since_date / --since) to the last completed month — "
             "the default monthly mode, so each run ADDS the new period to the "
             "previous data instead of replacing it. CLI wins over cohort.mode in "
             "config.yaml.",
    )
    ap.add_argument(
        "--months-back", type=int, default=None,
        help="with --cohort last: how many full calendar months to cover "
             "(default: cohort.months_back in config.yaml, else 1).",
    )
    ap.add_argument(
        "--since", default=None,
        help="with --cohort since: fixed anchor date (YYYY-MM-DD, UTC); the "
             "window starts here and the end advances each month "
             "(default: cohort.since_date in config.yaml).",
    )
    ap.add_argument(
        "--as-of", default=None,
        help="reference date used to compute the end of the window (default: now, "
             "UTC). Format YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ. Useful for "
             "deterministic / backfill runs.",
    )
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "GITHUB_TOKEN not set.\n"
            "  - Create a read-only fine-grained or classic token, then:\n"
            "      export GITHUB_TOKEN=ghp_...\n"
            "  - The collector only GETs data and never writes to GitHub.\n"
            "  - To demo offline without any token:  python make_mock_data.py\n",
            file=sys.stderr,
        )
        return 1

    config = load_config(args.config)
    os.makedirs(args.out_dir, exist_ok=True)

    start, end, cohort_meta = _effective_cohort(
        config, mode=args.cohort, months_back=args.months_back,
        as_of=args.as_of, since=args.since,
    )
    print(
        f"[collect] cohort window {start.isoformat(timespec='seconds')} .. {end.isoformat(timespec='seconds')} "
        f"(mode={cohort_meta.get('cohort_mode')})",
        file=sys.stderr,
    )

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "triage-metrics-collector (read-only)",
        }
    )

    snapshot = collect(config, args.out_dir, session, start=start, end=end, cohort_meta=cohort_meta)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    main_path = os.path.join(args.out_dir, "raw_snapshot.json")
    with open(main_path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)
    with open(os.path.join(args.out_dir, f"raw_snapshot-{ts}.json"), "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, sort_keys=True)

    total_issues = sum(len(r["issues"]) for r in snapshot["repos"].values())
    print(f"[collect] done. {total_issues} cohort issues -> {main_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
