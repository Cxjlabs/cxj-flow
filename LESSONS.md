# Lessons — CXJ Flow
After any correction or bug that took real digging: append the pattern here (what went wrong, why, the rule that prevents it).
Newest at the bottom. Read this at session start.

- Claude.ai connector tools are named `mcp__claude_ai_<Name>` (capitalised). A wrong `--allowedTools` prefix makes headless
  runs silently return nothing — the runner raises on `permission_denials` for this reason.
- Unfiltered Linear `list_issues` results get too big and spill to a file the dispatcher can't read. Always ask for filtered
  calls (state + label) in the dispatcher prompt.
- The builder's finish marker can sit far from the end of a stream-json log; read the whole log, not the tail.
- Hand-copying files onto the runner machine breaks `git pull` there (untracked files block it). Ship changes through git only.
- Gate agent names (plan/ship) differ from the model keys in WORKFLOW.md (planner/shipper); a KeyError there crashed every tick silently. Gate starts are now wrapped so one bad agent can't stop the builders.
- An installed Claude Code plugin can nudge headless runs towards tools they aren't allowed (a "process output in a sandbox"
  hint). The dispatcher was refused, carried on, and the tick still crashed because any refusal counted as failure. Only a
  refused Linear tool fails the tick now.
- The small dispatcher model over-reports labels (it listed issues with no `plan` label, and `needs-plan` is not `plan`). It
  returns each item's labels and the runner filters on exact membership (`labelled`, checked by `runner.py --self-check`).
- The dispatcher is told to leave out blocked issues, but the small model still let one through and the builder was
  started on a task whose blockers were Todo. It now returns each issue's blockers with their status and the runner skips
  any with an unfinished blocker (`blocked`, checked by `runner.py --self-check`).
