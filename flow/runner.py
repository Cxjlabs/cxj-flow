#!/usr/bin/env python3
"""CXJ Flow runner. One tick: reconcile running builders, start gate agents, dispatch new builds.

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
LOGS = FLOW / "logs"
LIST = {"type": "array", "items": {"type": "string"}}
DISPATCH_SCHEMA = {
    "type": "object",
    "properties": {"issues": {"type": "array", "items": {"type": "object", "properties": {
        "identifier": {"type": "string"}, "title": {"type": "string"}, "branch": {"type": "string"},
        "repo": {"type": "string"}}, "required": ["identifier", "title", "branch", "repo"]}},
        "ship": LIST, "plan": LIST},
    "required": ["issues", "ship", "plan"],
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
    if res.get("is_error") or res.get("permission_denials"):
        raise RuntimeError(f"dispatcher failed: {res.get('permission_denials') or res.get('result')}")
    so = res.get("structured_output")
    return so if so is not None else json.loads(res["result"])


def find_work(cfg, running_ids):
    t, lb = cfg["tracker"], cfg["labels"]
    prompt = (
        f"Using the Linear tools (team {t['team']}), with filtered list_issues calls that keep results small, "
        f"and no other tools:\n"
        f"1. `issues`: issues in state {t['active_states']} with label `{lb['build']}`, excluding any with labels "
        f"{[lb['idea'], lb['triage'], lb['decision']]}, any whose title starts with {t['excluded_title_prefixes']}, and any "
        f"blocked by an issue not in {t['terminal_states']}. For each: identifier, title, gitBranchName as branch, and repo = "
        f"the repo name from its project description's 'Repo: owner/name' line (just `name`, or 'none').\n"
        f"2. `ship`: identifiers of issues in state {t['handoff_state']} with label `{lb['go']}`.\n"
        f"3. `plan`: identifiers of open issues with label `{lb['plan']}` or `{lb['approved']}`.\n"
        f"Read only: change nothing. Return JSON only.")
    res = claude_json(cfg, prompt, DISPATCH_SCHEMA)
    res["issues"] = [i for i in res["issues"] if i["identifier"] not in running_ids]
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
        if "[Flow] ready for review" in text or "[Flow] blocked" in text:
            log(f"{ident}: finished (attempt {r['attempt']})")
            state["retries"].pop(ident, None)
        elif r["attempt"] >= a["max_retries"]:
            log(f"{ident}: gave up after {r['attempt']} attempts — needs a person")
            state["retries"][ident] = {"attempt": r["attempt"], "due": float("inf")}
        else:
            backoff = a["retry_backoff_seconds"][min(r["attempt"] - 1, len(a["retry_backoff_seconds"]) - 1)]
            state["retries"][ident] = {"attempt": r["attempt"], "due": time.time() + backoff}
            log(f"{ident}: exited without finishing, retry in {backoff}s")


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
        state["running"][ident] = {"pid": p.pid, "log": str(logf), "attempt": attempt, "started": time.time()}
        log(f"{ident}: started pid {p.pid} attempt {attempt} in {ws}")
        slots -= 1


def main():
    dry = "--dry-run" in sys.argv
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
        reconcile(cfg, state)
        dispatch(cfg, body, state, dry)
        if not dry:
            STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
