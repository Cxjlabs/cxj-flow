#!/usr/bin/env python3
"""CXJ Flow runner. One tick: reconcile running builders, apply SQL from merged PRs, start gate agents, dispatch builds.

Run every few minutes by launchd (see setup.sh). All config lives in WORKFLOW.md (in this repo, or in $FLOW_HOME).
Linear is read through Claude Code's own Linear connection (no API key): a small `claude -p` call lists work.

  python3 flow/runner.py            # one tick
  python3 flow/runner.py --dry-run  # say what it would start, start nothing
"""
import fcntl, json, os, re, signal, subprocess, sys, time
from pathlib import Path

import yaml

import agents
from agents import CODE, FLOW
STATE = FLOW / "state.json"
DRY = False
LOGS = FLOW / "logs"
LABELLED = {"type": "array", "items": {"type": "object", "properties": {
    "identifier": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}}},
    "required": ["identifier", "labels"]}}
SHIP = {"type": "array", "items": {"type": "object", "properties": {
    "identifier": {"type": "string"}, "status": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}}},
    "required": ["identifier", "status", "labels"]}}
DISPATCH_SCHEMA = {
    "type": "object",
    "properties": {"issues": {"type": "array", "items": {"type": "object", "properties": {
        "identifier": {"type": "string"}, "title": {"type": "string"}, "branch": {"type": "string"},
        "status": {"type": "string"}, "labels": {"type": "array", "items": {"type": "string"}},
        "repo": {"type": "string"}, "blocked_by": {"type": "array", "items": {"type": "object", "properties": {
            "identifier": {"type": "string"}, "status": {"type": "string"}}, "required": ["identifier", "status"]}}},
        "required": ["identifier", "title", "branch", "status", "labels", "repo", "blocked_by"]}},
        "ship": SHIP, "plan": LABELLED, "projects": LABELLED},
    "required": ["issues", "ship", "plan", "projects"],
}


def log(msg):
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOGS / "runner.log", "a") as f:
        f.write(line + "\n")


def load_workflow():
    _, front, body = (FLOW / "WORKFLOW.md").read_text().split("---", 2)
    cfg = yaml.safe_load(front)
    if not cfg["tracker"]["team"]:
        sys.exit("WORKFLOW.md: tracker.team is empty — run setup first (see README).")
    return cfg, body.strip()


def claude_json(cfg, prompt, schema):
    out = subprocess.run(["claude", "-p", prompt, "--model", cfg["models"]["dispatcher"], "--output-format", "json",
                          "--json-schema", json.dumps(schema), "--permission-mode", "dontAsk",
                          "--allowedTools", cfg["tools"]["linear"]],
                         capture_output=True, text=True, timeout=600, cwd=FLOW)
    res = json.loads(out.stdout)
    # A refused non-Linear tool (a plugin nudging the model towards a sandbox, say) is the permission system working:
    # the model carries on with Linear. Only a refused Linear tool means the answer can't be trusted.
    denied = [d for d in res.get("permission_denials") or [] if d.get("tool_name", "").startswith(cfg["tools"]["linear"])]
    if res.get("is_error") or denied:
        raise RuntimeError(f"dispatcher failed: {denied or res.get('result')}")
    so = res.get("structured_output")
    return so if so is not None else json.loads(res["result"])


def labelled(items, wanted):
    """Identifiers whose own label list really holds one of `wanted`: the small model over-reports (`needs-plan` is not `plan`)."""
    return [i["identifier"] for i in items if set(i["labels"]) & set(wanted)]


def buildable(items, states, label):
    """Issues really in an active state with the build label: the small model over-reports here too (it has sent Done ones)."""
    # ponytail: trusts the status/labels the model copies from Linear; read them over the Linear API if it ever invents those
    return [i for i in items if i["status"] in states and label in i["labels"]]


def blocked(issue, terminal):
    """Blockers that aren't finished yet. Checked here because the small model lets blocked issues through."""
    return [b["identifier"] for b in issue.get("blocked_by") or [] if b["status"] not in terminal]


def notify(msg):
    """Ping the owner: runs $FLOW_HOME/notify.sh "<message>" if you made one (ntfy, Slack, anything). Never raises."""
    hook = FLOW / "notify.sh"
    if DRY or not hook.exists():
        return
    try:
        subprocess.run([str(hook), msg], capture_output=True, timeout=30)
    except Exception as e:
        log(f"notify failed: {e!r}")


def find_work(cfg, running_ids):
    t, lb = cfg["tracker"], cfg["labels"]
    prompt = (
        f"Using the Linear tools (team {t['team']}), with filtered list_issues calls that keep results small, "
        f"and no other tools:\n"
        f"1. `issues`: issues in state {t['active_states']} with label `{lb['build']}`, excluding any with labels "
        f"{[lb['idea'], lb['triage'], lb['decision']]}, any whose title starts with {t['excluded_title_prefixes']}, and any "
        f"blocked by an issue not in {t['terminal_states']}. For each: identifier, title, gitBranchName as branch, status = its current "
        f"state name and labels = every label name on it, exactly as Linear returns them, and repo = "
        f"the repo name from its project description's 'Repo: owner/name' line (just `name`, or 'none'), and blocked_by = "
        f"every issue it is blocked by (get_issue with relations) as identifier + current status name, [] if none.\n"
        f"2. `ship`: issues in state {t['handoff_state']} with label `{lb['go']}`, each with status = its current state name.\n"
        f"3. `plan`: open issues with label `{lb['plan']}` or `{lb['approved']}` (one filtered call per label).\n"
        f"4. `projects`: projects of this team with the project label `{lb['plan']}` or `{lb['approved']}` (list_projects, one "
        f"filtered call per label, asking for the labels field). Use the project's name as its identifier.\n"
        f"For 2, 3 and 4 give each one's identifier and labels = every label name on it, exactly as Linear returns them.\n"
        f"Read only: change nothing. Return JSON only.")
    res = claude_json(cfg, prompt, DISPATCH_SCHEMA)
    # state checked here too: the small model kept sending a Done and a Canceled issue, so the shipper ran ~hourly and pinged each time
    res["ship"] = [i["identifier"] for i in buildable(res["ship"], [t["handoff_state"]], lb["go"])]
    res["plan"] = labelled(res["plan"], [lb["plan"], lb["approved"]])
    # a project is planned by the same agent; `project "Name"` tells it which kind it has been handed
    res["plan"] += [f'project "{n}"' for n in labelled(res["projects"], [lb["plan"], lb["approved"]])]
    ok = buildable(res["issues"], t["active_states"], lb["build"])
    for i in res["issues"]:
        if i not in ok:
            log(f"{i['identifier']}: skipped (state '{i['status']}', labels {i['labels']}: not {t['active_states']} + `{lb['build']}`)")
    res["issues"] = [i for i in ok if i["identifier"] not in running_ids]
    return res


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile(cfg, state):
    a = cfg["agent"]
    for ident, r in list(state["running"].items()):
        logf = Path(r["log"])
        idle = (time.time() - logf.stat().st_mtime) / 60 if logf.exists() else 0
        if alive(r["pid"]) and idle < a["stall_minutes"]:
            continue
        if alive(r["pid"]):
            log(f"{ident}: stalled {idle:.0f} min, killing pid {r['pid']}")
            os.killpg(r["pid"], signal.SIGTERM)
        del state["running"][ident]
        text = logf.read_text() if logf.exists() else ""
        what = f"{ident} {r.get('title', '')}".strip()
        if "[Flow] ready for review" in text or "[Flow] blocked" in text:
            log(f"{ident}: finished (attempt {r['attempt']})")
            state["retries"].pop(ident, None)
            notify(f"Ready for review: {what}. Add the `{cfg['labels']['go']}` label to ship it." if "[Flow] ready for review" in text
                   else f"Blocked, has a question for you: {what}")
        elif r["attempt"] >= a["max_retries"]:
            log(f"{ident}: gave up after {r['attempt']} attempts — needs a person")
            state["retries"][ident] = {"attempt": r["attempt"], "due": float("inf")}
            notify(f"Gave up after {r['attempt']} attempts, needs you: {what}")
        else:
            backoff = a["retry_backoff_seconds"][min(r["attempt"] - 1, len(a["retry_backoff_seconds"]) - 1)]
            state["retries"][ident] = {"attempt": r["attempt"], "due": time.time() + backoff}
            log(f"{ident}: exited without finishing, retry in {backoff}s")


def sh(cmd, cwd=None):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    return out.returncode, (out.stdout + out.stderr).strip()


def linear_comment(cfg, ident, text):
    """One small model call with only the Linear tools: Flow has no Linear key yet (W2 adds one)."""
    sh(["claude", "-p", f"Add this comment to Linear issue {ident}, exactly as written, and do nothing else:\n\n{text}",
        "--model", cfg["models"]["dispatcher"], "--permission-mode", "dontAsk", "--allowedTools", cfg["tools"]["linear"]])


def apply_merged(cfg, state, dry, run=sh, save=None):
    """After the owner merges a PR, apply its SQL files whose first line is `-- apply`, through apply-sql.

    The merge is the yes for the database change too. Each (PR, file) is applied at most once: it is marked in
    state.json before apply-sql runs, and apply-sql --once refuses a source it already applied. The outcome is
    commented on the PR and on the Linear issue named in the PR. Off unless WORKFLOW.md has `apply_sql.command`.
    """
    conf = cfg.get("apply_sql") or {}
    if not conf.get("command"):
        return
    since = state.setdefault("apply_since", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))  # never apply older merges
    done = state.setdefault("applied", {})
    save = save or (lambda: STATE.write_text(json.dumps(state, indent=1)))
    team = cfg["tracker"]["team"]
    for name, path in (cfg.get("repos") or {}).items():
        repo_dir = os.path.expanduser(path)
        try:  # one broken repo must not stop the others
            rc, out = run(["gh", "pr", "list", "--state", "merged", "--search", f"merged:>={since}", "--limit", "30",
                           "--json", "number,title,body,headRefName,mergeCommit,url,files"], repo_dir)
            if rc:
                raise RuntimeError(out[-300:])
            prs = json.loads(out or "[]")
        except Exception as e:
            log(f"apply-sql: {name}: can't list merged PRs: {e!r}")
            continue
        for pr in prs:
            sha = (pr.get("mergeCommit") or {}).get("oid")
            for f in pr.get("files") or []:
                key = f"{name}#{pr['number']}:{f['path']}"
                if not f["path"].endswith(".sql") or key in done or not sha:
                    continue
                try:
                    apply_one(cfg, conf, pr, f["path"], sha, key, repo_dir, team, done, dry, run, save)
                except Exception as e:
                    log(f"apply-sql: {key}: {e!r}")


def apply_one(cfg, conf, pr, path, sha, key, repo_dir, team, done, dry, run, save):
    run(["git", "fetch", "-q", "origin"], repo_dir)
    rc, sql = run(["git", "show", f"{sha}:{path}"], repo_dir)
    if rc or not sql.lstrip().lower().startswith("-- apply"):
        done[key] = "not marked -- apply" if not rc else "not in the merge (deleted?)"
        return
    found = re.search(rf"\b{team}-\d+\b", f"{pr['headRefName']} {pr['title']} {pr.get('body') or ''}", re.I)
    issue = found.group(0).upper() if found else None
    if dry:
        return log(f"DRY RUN would apply {key} ({issue or 'no issue'})")
    done[key] = "applying"
    save()  # marked before it runs: a crash mid-apply never re-applies
    LOGS.mkdir(parents=True, exist_ok=True)
    tmp = LOGS / ("apply-" + re.sub(r"[^\w.-]", "_", key))
    tmp.write_text(sql)
    cmd = [os.path.expanduser(conf["command"]), str(tmp), "--source", f"{pr['url']}#{path}", "--once"]
    rc, out = run(cmd + (["--issue", issue] if issue else []), None)
    done[key] = "applied" if rc == 0 else "failed"
    log(f"apply-sql: {key}: {done[key]}")
    text = (f"[Flow] {'Applied' if rc == 0 else 'Did NOT apply'} `{path}` after the merge, through apply-sql:\n"
            f"```\n{out[-1500:]}\n```")
    run(["gh", "pr", "comment", str(pr["number"]), "--body", text], repo_dir)
    if issue:
        linear_comment(cfg, issue, text)
    if rc:
        notify(f"SQL from a merged PR was not applied ({pr['url']}, {path}): {out.splitlines()[-1] if out else ''}")


def workspace(issue, repo_dir):
    ws = FLOW / "workspaces" / issue["identifier"]
    if not ws.exists():
        subprocess.run(["git", "-C", repo_dir, "fetch", "-q", "origin"], check=True)
        has_remote = subprocess.run(["git", "-C", repo_dir, "rev-parse", "--verify", "-q",
                                     f"origin/{issue['branch']}"], capture_output=True).returncode == 0
        start = f"origin/{issue['branch']}" if has_remote else "origin/main"
        ws.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", repo_dir, "worktree", "add", "-q", "-B", issue["branch"], str(ws), start], check=True)
    return ws


def render(body, cfg, issue, ws, attempt):
    vals = {"issue.identifier": issue["identifier"], "issue.title": issue["title"], "issue.branch_name": issue["branch"],
            "workspace.path": str(ws), "flow.path": str(FLOW), "owner": cfg["owner"] or "the owner",
            "attempt": str(attempt), "max_retries": str(cfg["agent"]["max_retries"])}
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: vals.get(m.group(1), m.group(0)), body)


def gates(cfg, found, state, dry):
    """Gate agents (agents.py): ship on `go`, plan on `plan`/`approved`, Todoist triage on a timer."""
    todo = {"ship": found["ship"], "plan": found["plan"]}
    if cfg["tools"].get("todoist") and time.time() - state.get("last_triage", 0) >= cfg["todoist"]["triage_every_minutes"] * 60:
        todo["triage"] = ["inbox"]
        if not dry:
            state["last_triage"] = time.time()
    for name, ids in todo.items():
        if not ids:
            continue
        if dry:
            log(f"DRY RUN would start {name} agent for {', '.join(ids)}")
            continue
        try:  # one broken gate agent must not stop the builders
            if (pid := agents.spawn(cfg, name, ids, state, alive)):
                log(f"{name} agent: started pid {pid} for {', '.join(ids)}")
        except Exception as e:
            log(f"{name} agent: failed to start: {e!r}")


def gate_results(state):
    """When a plan or ship agent has finished, pass its reply on (one plain line per item). Triage stays quiet."""
    for name, job in state.get("jobs", {}).items():
        if name == "triage" or job.get("told") is not False or alive(job["pid"]):
            continue
        job["told"] = True
        text = Path(job["log"]).read_text().strip() if Path(job["log"]).exists() else ""
        if text:
            notify(f"{name.capitalize()} agent: {text[-400:]}")


def dispatch(cfg, body, state, dry):
    a = cfg["agent"]
    found = find_work(cfg, set(state["running"]))
    gates(cfg, found, state, dry)
    slots = a["max_concurrent"] - len(state["running"])
    for issue in found["issues"]:
        if slots <= 0:
            break
        ident = issue["identifier"]
        retry = state["retries"].get(ident)
        if retry and retry["due"] > time.time():
            continue
        if (waiting := blocked(issue, cfg["tracker"]["terminal_states"])):
            log(f"{ident}: skipped (blocked by {', '.join(waiting)})")
            continue
        repos = cfg.get("repos") or {}
        if issue["repo"] not in repos or issue["repo"] in cfg.get("skip_repos", []):
            log(f"{ident}: skipped (repo '{issue['repo']}' not in WORKFLOW.md repos, or in skip_repos)")
            continue
        attempt = (retry["attempt"] + 1) if retry else 1
        if dry:
            log(f"DRY RUN would build {ident} '{issue['title']}' in {issue['repo']} (attempt {attempt})")
            slots -= 1
            continue
        ws = workspace(issue, Path(os.path.expanduser(repos[issue["repo"]])))
        logf = LOGS / f"{ident}-{int(time.time())}.log"
        with open(logf, "w") as out:
            p = subprocess.Popen(["claude", "-p", render(body, cfg, issue, ws, attempt), "--model", cfg["models"]["builder"],
                                  "--permission-mode", "auto", "--output-format", "stream-json", "--verbose"],
                                 cwd=ws, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        state["running"][ident] = {"pid": p.pid, "log": str(logf), "attempt": attempt, "started": time.time(),
                                   "title": issue["title"]}
        log(f"{ident}: started pid {p.pid} attempt {attempt} in {ws}")
        slots -= 1


def main():
    if "--self-check" in sys.argv:
        got = labelled([{"identifier": "A", "labels": ["needs-plan"]}, {"identifier": "B", "labels": ["Bug", "plan"]},
                        {"identifier": "C", "labels": []}], ["plan", "approved"])
        assert got == ["B"], got
        waits = blocked({"blocked_by": [{"identifier": "X", "status": "Done"}, {"identifier": "Y", "status": "Todo"}]}, ["Done", "Canceled"])
        assert waits == ["Y"], waits
        assert blocked({}, ["Done"]) == []
        todo = {"identifier": "T", "status": "Todo", "labels": ["agent"]}
        got = buildable([todo, {"identifier": "D", "status": "Done", "labels": ["agent"]},
                         {"identifier": "N", "status": "Todo", "labels": ["Bug"]}], ["Todo"], "agent")
        assert got == [todo], got
        # merged PR with an `-- apply` file: apply-sql runs once, never twice; unmarked SQL is never applied
        calls, saved = [], []
        pr = {"number": 7, "title": "Fix bookings (CXJ-12)", "body": "", "headRefName": "fix", "url": "https://x/pull/7",
              "mergeCommit": {"oid": "abc"}, "files": [{"path": "a.sql"}, {"path": "b.sql"}, {"path": "x.py"}]}
        def fake(cmd, cwd=None):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "list"]:
                return 0, json.dumps([pr])
            if cmd[:2] == ["git", "show"]:
                return 0, "-- apply\nupdate t set a = 1" if cmd[2].endswith("a.sql") else "update t set a = 2"
            return 0, "applied 1"
        cfg = {"apply_sql": {"command": "apply-sql"}, "repos": {"r": "/tmp"}, "tracker": {"team": "CXJ"},
               "models": {"dispatcher": "haiku"}, "tools": {"linear": "mcp__linear"}}
        st = {}
        global linear_comment
        real, linear_comment = linear_comment, lambda *a: calls.append(["linear", *a[1:]])
        for _ in range(2):
            apply_merged(cfg, st, False, fake, save=lambda: saved.append(dict(st["applied"])))
        linear_comment = real
        applies = [c for c in calls if c[0] == "apply-sql"]
        assert len(applies) == 1 and "--once" in applies[0] and applies[0][-2:] == ["--issue", "CXJ-12"], applies
        assert saved and saved[0]["r#7:a.sql"] == "applying", saved  # marked before apply-sql ran
        assert st["applied"] == {"r#7:a.sql": "applied", "r#7:b.sql": "not marked -- apply"}, st
        assert sum(c[:3] == ["gh", "pr", "comment"] for c in calls) == 1 and ["linear", "CXJ-12"] == [c[:2] for c in calls if c[0] == "linear"][0]
        return print("ok")
    global DRY
    dry = DRY = "--dry-run" in sys.argv
    FLOW.mkdir(parents=True, exist_ok=True)
    with open(FLOW / ".lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # previous tick still running
        cfg, body = load_workflow()
        if cfg.get("self_update"):
            for d in {CODE, FLOW}:  # the code, and your own files if they live in another repo
                subprocess.run(["git", "-C", d, "pull", "-q", "--rebase", "--autostash"], capture_output=True, timeout=120)
            cfg, body = load_workflow()
        state = json.loads(STATE.read_text()) if STATE.exists() else {"running": {}, "retries": {}}
        try:  # save even when the dispatcher fails, or reconcile/gate_results ping the same thing again next tick
            reconcile(cfg, state)
            apply_merged(cfg, state, dry)
            gate_results(state)
            dispatch(cfg, body, state, dry)
        finally:
            if not dry:
                STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
