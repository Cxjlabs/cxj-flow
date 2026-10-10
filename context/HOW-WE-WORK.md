# How we work

Working rules for CXJ Flow agents. The defaults below are sensible; edit anything that doesn't fit.

## Writing
- Plain English a non-technical person understands on first read. Lead with the answer.
- Task IDs are labels, never the explanation: say what changes and for whom.
- Every approval ask says: what it is, who it affects, what changes, what we do next.

## Tasks
- One task = one pull request, small enough to review in 10 minutes.
- Every task has a "Done when" with checks someone can observe.
- Ideas are never built until the owner adds the `plan` label, and plans never until `approved`.

## Code
- Smallest change that works. Match the existing style. No new dependencies without saying why.
- Tests or a verify step for anything with logic in it.

## Repo-specific notes
TODO (optional): per-repo rules, e.g. "web-app: run `npm run check` before a PR".
