# Lessons — CXJ Flow
After any correction or bug that took real digging: append the pattern here (what went wrong, why, the rule that prevents it).
Newest at the bottom. Read this at session start.

- Claude.ai connector tools are named `mcp__claude_ai_<Name>` (capitalised). A wrong `--allowedTools` prefix makes headless
  runs silently return nothing — the runner raises on `permission_denials` for this reason.
- Unfiltered Linear `list_issues` results get too big and spill to a file the dispatcher can't read. Always ask for filtered
  calls (state + label) in the dispatcher prompt.
- The builder's finish marker can sit far from the end of a stream-json log; read the whole log, not the tail.
- Hand-copying files onto the runner machine breaks `git pull` there (untracked files block it). Ship changes through git only.
