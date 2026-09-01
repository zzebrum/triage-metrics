# Triage Metrics Pipeline

A local, read-only data pipeline that reconstructs triage history from public
GitHub repositories and renders a self-contained HTML dashboard. Its purpose is
**data collection and observability** — historical reconstruction, transparent
classification, auditability and simplicity — not performance evaluation.

> **Read-only guarantee:** the collector (`collect.py`) only issues GET
> requests against the GitHub API. Nothing is ever created, edited, assigned,
> commented or closed on GitHub.

---

## 1. What it answers

For the triage team, per cohort issue:

1. How many issues does each triager process?
2. How long do issues remain under each triager's ownership?
3. How many issues are initially routed to another triage team member?
4. How many issues are handed off between triagers after real ownership?
5. How many issues are handed off to development?
6. How many issues are resolved directly by the triager?
7. Spam/invalid (excluded from normal cycle-time analysis)?
8. Which issues are currently aging / potentially stuck?
9. How do these metrics differ between months?
10. Which individual issues have unusually long ownership times?

---

## 2. Cohort definition

Issues whose `created_at` is inside the **half-open** window:

```text
2026-02-01T00:00:00Z  ≤  created_at  <  2026-08-01T00:00:00Z
```

This is exactly **February 2026 … July 2026** (six full calendar months) and
never includes August 1. An issue created inside the window stays in the cohort
even if it is completed after July 31, and ownership intervals are **never
truncated** at the cohort end.

| created_at          | in cohort? |
|---------------------|------------|
| 2026-01-31T23:59:59Z| no         |
| 2026-02-01T00:00:00Z| yes        |
| 2026-07-31T23:59:59Z| yes        |
| 2026-08-01T00:00:00Z| no         |

**Timezone:** all timestamps are UTC. Configuration dates without an explicit
offset are treated as UTC. Times shown in the dashboard are UTC.

### 2.1 Running once a month ("the last period")

Instead of hand-editing `start_date`/`end_date` each month, tell the collector to
gather the **previous full calendar period** automatically:

```bash
python collect.py --cohort last --months-back 1     # previous month
python collect.py --cohort last --months-back 6     # previous six months (current setup)
```

With the defaults in `config.yaml` you can also just set
`cohort: {mode: last, months_back: 1}` and run plain `python collect.py` — the
CLI `--cohort`/`--months-back` override the config when given.

- The window is computed as the previous **full calendar months** relative to
  today (UTC), always on month boundaries and half-open: running in August gives
  exactly July 2026, never a rolling 30 days and never a slice of the still-open
  current month. `months_back=6` reproduces the Feb..Jul window above.
- The computed window is written into the snapshot's meta
  (`cohort_start` / `cohort_end`, plus `cohort_mode`, `cohort_months_back`,
  `cohort_as_of_ref`), and `analyze.py` **always analyzes the exact window the
  collector recorded** (falling back to `config.yaml` only for snapshots that
  carry no window, e.g. legacy files). Analysis and collection therefore cannot
  drift apart on a monthly schedule.
- For deterministic, reproducible backfills use `--as-of YYYY-MM-DD` (the run's
  reference date):

  ```bash
  python collect.py --cohort last --as-of 2026-08-01 --months-back 1
  ```

---

## 3. Architecture

```text
GitHub REST API (read-only)
        │  GET issues / events / comments
        ▼
collect.py ─────────────► data/raw/raw_snapshot-<ts>.json   (normalized snapshot)
        │
        ▼
make_mock_data.py ──────► mock/raw_snapshot.json            (offline demo dataset)
        │
        ▼
analyze.py ──► data/processed/triage_ownership.json   (one row per ownership interval)
              ├── data/processed/issue_summary.json
              ├── data/processed/metrics.json          (documented aggregates)
              ├── data/processed/data_quality.json     (flagged records)
              └── dashboard/dashboard.html             (self-contained, opens in a browser)

/triage-quality (opencode command, LOCAL ONLY — §14)
        ▼
data/processed/triage_quality_report.{json,md}   (per-triager: non-English titles,
                                                  unanswered follow-ups on closed issues)
```

No database, no backend server, no hosted service, no React. The dashboard is a
single HTML file with the data embedded at generation time — it works offline
from `file://` with no network.

---

## 4. Setup

```bash
cd triage_metrics
python3 -m venv .venv
source .venv/bin/activate            # or: .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Create a **read-only** GitHub token and export it (never commit it):

```bash
export GITHUB_TOKEN=github_pat_...# or ghp_...
```

The token is only ever sent in `Authorization: Bearer …` on GET requests and
raises the API rate limit from 60 to 5000 requests/hour. Public repos need no
extra scopes for reading.

---

## 5. Running

**Offline demo (no token needed):**

```bash
python make_mock_data.py
python analyze.py --raw mock/raw_snapshot.json
```

**Real data:**

```bash
python collect.py                # fetches cohort issues + full assignee/label/close events + comments
python analyze.py                # reconstructs intervals, metrics, dashboard
```

**Monthly run (gathers only the last period):**

```bash
python collect.py --cohort last --months-back 1      # @monthly: previous full calendar month
python analyze.py
```

A typical cron entry for the 2nd day of each month:

```cron
0 2 2 * *  cd /path/to/triage_metrics && GITHUB_TOKEN=... ./run_monthly.sh
```

where `run_monthly.sh` runs the two commands above. Running on the 1st of each
month also works (the previous *completed* month is still selected). Because
collect writes a timestamped snapshot each run and analyze regenerates
everything from scratch, monthly reruns are idempotent — see §11.

**View the dashboard:** open `dashboard/dashboard.html` directly in a browser
(double-click), or if you prefer a server:

```bash
python -m http.server 8000 --bind 127.0.0.1
# → http://127.0.0.1:8000/dashboard/dashboard.html
```

### 5.1 Public deployment (automatic monthly via GitHub Actions + Pages)

The repo ships with two workflows under `.github/workflows/` that make this a
fire-and-forget, one-per-month run, publishing the dashboard to GitHub Pages:

- `ci.yml` — runs on every push/PR: the unit tests plus the offline mock
  pipeline, and verifies the generated dashboard loads no external resources.
- `monthly.yml` — on a cron schedule (1st of each month) runs
  `collect.py --cohort last --months-back 1`, then `analyze.py`, commits the
  refreshed results, and deploys the dashboard to Pages. `workflow_dispatch`
  lets you run/backfill manually (`months_back` / `as_of` inputs).

One-time setup (public repo, free plan):

1. Add a repository secret named `GH_READ_TOKEN` containing a read-only PAT.
   The collector only issues GET requests; the ambient Actions
   `GITHUB_TOKEN` is capped at 1,000 REST requests/h, which full event+comment
   history can exceed, so the PAT (5,000/h) is injected explicitly.
2. Enable **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. On the first run, choose **Actions → Monthly triage metrics → Run workflow**
   (e.g. `months_back: 6, as_of: 2026-08-01`) as a smoke test, then open
   `https://<user>.github.io/triage-metrics/`.

Because the dashboard is a single self-contained HTML file, no build step is
needed and the deployed page is identical to the local one. Raw timestamped
snapshots are git-ignored (only the canonical `data/raw/raw_snapshot.json` plus
`data/processed/*` are committed); the schedule is kept alive by the monthly
run itself.

**Filters:** the **Triager** and **Repository** dropdowns are *multiselect*
(checkbox panels — tick several people/repos, e.g. a sub-team working the same
repos; empty = All). **Month**, **Outcome** and **Status** are single-select.
Clicking any workload/outcome bar toggles that value in the corresponding
filter.

Every **Summary card** has a hover tooltip (ⓘ) explaining the metric's exact
meaning, so the definitions don't get forgotten.

**Tests:**

```bash
python -m pytest -q
```

---

## 6. Configuration (`config.yaml`)

```yaml
repositories: [ "owner/repo", ... ]          # explicit list, not inferred
triage_team:  [ "user1", ... ]               # ONLY these users are triagers

cohort:
  start_date: 2026-02-01
  end_date: 2026-08-01
  include_aug_1: false                       # half-open window
  # once-a-month mechanism (see §2.1): auto-compute the window as the previous
  # full calendar months instead of hand-editing the dates each month
  # mode: last                               # config (explicit dates) | last (previous months)
  # months_back: 1                           # used only when mode: last
  # CLI overrides: --cohort config|last --months-back N --as-of YYYY-MM-DD

default_assignee_history:                    # per repository, effective_from → assignee
  owner/repo:
    - { effective_from: 2026-02-01, assignee: alice }

exclude_creators: [ "bot-name", ... ]        # issues CREATED by these users are excluded
exclude_team_created_issues: true            # issues CREATED by a triage team member are excluded
exclude_labels:   [ "spam", ... ]            # issues carrying these labels are excluded
exclude_repo_before: { owner/repo: 2026-07-01 }  # issues created BEFORE this date in repo excluded
spam_labels: []                              # explicit signal only; see §9.
cut_triage_after_dev_handoff: true           # after 1st dev handoff, later triager returns are cut
default_assign_window_seconds: 300           # treat assignment within N s of creation as "at creation"
bot_actors: [ "bot-name", ... ]              # performed-reassignment bots (auto-detect '[bot]' too)
```

- Repositories and team members are **not hard-coded**.
- `triage_team` membership is **never** inferred from permissions, activity,
  labels or current assignees.
- The default assignee is a member of the triage team.
- `cohort.mode` (`config` | `last`) and `cohort.months_back` (optional) drive
  the **once-a-month mechanism** of §2.1: with `mode: last`, `collect.py`
  replaces the explicit dates with the previous `months_back` full calendar
  months (relative to today, or to `--as-of`), records the computed window in
  the snapshot meta, and `analyze.py` analyzes exactly that window.

`exclude_creators` (optional) lists users whose **created** issues are excluded
from the cohort from the start — they never reach the metrics, dashboard or Data
Quality report, but remain in `data/raw/raw_snapshot.json` for auditability.
This is used to drop bot/automation noise (e.g. `adguard-octobuddy[bot]`,
`adguard-bot`). Matching is case-insensitive.

`exclude_team_created_issues` (optional, default off) — when `true`, issues
**created by a member of the triage team** are excluded from the cohort entirely
(assumption: the creator already triaged it and passed it straight to
developers). Tracked separately as "Excluded (team-created)" so it stays
auditable.

`exclude_labels` (optional) lists labels that exclude an issue from the cohort
entirely — the same mechanism, but triggered when the label is present in the
issue's current labels **or** was applied via a `labeled` event (even if later
removed). Used to drop `spam`-marked issues from the numbers from the start.

`exclude_repo_before` (optional) maps a repository to a date; issues in that
repo created **before** the date are excluded from the cohort entirely. Used for
a previous default-owner's era that should not appear in statistics (e.g.
`AdGuardMiniForMac` before `IaroslavKhvorostenko` took over, whose default was
`windwak3r`, intentionally not part of the triage team).

`bot_actors` (optional) lists accounts recognized as **bots when they perform
assignment changes**. Any login ending with `[bot]` is auto-detected. When a bot
moves an issue between triagers (e.g. a bulk coverage/vacation reroute), the
interval structure is kept but the transfer actor is recorded
(`ownership_end_actor` / `ownership_start_actor`) and a `bot_reassignment`
Data Quality flag is raised, so it is auditable instead of looking like a
genuine handoff made after real ownership work. (In the current snapshot no
assignment events have a bot actor; this is forward-looking.)

---

## 7. Historical reconstruction algorithms

### 7.1 Default assignee at creation

The GitHub REST API does **not** expose repository default assignees (current or
historical), so v1 does not attempt `historically_known` reconstruction. The
per-repo `default_assignee_history` (date-stamped from the config-repo history)
is the source of truth:

- latest entry with `effective_from <= created_at` wins;
- resolution method recorded as `manually_configured`;
- issues created before the first entry (or in a gap, or on a repo with no
  history) get `default_assignee_at_creation = unknown` and a
  `default_assignee_unknown` Data Quality flag — **never** silently assumed.

`historically_known` and `inferred` are reserved enum values for a future
API-backed reconstruction.

### 7.2 Assignee timeline (from events)

`assigned` / `unassigned` issue events are grouped by timestamp and collapsed
into net transitions. This means an `unassigned(A)` + `assigned(B)` pair in the
same second becomes `A → B` directly (no phantom zero-length "unassigned" gap).
Events with identical timestamps are ordered by event `id` (deterministic).
`closed` boundaries are applied before reassignments at the same timestamp so a
close-while-owned is classified as `issue_closed`, never `unassigned`.

### 7.3 Initial routing vs. triage handoff (activity rule)

A transfer from the **default assignee** to another triager is:

| Evidence before the transfer                      | classification                    |
|---------------------------------------------------|-----------------------------------|
| default recorded **no** observable action         | `initial_routing` (no interval for default) |
| default authored a comment / changed a label / etc. | `triage_handoff` (default keeps an interval) |
| history genuinely incomplete (collection failure) | `unknown` + Data Quality flag     |

Observable action = a comment, a changed label, or being the issue author —
strictly **before** the transfer timestamp. **Pure `assigned`/`unassigned`
events never count** (they record assignment mechanics: the default-assign
auto-hook, self-assignments), so an auto-assigned default holder who only
re-assigns is `initial_routing` and gets no ownership time. Confirming/commenting
*after* the transfer does not count either.

The credit rule is **assignment-based**: whoever is the assignee during a period
owns that interval. Comments never grant or transfer ownership — they are evidence
only in the single test above.

Once a triager has genuinely owned an issue, every later triager→triager
transfer is a straightforward `triage_handoff`.

### 7.4 Ownership intervals

One row per triage ownership interval. Starts:

- `accepted_from_default` — default assignee holds from `created_at`;
- `initial_routing` — triager received the initial distribution (default never owned);
- `triage_handoff` — triager received from a triager who demonstrably owned it;
- `first_assignment` — triager received an issue that was unassigned or held by a
  non-triage user (our approved addition to the spec's enum).

Ends (`ownership_end_reason`): `handoff_to_triage`, `handoff_to_non_triage`,
`issue_closed`, `unassigned`, or (still active) `None`.

### 7.5 Issue outcomes

- `handed_off_to_development` — triager replaced by a non-triage user;
- `resolved_by_triager` — closed while owned by the triager (valid answer/closure);
- `spam_or_invalid` — closed while owned **and** an explicit spam label present;
- `unassigned` — triager unassigned with no replacement;
- `still_active` — open and still owned at collection time;
- `handoff_to_triage` — interval-level mid-chain state (issue went to another triager);
- `unknown` — cannot be determined.

For the first analysis no spam labels are configured, so **all issues are
treated as valid**; quick closures are never treated as spam on their own (they
classify as `resolved_by_triager`).

---

## 8. Data model (one row per ownership interval)

| field | meaning |
|---|---|
| `interval_id` | stable ID = sha1(repo:issue:user:ownership_start) — start-anchored |
| `repository`, `issue_number`, `issue_url`, `issue_title` | issue identity |
| `issue_created_at`, `issue_creation_month` | cohort month (primary reporting dimension) |
| `default_assignee_at_creation`, `default_assignee_resolution_method` | historical default |
| `triage_username`, `previous_assignee`, `next_assignee` | owner + chain links |
| `ownership_start_actor`, `ownership_end_actor` | who performed the assignment / handoff (null when GitHub omits it) |
| `ownership_start`, `ownership_end`, `ownership_duration_hours/days` | interval |
| `ownership_completion_month` | month of the interval end (distinct from cohort month) |
| `start_type`, `ownership_end_reason`, `issue_outcome`, `transition_type` | classification |
| `originated_from_initial_routing/handoff` | boolean convenience flags |
| `data_collected_at`, `data_quality_flags` | auditability |

`transition_type` records the classification of the **incoming** transition and
takes the same vocabulary as the start type plus `handoff_to_non_triage`,
`unassigned`, `issue_closed`, `unknown`.

---

## 9. Metrics

- **Overall workload** includes **all** observable ownership intervals
  (spam/invalid remain — they are real incoming work).
- **Normal triage cycle time** covers completed, non-spam intervals:
  average / median / p75 / p90 (hours and days).
- **Time to development handoff** covers per-issue lead time from the **first
  triage assignment** until the handoff to development (only issues whose
  outcome is `handed_off_to_development`): count / average / median / p75 / p90.
  Because it starts from the first triage assignment, it spans multi-triager
  chains (routing + hard-issue/vacation handoffs) rather than only the last
  triager's ownership span.
- Reporting dimension = **issue creation month** (`Feb`…`Jul 2026`); the
  same interval also carries a `completion_month` so cohort month and completion
  month are never mixed.
- Percentiles use the nearest-rank method; small samples are flagged in the
  docs/metrics (interpret p90 on small n with care).

**Active/aging work:** an attention signal, **not** a performance score. An
issue older than 72h may simply be waiting on a developer or information. The
GitHub data cannot prove "triager delay", so no such label is produced.

## 10. Data quality

Flagged records (never silently dropped): unresolved default assignee,
incomplete history, multi-assignee issues, non-triage→triager hand-backs,
closed→reopened restarts, issues in the cohort with no triage interval,
ambiguous routing, inconsistent timestamps. The report lists repository, issue,
category, detail and (where applicable) interval ID.

---

## 11. Incremental execution

Rerunning the pipeline is **idempotent, no duplicates**:

- `collect.py` always fetches fresh history from GitHub (source of truth) and
  writes a timestamped snapshot plus the canonical `raw_snapshot.json`.
- Monthly runs use `--cohort last` (see §2.1) so each run gathers only the
  **new last period** instead of re-fetching from the first month of the year.
  The collector computes the window (previous full calendar months, half-open),
  stores it in the snapshot's meta, and `analyze.py` analyzes exactly the window
  that was recorded — the config's fixed dates are only a fallback.
- `interval_id` is anchored to the **start** timestamp; the end timestamp is a
  mutable field. When an active interval later becomes completed, its existing
  row is updated in place — the ID does not change, so no duplicate row is
  created. (Because each monthly run has its own cohort month(s), IDs never
  collide across months either.)
- `analyze.py` regenerates all processed files and the dashboard from scratch
  each run, so repeated executions converge on the same output.

---

## 12. Edge cases & limitations

- **Multiple assignees:** only the primary assignee timeline is modelled;
  multi-assignee periods are flagged in Data Quality.
- **Reopened issues:** closure ends the interval (`issue_closed`); if the issue
  is reopened with a triager still assigned, a new interval starts at reopen
  time (flagged `issue_reopened`).
- **Bots / deleted / renamed users:** resolved as `unknown` where they cannot be
  classified and are flagged.
- **Non-triage user assigned at creation / dev→triager hand-back:** recorded as
  `first_assignment` and flagged.
- **`unassigned` interval endings are intentional in this org:** a triager ending
  an interval by unassignment can mean "sent to the development backlog for
  further investigation" (triage work done) or "feature request assigned to a PM
  outside the triage team". The pipeline records the observable `unassigned`
  ending; it does not guess which human process it represents.
- **Post-triage returns are cut** (`cut_triage_after_dev_handoff`): after the
  first handoff to a non-triage (dev) user, later re-assignments of the issue to
  a triager ("ready to check", QA verification, reopen-and-reassign) do NOT open
  new triage intervals; they are only counted as `post_dev_returns` on the issue
  summary. Genuine triager→triager handoffs that occur *before* any dev handoff
  (difficult issue, vacation coverage, help) are still real `triage_handoff`s.
- **A default holder who merely re-assigns gets no triage time**: `initial_routing`
  cancels the default-holder's interval when they recorded no work before passing
  it on.
- **Issues assigned only to developers / PMs** (never triaged) produce no
  triage ownership interval and are flagged `no_triage_interval` — they are not
  silently dropped. Team-created issues that were assigned straight to a
  non-triage user (e.g. PM or dev backlog) are part of this flag and are
  expected in this org's workflow.
- **Issues assigned only to developers** (never triaged) produce no ownership
  interval and are flagged `no_triage_interval` — they are not silently dropped.
- **GitHub does not record "who" performed an assignment** on `assigned` /
  `unassigned` events (actor is often null). The pipeline therefore never
  guesses intent; only the observable transition is recorded.
- **Communication outside GitHub** (chat, calls, waiting on developers) is not
  measurable and deliberately ignored.
- **No employee ranking.** No best/worst triager, no productivity score. Long
  ownership time is an attention signal only.

---

## 13. Files

```text
triage_metrics/
├── config.yaml                 # repos, team, cohort, default-assignee history, spam labels
├── collect.py                  # read-only GitHub collector
├── analyze.py                  # reconstruction, classification, metrics, DQ, dashboard render
├── make_mock_data.py           # offline demo dataset generator
├── requirements.txt
├── README.md
└── .opencode/                  # LOCAL-ONLY (git-ignored, never in the repo);
    ├── agent/triage-analyst.md     # `/triage-quality` analysis agent     \
    └── command/triage-quality.md   # `/triage-quality` opencode command   / keep per
                                    # machine, do not commit
├── data/raw/                   # raw snapshots (GitHub)
├── data/processed/             # triage_ownership.json, metrics.json, data_quality.json,
│                               # + triage_quality_report.{json,md} (§14, git-ignored)
├── mock/raw_snapshot.json      # offline demo data
├── dashboard/template.html     # dashboard source (single HTML, embedded data)
├── dashboard/dashboard.html    # generated self-contained dashboard
├── tests/test_classification.py# unit tests, offline (classification + monthly window)
└── tests/test_report_data.py   # unit tests, offline (triage-quality data plumbing)
```

---

## 14. Local triage-quality workflow (`/triage-quality`)

A **separate, deliberately local** analysis. Unlike the monthly metrics pipeline
(which can run in CI/Pages), this workflow **runs only on your machine** — it is
an opencode command, not a GitHub Action. The command and the `triage-analyst`
agent live under **`.opencode/`, which is git-ignored and never committed or
pushed** (they're defined for this working copy; create them per machine). It
inspects the collected issues and gives the triage team a per-person review of
two quality habits:

1. **Issue titles should be in English** — the triager is expected to rename a
   title (often written in its original language) to English during triage.
2. **Unanswered follow-ups on closed issues** — when a user continues the
   dialogue on an issue the triager already closed, the triager should still
   reply.

The report is written locally:

```text
data/processed/triage_quality_report.json   # machine-readable (source of truth)
data/processed/triage_quality_report.md     # copy-paste sheet: @slack header + link lists
```

The Markdown is a lean, Slack-ready sheet: one `## @slack_handle (github_login)`
section per flagged triager, and under each just bullet lists of `issue_url`
links — `Non-English titles`, `Unanswered issues after close` (both closed-issue
flags combined) and `Community answered (ok)`. Triagers are addressed by their
**Slack handle** (see `slack_mapping` below) so a line like `@m.kuzmin` plus the
links can be pasted straight into Slack. All detail (rationale, snippets,
reasons) stays in the JSON.

### 14.1 Run it

```bash
# inside opencode, from the repo root:
/triage-quality

# optional filters (analyze everything when omitted):
/triage-quality AdguardTeam/AdguardForAndroid   # one repository
/triage-quality 2026-06                         # one creation month
/triage-quality maxikuzmin                      # one triager
/triage-quality mock                            # offline demo dataset
```

Prerequisites: run the pipeline first so the inputs exist (collect needs a
read-only `GITHUB_TOKEN`; the mock path needs none):

```bash
python collect.py          # or: python make_mock_data.py  for the offline demo
python analyze.py          # produces data/processed/triage_ownership.json (attribution)
```

The command is read-only: it reads `config.yaml`, the raw snapshot and the
ownership data, computes locally, and writes **only** the two report files. It
never touches GitHub and never sends anything anywhere (no Slack, no network).

The two report files are **git-ignored** (`data/processed/triage_quality_report.
{json,md}`) — they are awareness tools for the team, generated locally and never
pushed to the repository.

### 14.2 What is checked

- **Non-English title** — a deterministic heuristic script detector. A title is
  flagged when the majority of its letters belong to a non-Latin script
  (Cyrillic, Greek, CJK, Arabic, Hebrew, Devanagari, Thai, Georgian, …), i.e.
  non-Latin script share ≥ 0.5. A Latin-script title in a foreign language
  (French/German written in a–z) is intentionally not flagged — that is a known
  limitation of the heuristic.
- **Unanswered follow-up (`unanswered_after_close`)** — the issue is closed,
  someone continued the dialogue **after** `closed_at`, and the **earliest**
  such follow-up was **never followed by a triager comment** — the first
  question in the continued dialogue went unanswered by the triage team. A
  triager reply later in the thread counts as engaging (the issue is not
  flagged, even if a user then has the last word).
- **Closed without reply (`closed_without_reply`)** — the issue is closed, the
  last human comment is by a non-triager, and the triager left **zero** comments
  on the issue at all.

Both checks run a **context double-check** before flagging (so healthy threads
are never reported as violations), based on a semantic review of the whole
thread when comment bodies are present:
1. If the **last human comment is by a third party** (not the topic starter),
   the thread ended on a community message — normally the answer — and a silent
   triager close is fine → moved to `community_answered_skipped` (reason
   `last_commenter_not_issue_author`).
2. If a **community member answered** the question meanwhile → moved to
   `community_answered_skipped` (reason `answered_by_community`).
3. If the continued dialogue / last word is only an **acknowledgment** — "thanks",
   "works now" — with no new question, nothing needs a reply → moved to
   `no_reply_needed` (reason `acknowledgment_only`). A thank-you after a triager
   answered is NOT an unanswered issue.
4. Only a genuine, still-unanswered `question_or_report` is flagged
   (`unanswered_after_close` / `closed_without_reply`).
5. When comment bodies are unavailable (old snapshot) the structural rule 1
   still applies; remaining candidates are flagged with a "bodies unavailable,
   re-check manually" note, because a thank-you may be hiding there.

Skipped issues are kept in the report under "Community answered (ok)" and
"No reply needed (thanks)" so the filtering is transparent and auditable.

Bot accounts (logins ending in `[bot]` or listed in `bot_actors`) and bot-created
issues are ignored, and the same cohort exclusions as `analyze.py`
(`exclude_creators`, `exclude_labels`, `exclude_team_created_issues`,
`exclude_repo_before`) are applied so the report matches the metrics population.
Issues are attributed to the triager(s) who owned them (from
`triage_ownership.json`).

### 14.3 Data requirement: comment bodies

The "unanswered follow-up" checks and the report's quoted snippets need the text
of comments. `collect.py` **now stores each comment's `body`**; snapshots
collected before that change have no bodies, so the report lists such issues
with an empty snippet. Re-run `collect.py` after upgrading to get quotation in
the report.

### 14.4 Slack handles (private, local-only)

The Markdown report addresses triagers by Slack handle (`## @m.kuzmin`) so a
line plus its links can be pasted straight into Slack. The GitHub→Slack map
lives in a **private, git-ignored** file `slack_mapping.local.yaml` at the repo
root (git login → Slack username, **without** `@`); it is never committed or
pushed. Create it once on your machine and fill in the real handles — values in
the shipped file are placeholders. Triagers without a mapping fall back to
`@<github login>`. This map is purely cosmetic: attribution never uses it.
