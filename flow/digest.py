#!/usr/bin/env python3
"""CXJ Flow daily view: the ONE message a day. Built by code from Linear (linear.py) and Flow's own state, no model.

  shipped (24h) · % vs plan per project · plans awaiting a yes · needs you · clashes · skipped errors · weekly metrics

Sent through notify.sh, and saved as logs/daily-YYYY-MM-DD.md. Run daily by launchd (setup.sh).
  python3 flow/digest.py --print   # build and print it, send nothing
"""
import calendar, json, os, subprocess, sys, time

import linear
from runner import LOGS, STATE, load_workflow, notify


def ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) if s else None


def metrics(week, now):
    """Plan adherence: of the issues shipped this week, the share already in a project before the week began (planned
    work, not a side quest). After-the-fact: issues created and closed within an hour."""
    # ponytail: "in a project before the week" uses createdAt + current project; Linear's issue history would be exact
    start = now - 7 * 86400
    shipped = [i for i in week if i["completedAt"]]
    planned = [i for i in shipped if i["project"] and ts(i["createdAt"]) < start]
    fast = [i for i in week if ts(i["createdAt"]) > start and (ts(i["completedAt"] or i["canceledAt"]) - ts(i["createdAt"])) < 3600]
    return len(planned), len(shipped), [i["identifier"] for i in fast]


def sql_applies(cfg):
    """applied_sql over 7 days: after-merge applies by Flow vs others (sessions, by hand), and refused/failed ones."""
    supaq = os.path.join(os.path.dirname(os.path.expanduser((cfg.get("apply_sql") or {}).get("command") or "")), "supaq")
    if not os.path.exists(supaq):
        return None
    out = subprocess.run([supaq, "select coalesce(source like '%/pull/%', false) as flow, status, count(*) as n from applied_sql "
                                 "where created_at > now() - interval '7 days' group by 1, 2"], capture_output=True, text=True, timeout=120)
    rows = json.loads(out.stdout or "[]") if out.returncode == 0 else []
    flow = sum(r["n"] for r in rows if r["flow"] and r["status"] == "applied")
    other = sum(r["n"] for r in rows if not r["flow"] and r["status"] == "applied")
    bad = sum(r["n"] for r in rows if r["status"] != "applied")
    return f"SQL applies (7d): {flow} by Flow after a merge, {other} from sessions, {bad} refused or failed"


def build(cfg, state, d, now=None, sql=None):
    now = now or time.time()
    lb, out = cfg["labels"], [f"# Flow daily · {time.strftime('%a %d %b', time.localtime(now))}"]

    def section(title, rows):
        if rows:
            out.extend(["", f"## {title}", *(f"- {r}" for r in rows)])

    section("Shipped (24h)", [f"{i['identifier']} {i['title']}" + (f" ({i['project']['name']})" if i["project"] else "")
                              for i in d["shipped"]["nodes"]])
    section("Projects vs plan", [f"{p['name']}: {round(p['progress'] * 100)}% ({p['status']['name']})" for p in d["projects"]["nodes"]])
    waiting = [f"project \"{p['name']}\": plan written, add the project label `{lb['approved']}` to build it"
               for p in d["projects"]["nodes"] if "[Planner] plan" in (p["description"] or "")
               and p["status"]["name"] == "Planned" and lb["approved"] not in linear.names(p["labels"])]
    waiting += [f"{i['identifier']} {i['title']}: plan written, add `{lb['approved']}` to build it" for i in d["awaiting"]["nodes"]
                if lb["approved"] not in linear.names(i["labels"]) and lb["build"] not in linear.names(i["labels"])]
    section("Plans awaiting your yes", waiting)
    stuck = [f"{i['identifier']} {i['title']}: the builder has a question (answer in Linear, move it back to Todo)" for i in d["blocked"]["nodes"]]
    stuck += [f"{k} {r.get('title', '')}: the builder gave up after {r['attempt']} tries" for k, r in state.get("retries", {}).items()
              if r["due"] == float("inf")]
    section("Needs you", stuck)
    jan = state.get("janitor") or {}
    section("Board tidy-ups waiting for your yes (janitor; do them in Linear, or tell a session)", jan.get("proposals"))
    section("Janitor moved (last run, each with a [Janitor] comment saying why)", jan.get("made"))
    section("Janitor marked these projects Completed (last run)", jan.get("completed"))
    clash = sorted(state.get("clash", {}).values(), key=lambda c: not c.get("sig"))
    section("Open PRs (clash check)", [("⚠ " if c.get("sig") else "") + c["line"] for c in clash if c.get("line")])
    day_ago, errs = now - 86400, {}
    for e in state.get("errors", []):
        if e["at"] > day_ago:
            errs.setdefault(f"{e['where']} {e['item']}".strip(), []).append(e["error"])
    section("Skipped after an error (24h)", [f"{k}: {len(v)}× — {v[-1][:120]}" for k, v in errs.items()])
    planned, shipped, fast = metrics(d["week"]["nodes"], now)
    rows = [f"Plan adherence (7d): {planned}/{shipped} shipped issues were planned before the week" +
            (f" ({round(100 * planned / shipped)}%, target 80%)" if shipped else ""),
            f"After-the-fact issues (created→closed < 1h, 7d): {len(fast)}" + (f" ({', '.join(fast[:8])})" if fast else "")]
    if sql:
        rows.append(sql)
    if state.get("sorter_mode", "").startswith("timer"):
        rows.append("Sorter: no Todoist key in the Keychain, so it still runs on its timer")
    if state.get("linear_ok") is False:
        rows.append("Linear: Flow can't read it (key in the Keychain?)")
    section("This week", rows)
    return "\n".join(out)


def main():
    cfg, _ = load_workflow()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    text = build(cfg, state, linear.day(cfg), sql=sql_applies(cfg))
    if "--print" in sys.argv:
        return print(text)
    LOGS.mkdir(exist_ok=True)
    (LOGS / f"daily-{time.strftime('%Y-%m-%d')}.md").write_text(text + "\n")
    notify(text, "Flow daily")
    with open(LOGS / "digest.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} sent ({len(text)} chars)\n")
    print(text)


if __name__ == "__main__":
    main()
