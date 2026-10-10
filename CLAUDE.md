# CXJ Flow — map

Linear + Todoist + Claude Code autopilot. You approve with three Linear labels; agents plan, build and ship.
Config + builder prompt: `WORKFLOW.md`. Company knowledge: `context/`. Code: `flow/`. Install: `setup.sh`.

## Routing
| Task | Read | Run |
|---|---|---|
| "set me up" / first run / `TODO:` left in `context/` | the interview below | `./setup.sh` |
| Change what agents may do | `WORKFLOW.md` (front matter + hard rules) | `python3 flow/runner.py --dry-run` |
| Change a gate agent (plan / ship / triage) | `flow/agents.py` | — |
| Change the morning digest | `flow/digest.py` | `python3 flow/digest.py` |
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
- Never weaken the hard rules in `WORKFLOW.md` (no merge without `go`, no deploys, no production data, no messages, no spending).
- Linear and Todoist are reached only through Claude Code's own connectors; never add API keys to this repo.
- Keep it small: three Python files and one shell script. New feature → ask whether a prompt change does it first.
