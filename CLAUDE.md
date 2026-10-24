# CXJ Flow — map

Linear + Todoist + Claude Code autopilot. You approve with three Linear labels; agents plan, build and ship.
Config + builder prompt: `WORKFLOW.md`. Company knowledge: `context/`. Code: `flow/`. Install: `setup.sh`.

## Routing
| Task | Read | Run |
|---|---|---|
| "set me up" / first run / `TODO:` left in `context/` | the interview below | `./setup.sh` |
| Change what agents may do | `WORKFLOW.md` (front matter + hard rules) | `python3 flow/runner.py --dry-run` |
| Change a gate agent (plan / ship / triage) | `flow/agents.py` | — |
| Change the daily view | `flow/digest.py` | `python3 flow/digest.py --print` |
| Change how Linear is read or written | `flow/linear.py` | `python3 flow/runner.py --self-check` |
| Why something happened | `logs/runner.log`, `logs/<id>-*.log` | — |
| Past mistakes | `LESSONS.md` — append new ones | — |

## "Set me up" interview
When the user says "set me up", or `WORKFLOW.md` has `team: ""`, or `context/COMPANY.md` still has `TODO:`:
1. Ask, a few at a time, in plain English: their first name; their Linear team key; each GitHub repo agents may work in and
   where it's checked out locally; repos agents must never touch; whether they use Todoist; what time they want the digest.
2. Then walk `context/COMPANY.md` section by section: ask, draft their answer back in a line or two, write it once they agree.
   Offer to read an existing doc or website instead of typing. Leave a section as `TODO:` if they want to skip it.
3. Fill `WORKFLOW.md` (`owner`, `tracker.team`, `repos`, `skip_repos`, `digest.time`; set `tools.todoist: ""` if no Todoist).
4. Remind them each Linear project needs a line `Repo: owner/name` in its description, so the builder knows where to work.
5. Tell them to run `./setup.sh` in a normal Terminal window (not over SSH).

## Rules
- Never weaken the hard rules in `WORKFLOW.md` (no merge without `go`, no deploys, data only through apply-sql, no messages, no spending).
- Facts are code, judgement is an agent: anything a script can read exactly (Linear state, PRs, checks, merged SQL)
  is plain code in `runner.py` / `linear.py`; agents in `agents.py` only plan, build and sort. Secrets (the Linear and Todoist
  API keys) are read from the macOS Keychain at run time, never written into this repo.
- Every per-item step of a tick runs inside `isolated(...)`: an error is logged, listed in the daily view, and skipped.
- Pings: only clash, outage, failed apply and the daily view. Anything else waits for the daily view.
- Every change keeps `python3 flow/runner.py --self-check` green; new behaviour adds a case in `flow/selfcheck.py`.
- Keep it small: a new feature is a few lines in an existing file or a WORKFLOW.md setting before it is a new file.
  SQL from merged PRs is applied only when WORKFLOW.md sets `apply_sql.command` (ABC: `~/.claude/bin/apply-sql`).
