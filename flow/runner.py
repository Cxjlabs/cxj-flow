#!/usr/bin/env python3
"""CXJ Flow runner. One tick, in this order:

  reconcile builders ─► apply SQL from merged PRs ─► read Linear (linear.py) ─► code shipper (`go`)
      ─► gate agents (plan, sorter) ─► start builders ─► janitor (daily, if on) ─► clash check on every open PR
      ─► daily prune ─► heartbeat

Every per-item step is isolated: an error is logged, kept for the daily view, and that item is skipped.
An idle tick makes no model call. Run every few minutes by launchd (see setup.sh). All config lives in WORKFLOW.md
(in this repo, or in $FLOW_HOME). Linear is read and written over its API (flow/linear.py), GitHub through gh.

  python3 flow/runner.py              # one tick
  python3 flow/runner.py --dry-run    # say what it would do, change nothing
  python3 flow/runner.py --self-check # the tests (flow/selfcheck.py)
"""
import calendar, contextlib, fcntl, fnmatch, hashlib, json, os, re, signal, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

import yaml

import agents, linear
from agents import CODE, FLOW
STATE = FLOW / "state.json"
DRY = False
LOGS = FLOW / "logs"
KEEP_LOGS = {"runner.log", "digest.log", "launchd.out", "launchd.err"}  # append-only logs the prune never deletes
MARK = "<!-- cxj-flow clash check -->"


def log(msg):
    LOGS.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(LOGS / "runner.log", "a") as f:
        f.write(line + "\n")


@contextlib.contextmanager
def isolated(state, where, item=""):
    """One bad item never stops the tick: log the error, keep it for the daily view, carry on with the next item.
    A lost Linear key is not one item, so it goes up to tick(), which pings once."""
    try:
        yield
    except linear.AuthError:
        raise
    except Exception as e:
        log(f"{where} {item}: skipped after error: {e!r}")
        errs = state.setdefault("errors", [])
        errs.append({"at": time.time(), "where": where, "item": str(item), "error": repr(e)[:300]})
        del errs[:-100]


def load_workflow():
    _, front, body = (FLOW / "WORKFLOW.md").read_text().split("---", 2)
    cfg = yaml.safe_load(front)
    if not cfg["tracker"]["team"]:
        sys.exit("WORKFLOW.md: tracker.team is empty — run setup first (see README).")
    linear.PREFIX = cfg["tracker"].get("comment_prefix") or ""
    return cfg, body.strip()


def labelled(items, wanted):
    """Identifiers whose own label list really holds one of `wanted` (`needs-plan` is not `plan`)."""
    return [i["identifier"] for i in items if set(i["labels"]) & set(wanted)]


def buildable(items, states, label):
    """Issues really in an active state with the build label."""
    return [i for i in items if i["status"] in states and label in i["labels"]]


def blocked(issue, terminal):
    """Blockers that aren't finished yet."""
    return [b["identifier"] for b in issue.get("blocked_by") or [] if b["status"] not in terminal]


def notify(msg, title=None):
    """Ping the owner: runs $FLOW_HOME/notify.sh "<message>" ["<title>"] if you made one. Never raises.
    Only clash, outage and failed-apply pings and the one daily view use it; everything else waits for the daily view."""
    hook = FLOW / "notify.sh"
    if DRY or not hook.exists():
        return
    try:
        subprocess.run([str(hook), msg] + ([title] if title else []), capture_output=True, timeout=30)
    except Exception as e:
        log(f"notify failed: {e!r}")


def find_work(cfg, running_ids, read=None):
    """What to build, ship and plan, read exactly from Linear (linear.read_work). The filters stay as a safety net."""
    t, lb = cfg["tracker"], cfg["labels"]
    res = (read or linear.read_work)(cfg)
    res["ship"] = [i["identifier"] for i in buildable(res["ship"], [t["handoff_state"]], lb["go"])]
    res["plan"] = labelled(res["plan"], [lb["plan"], lb["approved"]])
    # a project is planned by the same agent; `project "Name"` tells it which kind it has been handed
    res["plan"] += [f'project "{n}"' for n in labelled(res["projects"], [lb["plan"], lb["approved"]])]
    ok = buildable(res["issues"], t["active_states"], lb["build"])
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
        with isolated(state, "reconcile", ident):
            logf = Path(r["log"])
            # no log at all counts from the start, so a builder that never wrote one is still caught as stalled
            idle = (time.time() - (logf.stat().st_mtime if logf.exists() else r.get("started", 0))) / 60
            if alive(r["pid"]) and idle < a["stall_minutes"]:
                continue
            if alive(r["pid"]):
                log(f"{ident}: stalled {idle:.0f} min, killing pid {r['pid']}")
                try:
                    os.killpg(r["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass  # it exited between the check and the kill
            del state["running"][ident]
            text = logf.read_text() if logf.exists() else ""
            if "[Flow] ready for review" in text or "[Flow] blocked" in text:
                log(f"{ident}: finished (attempt {r['attempt']})")  # the daily view shows what needs a yes
                state["retries"].pop(ident, None)
            elif r["attempt"] >= a["max_retries"]:
                log(f"{ident}: gave up after {r['attempt']} attempts — needs a person")
                state["retries"][ident] = {"attempt": r["attempt"], "due": float("inf"), "title": r.get("title", "")}
            else:
                backoff = a["retry_backoff_seconds"][min(r["attempt"] - 1, len(a["retry_backoff_seconds"]) - 1)]
                state["retries"][ident] = {"attempt": r["attempt"], "due": time.time() + backoff}
                log(f"{ident}: exited without finishing, retry in {backoff}s")


def sh(cmd, cwd=None):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    return out.returncode, (out.stdout + out.stderr).strip()


def gh_json(run, cmd, cwd=None):
    rc, out = run(cmd, cwd)
    if rc:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {out[-300:]}")
    return json.loads(out or "null")


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
        prs = []
        with isolated(state, "apply-sql: list merged PRs", name):
            prs = gh_json(run, ["gh", "pr", "list", "--state", "merged", "--search", f"merged:>={since}", "--limit", "30",
                                "--json", "number,title,body,headRefName,mergeCommit,url,files"], repo_dir) or []
        for pr in prs:
            sha = (pr.get("mergeCommit") or {}).get("oid")
            for f in pr.get("files") or []:
                key = f"{name}#{pr['number']}:{f['path']}"
                if not f["path"].endswith(".sql") or key in done or not sha:
                    continue
                with isolated(state, "apply-sql", key):
                    apply_one(cfg, conf, pr, f["path"], sha, key, repo_dir, team, done, dry, run, save)


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
        linear.comment(issue, text)
    if rc:
        notify(f"SQL from a merged PR was not applied ({pr['url']}, {path}): {out.splitlines()[-1] if out else ''}",
               "Flow: SQL not applied")


# ---------- the code shipper: `go` on an In Review task merges its PR ----------

FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def checks(rollup):
    """One honest line for a PR's checks. A repo with no CI says "none configured", never "green"."""
    if not rollup:
        return "none configured"
    name = lambda c: c.get("name") or c.get("context") or "?"
    bad = [name(c) for c in rollup if (c.get("conclusion") or c.get("state")) in FAIL]
    if bad:
        return "failing: " + ", ".join(bad)
    wait = [name(c) for c in rollup if c.get("status", "COMPLETED") != "COMPLETED" or c.get("state") in ("PENDING", "EXPECTED")]
    return ("pending: " + ", ".join(wait)) if wait else f"green ({len(rollup)})"


def repo_of_url(url):
    return "/".join(url.split("/")[3:5])


def ship(cfg, state, ids, dry, run=sh, lin=linear):
    """Merge (squash) the PR of each In Review task the owner labelled `go`, then close the task. No model involved."""
    t, go = cfg["tracker"], cfg["labels"]["go"]
    done_state = t["terminal_states"][0]
    for ident in ids:
        with isolated(state, "ship", ident):
            if dry:
                log(f"DRY RUN would ship {ident}")
                continue
            i = lin.issue(ident)
            if i["status"] != t["handoff_state"] or go not in i["labels"]:
                continue

            def stop(text, close=False):
                lin.comment(ident, f"[Ship] {text}")
                lin.remove_label(i, go)
                if close:
                    lin.set_state(i, done_state)
                log(f"ship {ident}: {text}")

            if not i["prs"]:
                stop("closed — nothing to merge", close=True)
                continue
            views = [gh_json(run, ["gh", "pr", "view", u, "--json", "state,mergeable,headRefName,baseRefName,statusCheckRollup,url,title"])
                     for u in i["prs"]]
            open_ = [v for v in views if v["state"] == "OPEN"]
            if not open_:
                merged = [v for v in views if v["state"] == "MERGED"]
                stop(f"merged {merged[0]['url']} — {merged[0]['title']}" if merged else "can't merge: its PR is closed", close=bool(merged))
                continue
            if len(open_) > 1:
                stop(f"can't merge: {len(open_)} open PRs ({', '.join(v['url'] for v in open_)}), merge the right one by hand")
                continue
            pr = open_[0]
            repo = repo_of_url(pr["url"])
            if repo.split("/")[1] in (cfg.get("skip_repos") or []):
                stop("this repo ships by hand")
                continue
            ci = checks(pr.get("statusCheckRollup"))
            if pr["mergeable"] == "UNKNOWN" or ci.startswith("pending"):
                log(f"ship {ident}: waiting ({pr['mergeable']}, checks {ci})")  # keeps `go`, tries next tick
                continue
            if pr["mergeable"] != "MERGEABLE" or ci.startswith("failing"):
                stop(f"can't merge: {'conflicts with ' + pr['baseRefName'] if pr['mergeable'] != 'MERGEABLE' else 'checks ' + ci}")
                continue
            # PRs stacked on this branch would be closed when it is deleted: point them at this PR's base first
            for s in gh_json(run, ["gh", "pr", "list", "-R", repo, "--base", pr["headRefName"], "--state", "open", "--json", "number"]):
                run(["gh", "pr", "edit", str(s["number"]), "-R", repo, "--base", pr["baseRefName"]])
            rc, out = run(["gh", "pr", "merge", pr["url"], "--squash", "--delete-branch"])
            if rc:
                stop(f"can't merge: {out.splitlines()[-1] if out else 'gh pr merge failed'}")
                continue
            stop(f"merged {pr['url']} — {pr['title']}", close=True)


# ---------- the clash check: one comment kept current on every open PR ----------

def rules_for(conf, repo):
    rules = {}
    for src in ((conf.get("default") or {}), (conf.get("repos") or {}).get(repo) or {}):
        for area, globs in src.items():
            rules.setdefault(area, []).extend(globs)
    return rules


def touches(paths, rules):
    """{area: [paths]} for the live systems a PR touches (database, n8n, live messaging: per-repo globs in WORKFLOW.md)."""
    hit = {a: [p for p in paths if any(fnmatch.fnmatch(p, g) for g in globs)] for a, globs in rules.items()}
    return {a: ps for a, ps in hit.items() if ps}


def short(p):
    return f"{p['repo'].split('/')[1]}#{p['number']}"


NAME = r'("?\w+"?(?:\s*\.\s*"?\w+"?)?)'
CHANGES = re.compile(  # statements that change a table: two PRs changing the same one can clash; reads and inserts can't
    r'\bupdate\s+(?:only\s+)?' + NAME + r'|\bdelete\s+from\s+(?:only\s+)?' + NAME +
    r'|\btruncate\s+(?:table\s+)?(?:only\s+)?' + NAME + r'|\bmerge\s+into\s+' + NAME +
    r'|\b(?:create|alter|drop)\s+(?:or\s+replace\s+)?(?:unlogged\s+)?(?:table|view|materialized\s+view)\s+'
    r'(?:if\s+(?:not\s+)?exists\s+)?(?:only\s+)?' + NAME + r'|\b(?:before|after|instead\s+of)\s[^;]*?\bon\s+' + NAME, re.I)
TEMP = re.compile(r'\bcreate\s+(?:local\s+|global\s+)?temp(?:orary)?\s+table\s+(?:if\s+not\s+exists\s+)?' + NAME, re.I)


def changed_tables(sql):
    """Tables a SQL file changes (DDL, update, delete, truncate, merge, triggers), schema-qualified. Not reads or
    inserts: two PRs that both add a row to a log table don't clash."""
    # ponytail: a regex, not a SQL parser; a CTE named like a real table, or dynamic SQL, can fool it
    sql = re.sub(r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'", " ", sql, flags=re.S)
    norm = lambda n: (lambda x: x if "." in x else "public." + x)(re.sub(r'[\s"]', "", n).lower())
    temp = {norm(m) for m in TEMP.findall(sql)}
    found = {norm(next(g for g in m if g)) for m in CHANGES.findall(sql)}
    return sorted(t for t in found - temp if t.split(".")[-1] not in {"set", "skip", "nowait", "of"})


def raw(run, p, path):
    """A file's text at the PR's head, or None (deleted in this PR)."""
    rc, text = run(["gh", "api", "-H", "Accept: application/vnd.github.raw",
                    f"repos/{p['repo']}/contents/{urllib.parse.quote(path)}?ref={p['headRefOid']}"])
    return None if rc else text


def pr_tables(state, p, run):
    """Every table the PR's SQL files change, cached by head SHA."""
    cache, out = state.setdefault("sql_tables", {}), set()
    for path in [x for x in p["paths"] if x.endswith(".sql")]:
        k = f"{p['url']}@{p['headRefOid'][:12]}:{path}"
        if k not in cache:
            cache[k] = changed_tables(raw(run, p, path) or "")
        out |= set(cache[k])
    return out


def dry_runs(cfg, state, p, run):
    """apply-sql --dry-run of each `-- apply` SQL file in the PR, at its head. Cached by head + base SHA."""
    cmd = (cfg.get("apply_sql") or {}).get("command")
    cache, out = state.setdefault("dry_runs", {}), []
    for path in p["paths"] if cmd else []:
        if not path.endswith(".sql"):
            continue
        k = f"{p['url']}@{p['headRefOid'][:12]}.{p['baseRefOid'][:12]}:{path}"
        if k not in cache:
            sql = raw(run, p, path)
            cache[k] = None  # deleted in this PR, or not marked `-- apply`: never applied, so nothing to dry-run
            if sql is not None and sql.lstrip().lower().startswith("-- apply"):
                LOGS.mkdir(parents=True, exist_ok=True)
                tmp = LOGS / ("dry-" + re.sub(r"[^\w.-]", "_", k))
                tmp.write_text(sql)
                rc, res = run([os.path.expanduser(cmd), str(tmp), "--dry-run"])
                tmp.unlink()
                cache[k] = {"ok": rc == 0, "out": res[-800:]}
        if cache[k]:
            out.append((path, cache[k]))
    return out


def clash_body(cfg, state, p, prs, run):
    t = p["touches"]
    others, live = [], []
    for o in prs:  # nearby = the same files (same repo) or the same tables changed; a shared area alone is not a clash
        if o is p:
            continue
        for q in (p, o):
            if "tables" not in q:
                q["tables"] = pr_tables(state, q, run)
        tables = sorted(p["tables"] & o["tables"])
        files = sorted(set(o["paths"]) & set(p["paths"])) if o["repo"] == p["repo"] else []
        if tables or files:
            why = ([f"same tables: {', '.join(tables[:4])}"] if tables else []) + ([f"same files: {', '.join(files[:3])}"] if files else [])
            title = f" \"{o['title'][:60]}\"" if o["repo"] == p["repo"] else ""  # another repo's title may be private
            others.append(f"{short(o)}{title} ({'; '.join(why)})")
            if o.get("createdAt", "") <= p.get("createdAt", ""):
                live.append(o["url"])  # the newer PR of a pair reports the clash, so a pair pings once
    sql = dry_runs(cfg, state, p, run)
    ci = checks(p.get("statusCheckRollup"))
    what = "; ".join(f"{a} ({', '.join(fs[:3])}{', …' if len(fs) > 3 else ''})" for a, fs in sorted(t.items()))
    lines = [MARK, f"**Clash check** · kept current by CXJ Flow · head `{p['headRefOid'][:7]}`", "",
             f"- **Touches:** {what or 'nothing live (no database, n8n or messaging paths)'}",
             f"- **In flight, same files or tables:** {'; '.join(others) or 'none'}",
             f"- **Checks:** {ci}"]
    for path, r in sql:
        lines += [f"- **SQL dry run** `{path}`: {'ok' if r['ok'] else 'REFUSED'}", "  ```", *("  " + l for l in r["out"].splitlines()), "  ```"]
    line = (f"{short(p)} {p['title'][:50]} — {', '.join(sorted(t)) or 'nothing live'}; "
            f"{len(others)} in flight nearby; checks {ci}" + "".join(f"; SQL {path} REFUSED" for path, r in sql if not r["ok"]))
    sig = sorted(live) + [path for path, r in sql if not r["ok"]]  # what is worth a ping: a clash, a refused dry run
    return "\n".join(lines), line, sig


def put_comment(run, p, body, cid):
    """Edit Flow's one clash comment on the PR; find it first if the state lost its id, so there is never a second one."""
    issue = f"repos/{p['repo']}/issues"
    if not cid:
        rc, out = run(["gh", "api", f"{issue}/{p['number']}/comments", "--paginate", "--jq",
                       f'.[] | select(.body | startswith("{MARK}")) | .id'])
        cid = out.split()[0] if rc == 0 and out.split() else None
    if cid:
        rc, _ = run(["gh", "api", "-X", "PATCH", f"{issue}/comments/{cid}", "-f", f"body={body}", "--jq", ".id"])
        if rc == 0:
            return cid
    rc, out = run(["gh", "api", f"{issue}/{p['number']}/comments", "-f", f"body={body}", "--jq", ".id"])
    if rc:
        raise RuntimeError(f"can't comment on {p['url']}: {out[-300:]}")
    return out.strip()


def clash_check(cfg, state, dry, run=sh):
    conf = cfg.get("clash") or {}
    prs, listed = [], set()
    for repo in conf.get("repos") or {}:
        with isolated(state, "clash: list PRs", repo):
            rules = rules_for(conf, repo)
            for p in gh_json(run, ["gh", "pr", "list", "-R", repo, "--state", "open", "--limit", "50", "--json",
                                   "number,title,url,createdAt,headRefOid,baseRefOid,files,statusCheckRollup"]) or []:
                p["repo"], p["paths"] = repo, [f["path"] for f in p.get("files") or []]
                p["touches"] = touches(p["paths"], rules)
                prs.append(p)
            listed.add(repo)
    cache = state.setdefault("clash", {})
    # PRs already open when Flow started watching are a baseline: listed in the daily view, never pinged about
    since = state.setdefault("clash_since", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    for p in prs:
        with isolated(state, "clash", p["url"]):
            body, line, sig = clash_body(cfg, state, p, prs, run)
            c = cache.get(p["url"]) or {}
            key = hashlib.sha256(body.encode()).hexdigest()[:16]
            if c.get("key") == key:
                continue  # nothing changed since the last tick: no GitHub write
            if dry:
                log(f"DRY RUN would update the clash check on {p['url']}: {line}")
                continue
            c["id"] = put_comment(run, p, body, c.get("id"))
            if set(sig) - set(c.get("sig") or []) and p.get("createdAt", "") >= since:
                notify(f"Clash: {line}\n{p['url']}", "Flow: clash")  # only a NEW clash pings
            c.update(key=key, line=line, sig=sig, repo=p["repo"])
            cache[p["url"]] = c
    open_urls = {p["url"] for p in prs}
    for url in [u for u, c in cache.items() if c.get("repo") in listed and u not in open_urls]:
        del cache[url]  # merged or closed
    for name in ("dry_runs", "sql_tables"):
        for k in [k for k in state.get(name, {}) if k.split("@")[0] not in open_urls]:
            del state[name][k]


# ---------- gate agents, builders ----------

def unsorted(tasks, done_label, now=None):
    """Open Inbox tasks the sorter hasn't labelled yet (and not added in the last 10 minutes: still being typed)."""
    now = now or time.time()

    def age(t):
        try:
            return now - calendar.timegm(time.strptime(t["added_at"][:19], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, TypeError, ValueError):
            return 1e9
    return [t for t in tasks if done_label not in (t.get("labels") or []) and not t.get("content", "").startswith("Flow —")
            and age(t) >= 600]


def todoist_inbox(cfg):
    """Open Inbox tasks without the sorter's label, read over the Todoist API. None when there is no key in the Keychain
    (security add-generic-password -s todoist-api-key -a cxj-flow -w): the sorter then keeps to its timer."""
    out = subprocess.run(["security", "find-generic-password", "-s", "todoist-api-key", "-a", "cxj-flow", "-w"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode or not out.stdout.strip():
        return None
    q = urllib.parse.quote(f"#Inbox & !@{cfg['todoist']['done_label']}")
    req = urllib.request.Request(f"https://api.todoist.com/api/v1/tasks/filter?query={q}&limit=50",
                                 headers={"Authorization": f"Bearer {out.stdout.strip()}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)
    return res.get("results", []) if isinstance(res, dict) else res


def sorter_due(cfg, state, inbox=todoist_inbox):
    """Sorter-once: start it only when the Inbox has a task it hasn't sorted, at most every triage_every_minutes
    (so a task it can't sort never costs a run per tick). No Todoist key: the timer alone, as before."""
    if time.time() - state.get("last_triage", 0) < cfg["todoist"]["triage_every_minutes"] * 60:
        return False
    tasks = inbox(cfg)
    state["sorter_mode"] = "timer (no Todoist key)" if tasks is None else "new tasks only"
    return tasks is None or bool(unsorted(tasks, cfg["todoist"]["done_label"]))


def gates(cfg, found, state, dry):
    """Gate agents (agents.py): plan on `plan`/`approved`, the Todoist sorter when the Inbox has something new."""
    todo = {"plan": found["plan"]}
    if cfg["tools"].get("todoist"):
        with isolated(state, "sorter", "inbox"):
            if sorter_due(cfg, state):
                todo["triage"] = ["inbox"]
                if not dry:
                    state["last_triage"] = time.time()
    for name, ids in todo.items():
        if not ids:
            continue
        if dry:
            log(f"DRY RUN would start {name} agent for {', '.join(ids)}")
            continue
        with isolated(state, f"{name} agent", ", ".join(ids)):
            if (pid := agents.spawn(cfg, name, ids, state, alive)):
                log(f"{name} agent: started pid {pid} for {', '.join(ids)}")


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


def start(cfg, body, issue, ws, attempt):
    logf = LOGS / f"{issue['identifier']}-{int(time.time())}.log"
    with open(logf, "w") as out:
        p = subprocess.Popen(["claude", "-p", render(body, cfg, issue, ws, attempt), "--model", cfg["models"]["builder"],
                              "--permission-mode", "auto", "--output-format", "stream-json", "--verbose"],
                             cwd=ws, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    return p.pid, logf


def dispatch(cfg, body, state, found, dry):
    slots = cfg["agent"]["max_concurrent"] - len(state["running"])
    for issue in found["issues"]:
        if slots <= 0:
            break
        ident = issue["identifier"]
        with isolated(state, "start", ident):  # one broken workspace never stops the next issue
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
            pid, logf = start(cfg, body, issue, ws, attempt)
            state["running"][ident] = {"pid": pid, "log": str(logf), "attempt": attempt, "started": time.time(),
                                       "title": issue["title"]}
            log(f"{ident}: started pid {pid} attempt {attempt} in {ws}")
            slots -= 1


# ---------- the janitor: board structure, judged by an agent, carried out by code ----------

def janitor_on(cfg):
    v = cfg.get("janitor", False)  # YAML reads a bare on/off as true/false
    return v is True or str(v).lower() == "on"


def tidy(b, terminal):
    """The janitor sees open issues only (a Duplicate-state issue is still in Linear's 'open' list)."""
    for p in b["projects"]:
        p["issues"] = [i for i in p["issues"] if i["status"] not in terminal]
    b["no_project"] = [i for i in b["no_project"] if i["status"] not in terminal]
    return b


def janitor(cfg, state, dry, judge=None, lin=linear):
    """Once a day, and only when the board changed: the janitor judges structure on a snapshot linear.py read.
    Code carries out only the reversible moves (an issue to another project, a link between two issues), each with a
    "[Janitor]" comment saying why, and never the same one twice (so undoing one by hand sticks). Merging projects and
    marking duplicates are only proposed, in the daily view. Off unless WORKFLOW.md says `janitor: on`."""
    if not janitor_on(cfg) or time.time() - state.get("last_janitor", 0) < 86400:
        return
    b = tidy(lin.board(cfg), cfg["tracker"]["terminal_states"])
    j = state.setdefault("janitor", {})
    key = hashlib.sha256(json.dumps(b, sort_keys=True).encode()).hexdigest()[:16]
    if j.get("key") == key:
        state["last_janitor"] = time.time()
        return  # nothing on the board changed: no model call
    if dry:
        return log("DRY RUN would run the janitor")
    state["last_janitor"] = time.time()  # a failed run waits for tomorrow, it doesn't rerun every tick
    acts = (judge or agents.judge)(cfg, b)
    projects = {p["name"]: p for p in b["projects"]}
    where = {i["id"]: p["name"] for p in b["projects"] for i in p["issues"]}
    where.update({i["id"]: "no project" for i in b["no_project"]})
    done, made, proposals = j.setdefault("done", []), [], []
    for a in acts:
        with isolated(state, "janitor", a.get("do")):
            why = a.get("why", "").strip()
            if a["do"] == "move":
                ident, to = a.get("issue"), a.get("to")
                sig = f"move {ident} {to}"
                if sig in done or ident not in where or to not in projects or where[ident] == to:
                    continue
                lin.move(ident, projects[to]["id"])
                lin.comment(ident, f"[Janitor] moved from \"{where[ident]}\" to \"{to}\": {why}")
                made.append(f"{ident} moved to \"{to}\": {why}")
            elif a["do"] == "link":
                pair = sorted(set(a.get("issues") or []))
                sig = "link " + " ".join(pair)
                if sig in done or len(pair) != 2 or not all(x in where for x in pair):
                    continue
                lin.relate(*pair)
                for x, y in (pair, pair[::-1]):
                    lin.comment(x, f"[Janitor] linked to {y}: {why}")
                made.append(f"{pair[0]} ↔ {pair[1]} linked: {why}")
            elif a["do"] == "merge":
                ps = a.get("projects") or []
                if len(ps) == 2 and all(p in projects for p in ps):
                    into = a.get("into") if a.get("into") in ps else ps[1]
                    other = ps[0] if into == ps[1] else ps[1]
                    proposals.append(f"merge project \"{other}\" into \"{into}\": {why}")
                continue
            elif a["do"] == "duplicate":
                if a.get("issue") in where and a.get("of") in where:
                    proposals.append(f"{a['issue']} duplicates {a['of']} (mark it Duplicate): {why}")
                continue
            else:
                continue
            done.append(sig)
            log(f"janitor: {made[-1]}")
    del done[:-500]
    j.update(key=key, at=time.time(), made=made, proposals=proposals)


# ---------- housekeeping ----------

def prune(cfg, state, dry, run=sh, lookup=None):
    """Once a day: remove workspaces whose issue is Done/Canceled (never one still running), and agent logs over 14 days."""
    if time.time() - state.get("last_prune", 0) < 86400:
        return
    root = FLOW / "workspaces"
    names = [d.name for d in root.iterdir() if d.is_dir() and re.fullmatch(r"[A-Z]+-\d+", d.name)] if root.exists() else []
    found = (lookup or linear.states)(names) if names else {}
    for name in names:
        with isolated(state, "prune", name):
            if name in state["running"] or found.get(name) not in cfg["tracker"]["terminal_states"]:
                continue
            ws = root / name
            if dry:
                log(f"DRY RUN would remove workspace {name} ({found[name]})")
                continue
            rc, common = run(["git", "-C", str(ws), "rev-parse", "--path-format=absolute", "--git-common-dir"])
            rc = rc or run(["git", "-C", str(Path(common).parent), "worktree", "remove", "--force", str(ws)])[0]
            if rc:
                raise RuntimeError(f"git worktree remove failed for {ws}")
            log(f"prune: removed workspace {name} ({found[name]})")
    cutoff = time.time() - 14 * 86400
    for f in LOGS.glob("*") if LOGS.exists() else []:
        with isolated(state, "prune log", f.name):
            if f.is_file() and f.name not in KEEP_LOGS and f.stat().st_mtime < cutoff and not dry:
                f.unlink()
    if not dry:
        state["last_prune"] = time.time()


def heartbeat(cfg, state):
    """Tell the watcher on another machine (n8n on the VPS) this tick ran. It pings once if the beats stop, or if
    linear_ok stays false. Never raises."""
    url = (cfg.get("heartbeat") or {}).get("url")
    if not url or DRY:
        return
    day_ago = time.time() - 86400
    body = {"linear_ok": state.get("linear_ok", True), "at": int(time.time()),
            "errors_24h": sum(e["at"] > day_ago for e in state.get("errors", []))}
    try:
        req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:
        log(f"heartbeat failed: {e!r}")


def tick(cfg, body, state, dry):
    reconcile(cfg, state)
    apply_merged(cfg, state, dry)
    try:
        with isolated(state, "linear", "read"):
            found = find_work(cfg, set(state["running"]))
            if state.get("linear_ok") is False:
                log("Linear readable again")
            state["linear_ok"] = True
            ship(cfg, state, found["ship"], dry)
            gates(cfg, found, state, dry)
            dispatch(cfg, body, state, found, dry)
        with isolated(state, "janitor"):
            janitor(cfg, state, dry)
    except linear.AuthError as e:
        log(f"can't read Linear: {e}")
        if state.get("linear_ok", True):
            notify(f"Flow can't read Linear, so it builds and ships nothing until the key works again: {e}", "Flow: outage")
        state["linear_ok"] = False
    with isolated(state, "clash"):
        clash_check(cfg, state, dry)
    with isolated(state, "prune"):
        prune(cfg, state, dry)


def main():
    if "--self-check" in sys.argv:
        import selfcheck
        return selfcheck.run()
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
            for d in {CODE, FLOW}:  # the code, and your own files if they live in another repo; never rebases local work
                subprocess.run(["git", "-C", d, "pull", "-q", "--ff-only"], capture_output=True, timeout=120)
            cfg, body = load_workflow()
        state = json.loads(STATE.read_text()) if STATE.exists() else {"running": {}, "retries": {}}
        try:  # save even when a step fails, or the same thing is redone next tick
            tick(cfg, body, state, dry)
        finally:
            heartbeat(cfg, state)
            if not dry:
                STATE.write_text(json.dumps(state, indent=1))


if __name__ == "__main__":
    main()
