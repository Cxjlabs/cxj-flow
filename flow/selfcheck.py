"""CXJ Flow tests, run by `python3 flow/runner.py --self-check`. Plain asserts, fakes for gh/git/Linear; touches nothing live."""
import json, os, tempfile, time
from pathlib import Path

import digest, linear, runner
from runner import blocked, buildable, labelled

HERE = Path(__file__).parent
CFG = {"tracker": {"team": "CXJ", "active_states": ["Todo"], "handoff_state": "In Review", "terminal_states": ["Done", "Canceled"],
                   "excluded_title_prefixes": ["You:"]},
       "labels": {"build": "agent", "plan": "plan", "approved": "approved", "go": "go", "idea": "Idea", "triage": "Triage",
                  "decision": "Decision"},
       "agent": {"max_concurrent": 2, "stall_minutes": 30, "max_retries": 3, "retry_backoff_seconds": [60]},
       "todoist": {"done_label": "in-linear", "triage_every_minutes": 15}, "tools": {"linear": "mcp__linear", "todoist": "mcp__todoist"},
       "repos": {"app": "/tmp", "cxj-flow": "/tmp"}, "skip_repos": ["web-app"], "owner": "Sam", "models": {"builder": "opus"}}
# globs like a real WORKFLOW.md, tested on real file lists of two merged PRs (paths renamed)
CLASH = {"default": {"database": ["*.sql", "supabase/migrations/*", "db/migrations/*"], "n8n": ["*n8n*"]},
         "repos": {"o/ops": {"live messaging": ["bot/agent/*"]},
                   "o/web-app": {"live messaging": ["*send*", "*/messages/*", "*channel-actions*", "*crm*"]}}}
PR20 = ["data/cleanup/README.md", "data/cleanup/fix-42/fix-42-fix.sql", "data/cleanup/fix-42/fix-42-rollback.sql",
        "data/cleanup/fix-42/fix-42-test.mjs", "data/cleanup/fix-42/fix-42-verify.sql"]
PR37 = ["DOCS.md", "src/app/(app)/orders/[orderId]/channel-actions.ts", "src/app/api/crm-calendar/slots/route.ts",
        "src/components/orders/order-actions.tsx", "src/lib/orders/record.ts",
        "supabase/migrations/20261022a_order_channel_override.sql", "supabase/migrations/20261022a_order_channel_override_verify.sql"]


def golden():
    fixture = json.loads((HERE / "fixtures/linear-work.json").read_text())
    seen = {}
    real, linear.gql = linear.gql, lambda q, **v: seen.update(v) or fixture
    try:
        got = runner.find_work(CFG, {"CXJ-99"})
    finally:
        linear.gql = real
    assert seen == {"team": "CXJ", "active": ["Todo"], "build": "agent", "handoff": "In Review", "go": "go", "plan": ["plan", "approved"]}, seen
    want = json.loads((HERE / "fixtures/linear-work.golden.json").read_text())
    assert got == want, json.dumps(got, indent=1)
    assert [blocked(i, ["Done"]) for i in got["issues"]][1] == ["CXJ-3"]


def apply_once():
    """merged PR with an `-- apply` file: apply-sql runs once, never twice; unmarked SQL is never applied"""
    calls, saved = [], []
    pr = {"number": 7, "title": "Fix orders (CXJ-12)", "body": "", "headRefName": "fix", "url": "https://x/pull/7",
          "mergeCommit": {"oid": "abc"}, "files": [{"path": "a.sql"}, {"path": "b.sql"}, {"path": "x.py"}]}

    def fake(cmd, cwd=None):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps([pr])
        if cmd[:2] == ["git", "show"]:
            return 0, "-- apply\nupdate t set a = 1" if cmd[2].endswith("a.sql") else "update t set a = 2"
        return 0, "applied 1"
    cfg = {**CFG, "apply_sql": {"command": "apply-sql"}, "repos": {"r": "/tmp"}}
    st = {}
    real, linear.comment = linear.comment, lambda *a: calls.append(["linear", *a])
    try:
        for _ in range(2):
            runner.apply_merged(cfg, st, False, fake, save=lambda: saved.append(dict(st["applied"])))
    finally:
        linear.comment = real
    applies = [c for c in calls if c[0] == "apply-sql"]
    assert len(applies) == 1 and "--once" in applies[0] and applies[0][-2:] == ["--issue", "CXJ-12"], applies
    assert saved and saved[0]["r#7:a.sql"] == "applying", saved  # marked before apply-sql ran
    assert st["applied"] == {"r#7:a.sql": "applied", "r#7:b.sql": "not marked -- apply"}, st
    assert sum(c[:3] == ["gh", "pr", "comment"] for c in calls) == 1 and [c[:2] for c in calls if c[0] == "linear"] == [["linear", "CXJ-12"]]


def isolation():
    """a failing item is logged, listed for the daily view and skipped; the rest of the tick still runs"""
    tmp = Path(tempfile.mkdtemp())
    good = tmp / "good.log"
    good.write_text("[Flow] ready for review: https://x")
    os.utime(good, (time.time() - 7200, time.time() - 7200))
    st = {"running": {"BAD": {"pid": "not-a-pid", "log": str(tmp / "bad.log"), "attempt": 1, "started": time.time() - 7200},
                      "OK": {"pid": 999999, "log": str(good), "attempt": 1, "started": 0},
                      "NOLOG": {"pid": 4242, "log": str(tmp / "never.log"), "attempt": 1, "started": time.time() - 7200}},
          "retries": {}}
    killed = []
    real = runner.alive, runner.os.killpg
    runner.alive = lambda pid: pid == 4242 if isinstance(pid, int) else runner.os.kill(pid, 0)
    def killpg(pid, sig):
        killed.append(pid)
        raise ProcessLookupError  # it exited between the check and the kill (Q1)
    runner.os.killpg = killpg
    try:
        runner.reconcile(CFG, st)
    finally:
        runner.alive, runner.os.killpg = real
    assert killed == [4242], killed  # no log + older than stall_minutes = stalled (Q3), and the kill race didn't crash
    assert "OK" not in st["running"] and "NOLOG" not in st["running"] and "BAD" in st["running"], st["running"]
    assert [e["item"] for e in st["errors"]] == ["BAD"], st["errors"]

    # Q2: a workspace that can't be made never stops the next issue
    started = []
    real = runner.workspace, runner.start
    runner.workspace = lambda issue, d: (_ for _ in ()).throw(RuntimeError("branch already checked out")) if issue["identifier"] == "A" else d
    runner.start = lambda cfg, body, issue, ws, attempt: (started.append(issue["identifier"]) or 1, tmp / "x.log")
    st = {"running": {}, "retries": {}}
    issue = lambda i: {"identifier": i, "title": i, "branch": i, "repo": "app", "blocked_by": []}
    try:
        runner.dispatch(CFG, "", st, {"issues": [issue("A"), issue("B")]}, False)
    finally:
        runner.workspace, runner.start = real
    assert started == ["B"] and list(st["running"]) == ["B"] and st["errors"][0]["item"] == "A", (started, st)


def linear_auth():
    """a lost key pings once per outage and the heartbeat says linear_ok=false"""
    pings, beats = [], []
    st = {"running": {}, "retries": {}}
    real = runner.find_work, runner.notify, runner.clash_check, runner.prune, runner.urllib.request.urlopen, runner.DRY
    runner.find_work = lambda *a: (_ for _ in ()).throw(linear.AuthError("Keychain locked"))
    runner.notify = lambda msg, title=None: pings.append(title)
    runner.clash_check = runner.prune = lambda *a: None
    runner.urllib.request.urlopen = lambda req, timeout: beats.append(json.loads(req.data)) or open(os.devnull)
    runner.DRY = False
    try:
        for _ in range(2):
            runner.tick(CFG, "", st, False)
            runner.heartbeat({"heartbeat": {"url": "https://n8n/webhook/x"}}, st)
    finally:
        runner.find_work, runner.notify, runner.clash_check, runner.prune, runner.urllib.request.urlopen, runner.DRY = real
    assert pings == ["Flow: outage"] and st["linear_ok"] is False, pings
    assert [b["linear_ok"] for b in beats] == [False, False], beats


def clash():
    assert runner.touches(PR20, runner.rules_for(CLASH, "o/ops")) == {"database": PR20[1:2] + PR20[2:3] + PR20[4:5]}
    t37 = runner.touches(PR37, runner.rules_for(CLASH, "o/web-app"))
    assert sorted(t37) == ["database", "live messaging"] and len(t37["live messaging"]) == 2, t37
    assert runner.touches(["README.md", "flow/runner.py"], runner.rules_for(CLASH, "o/cxj-flow")) == {}
    assert runner.checks([]) == "none configured"
    assert runner.checks([{"name": "build", "status": "COMPLETED", "conclusion": "SUCCESS"}]) == "green (1)"
    assert runner.checks([{"name": "build", "status": "IN_PROGRESS", "conclusion": ""}]).startswith("pending")
    assert runner.checks([{"context": "vercel", "state": "FAILURE"}, {"name": "t", "conclusion": "SUCCESS"}]) == "failing: vercel"

    prs = {"o/ops": [{"number": 1, "title": "Fix charges", "url": "https://github.com/o/ops/pull/1", "headRefOid": "a" * 40,
                      "baseRefOid": "b" * 40, "files": [{"path": p} for p in PR20], "statusCheckRollup": [],
                      "createdAt": "2000-01-01T00:00:00Z"}],  # open before Flow started watching: a quiet baseline
           "o/web-app": [{"number": 37, "title": "Closer page", "url": "https://github.com/o/web-app/pull/37",
                          "headRefOid": "c" * 40, "baseRefOid": "d" * 40, "files": [{"path": p} for p in PR37],
                          "statusCheckRollup": [{"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                          "createdAt": "2999-01-01T00:00:00Z"}]}  # opened after: new
    writes, existing, sql = [], {}, {"data/cleanup/fix-42/fix-42-fix.sql": "-- apply\nupdate t set x = 1;"}

    def gh(cmd, cwd=None):
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(prs[cmd[4]])
        if cmd[:3] == ["gh", "api", "-H"]:  # file contents at the head
            return 0, sql.get(cmd[4].split("contents/")[1].split("?")[0], "select 1;")
        if cmd[0] == "apply-sql":
            return 0, "dry run, rolled back:\n  public.t: 3 rows"
        if cmd[:2] == ["gh", "api"] and "--paginate" in cmd:
            return 0, existing.get(cmd[2], "")
        writes.append(cmd)
        return 0, "555"
    cfg = {**CFG, "clash": {**CLASH, "repos": {"o/ops": CLASH["repos"]["o/ops"], "o/web-app": CLASH["repos"]["o/web-app"]}},
           "apply_sql": {"command": "apply-sql"}}
    st, pings = {}, []
    real = runner.notify
    runner.notify = lambda msg, title=None: pings.append(msg)
    try:
        runner.clash_check(cfg, st, False, gh)
        assert len(writes) == 2 and all("-X" not in w for w in writes), writes  # one new comment per PR
        body = writes[0][-3][5:]
        assert body.startswith(runner.MARK) and "Touches:** database" in body and "SQL dry run" in body, body
        assert "**In flight, same files or tables:** none" in body, body  # both touch the database, but share no file or table
        assert "**Checks:** none configured" in body, body
        assert pings == [], pings
        runner.clash_check(cfg, st, False, gh)
        assert len(writes) == 2 and pings == [], writes  # nothing changed: no GitHub write, no ping
        # a new push to web-app#37 changes table t, which ops#1 changes too: a NEW clash, pinged once, by the newer PR
        prs["o/web-app"][0]["headRefOid"] = "e" * 40
        sql["supabase/migrations/20261022a_order_channel_override.sql"] = "alter table public.t add column y int;"
        runner.clash_check(cfg, st, False, gh)
        assert len(pings) == 1 and "web-app#37" in pings[0], pings
        assert "same tables: public.t" in writes[-1][-3], writes[-1]
        runner.clash_check(cfg, st, False, gh)
        assert len(pings) == 1, pings  # the same clash never pings twice
        prs["o/ops"][0]["statusCheckRollup"] = [{"name": "ci", "status": "COMPLETED", "conclusion": "FAILURE"}]
        n = len(writes)
        runner.clash_check(cfg, st, False, gh)
        assert len(writes) == n + 1 and writes[-1][:4] == ["gh", "api", "-X", "PATCH"], writes[-1]  # edited in place
        assert len(pings) == 1, pings
        # state lost: the comment is found by its marker and edited, never duplicated
        existing["repos/o/ops/issues/1/comments"] = "777"
        runner.clash_check(cfg, {}, False, gh)
        assert any(w[:4] == ["gh", "api", "-X", "PATCH"] and w[4].endswith("/comments/777") for w in writes[n + 1:]), writes[n + 1:]
        # merged PR: its cache entries go
        prs["o/ops"] = []
        runner.clash_check(cfg, st, False, gh)
        assert list(st["clash"]) == ["https://github.com/o/web-app/pull/37"], st["clash"]
        assert all(k.startswith("https://github.com/o/web-app/") for k in st["sql_tables"]), st["sql_tables"]
    finally:
        runner.notify = real


# The real false alarm of 24 Oct: two open PRs that both carry .sql files but share no file and change no common table.
# File lists exact (names of people/projects swapped); SQL cut down to statements with the same changed tables.
PR24 = ["data/cleanup/fix-42/2026-10-16-backfill-32-rollback.sql", "data/cleanup/fix-42/2026-10-16-buyers-without-order-60d.csv",
        "data/cleanup/fix-42/fix-42-check.sql", "data/cleanup/fix-42/fix-42-merge.sql",
        "data/cleanup/fix-42/fix-42-nightly-orders-rollback.sql", "data/cleanup/fix-42/fix-42-nightly-orders.sql",
        "data/cleanup/fix-42/fix-42-rollback.sql", "data/cleanup/fix-42/fix-42-rule.sql",
        "data/cleanup/fix-42/fix-42-test.mjs", "data/cleanup/fix-42/fix-42-verify.sql"]
PR31 = ["apps/bot/agent/agent.py", "apps/bot/agent/knowledge.md", "apps/bot/agent/rag.py", "apps/bot/agent/server.py",
        "apps/bot/agent/sql/class-slot-tz.sql", "apps/bot/agent/sql/bot-events.sql", "apps/bot/agent/sql/bot-knowledge.sql",
        "apps/bot/agent/sql/bot-messages-failed.sql", "apps/bot/agent/sql/bot-switch-on-order.sql",
        "apps/bot/agent/sql/bot-templates.sql", "apps/bot/agent/tools.py", "apps/bot/prompt.md"]
SQL24 = {"2026-10-16-backfill-32-rollback.sql": "update orders set rep = null where id in (select id from fix42_before);",
         "fix-42-check.sql": "select count(*) from invoices p join orders b on b.id = p.order_id;",
         "fix-42-merge.sql": "with x as (select 1 as id) update orders b set customer_id = x.id from x;",
         "fix-42-nightly-orders-rollback.sql": "delete from orders where source = 'nightly';",
         "fix-42-nightly-orders.sql": "insert into orders (id) select id from buyers; update public.orders set x = 1 where false;\n"
                                          "insert into audit_log (check_name) values ('nightly');",
         "fix-42-rollback.sql": "delete from audit_log where check_name = 'fix-42'; update customers set a = 1; delete from customer_ids;\n"
                                 "update visit_notes set b = 2; delete from match_queue; update orders set c = 3;",
         "fix-42-rule.sql": "select * from leads c left join lateral (select 1) l on true;",
         "fix-42-verify.sql": "-- update nothing here\nselect exists (select 1 from audit_log);"}
SQL31 = {"class-slot-tz.sql": "update class_slot set tz = 'Europe/London'; insert into audit_log (check_name) values ('slot');",
         "bot-events.sql": "create trigger bot_booked after insert on crm_orders for each row execute function f();\n"
                           "create trigger bot_outcome after update on public.crm_visit_notes for each row execute function f();",
         "bot-knowledge.sql": "create table if not exists bot_knowledge (id int); create temp table _k as select 1;",
         "bot-messages-failed.sql": "alter table bot_messages add column kind text;\n"
                                    "insert into bot_messages (id) values (1) on conflict (id) do update set kind = excluded.kind;",
         "bot-switch-on-order.sql": "create or replace trigger bot_switch after insert on crm_orders for each row execute function g();",
         "bot-templates.sql": "update bot_templates set body = 'Hi -- update orders set x';"}


def clash_false_alarm():
    want24 = ["public.audit_log", "public.customer_ids", "public.customers", "public.match_queue", "public.orders", "public.visit_notes"]
    want31 = ["public.bot_knowledge", "public.bot_messages", "public.bot_templates", "public.class_slot", "public.crm_orders", "public.crm_visit_notes"]
    assert sorted({t for q in SQL24.values() for t in runner.changed_tables(q)}) == want24
    assert sorted({t for q in SQL31.values() for t in runner.changed_tables(q)}) == want31  # inserts, temp tables, `do update set`, strings: not changes
    pr = lambda n, files, when: {"number": n, "title": f"PR {n}", "url": f"https://github.com/o/ops/pull/{n}", "headRefOid": f"{n}" * 20,
                                 "baseRefOid": "b" * 40, "files": [{"path": f} for f in files], "statusCheckRollup": [], "createdAt": when}
    prs = [pr(24, PR24, "2999-01-01T00:00:00Z"), pr(31, PR31, "2999-01-02T00:00:00Z")]  # both new, so only the rule can keep them quiet
    contents = {**SQL24, **SQL31}

    def gh(cmd, cwd=None):
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(prs)
        if cmd[:3] == ["gh", "api", "-H"]:
            return 0, contents.get(cmd[4].split("?")[0].rsplit("/", 1)[1], "")
        return 0, "1"
    cfg = {**CFG, "clash": {"default": CLASH["default"], "repos": {"o/ops": {"live messaging": ["apps/bot/agent/*"]}}}}
    st, pings = {}, []
    real = runner.notify
    runner.notify = lambda msg, title=None: pings.append(msg)
    try:
        runner.clash_check(cfg, st, False, gh)
    finally:
        runner.notify = real
    lines = sorted(c["line"] for c in st["clash"].values())
    assert pings == [] and all("0 in flight nearby" in l for l in lines), (pings, lines)
    assert "database, live messaging" in lines[1] and "— database;" in lines[0], lines  # the Touches lines are unchanged


def clash_pings():
    """A, B open before Flow watched; C and D new. All change table t. Pings: C once, D once; never A or B; never again."""
    mk = lambda n, when, head="h": {"number": n, "title": f"PR {n}", "url": f"https://github.com/o/ops/pull/{n}", "headRefOid": head * 40,
                                    "baseRefOid": "b" * 40, "files": [{"path": f"m{n}.sql"}], "statusCheckRollup": [], "createdAt": when}
    prs = [mk(1, "2000-01-01T00:00:00Z"), mk(2, "2001-01-01T00:00:00Z")]

    def gh(cmd, cwd=None):
        if cmd[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(prs)
        if cmd[:3] == ["gh", "api", "-H"]:
            return 0, "update t set x = 1;"
        return 0, "1"
    cfg = {**CFG, "clash": {"default": CLASH["default"], "repos": {"o/ops": {}}}}
    st, pings = {}, []
    real = runner.notify
    runner.notify = lambda msg, title=None: pings.append(msg.split("\n")[1])
    try:
        runner.clash_check(cfg, st, False, gh)
        assert pings == [], pings  # A and B clash, but both were open already: a quiet baseline
        prs.append(mk(3, "2999-01-01T00:00:00Z"))
        runner.clash_check(cfg, st, False, gh)
        assert pings == ["https://github.com/o/ops/pull/3"], pings
        prs.append(mk(4, "3000-01-01T00:00:00Z"))
        runner.clash_check(cfg, st, False, gh)
        assert pings == ["https://github.com/o/ops/pull/3", "https://github.com/o/ops/pull/4"], pings  # C doesn't ping again for D
        prs[2] = {**mk(3, "2999-01-01T00:00:00Z", head="n"), "statusCheckRollup": [{"name": "ci", "conclusion": "FAILURE"}]}
        runner.clash_check(cfg, st, False, gh)
        assert len(pings) == 2, pings  # a new push and red CI change the comment, but it is the same clash: no ping
    finally:
        runner.notify = real


def shipper():
    calls, notes = [], []

    class Lin:
        def __init__(self, i): self.i = i
        def issue(self, ident): return self.i
        def comment(self, ident, body): notes.append(body)
        def remove_label(self, i, name): notes.append(f"-{name}")
        def set_state(self, i, name): notes.append(f"={name}")
    base = {"status": "In Review", "labels": {"go": "L1"}, "prs": []}
    runner.ship(CFG, {}, ["CXJ-1"], False, lin=Lin(base))
    assert notes == ["[Ship] closed — nothing to merge", "-go", "=Done"], notes

    def gh(pr):
        def run(cmd, cwd=None):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "view"]:
                return 0, json.dumps(pr)
            if cmd[:3] == ["gh", "pr", "list"]:
                return 0, json.dumps([{"number": 9}])
            return 0, ""
        return run
    pr = {"state": "OPEN", "mergeable": "MERGEABLE", "headRefName": "f", "baseRefName": "main", "url": "https://github.com/o/ops/pull/5",
          "title": "Fix", "statusCheckRollup": [{"name": "ci", "conclusion": "FAILURE"}]}
    notes.clear()
    runner.ship(CFG, {}, ["CXJ-1"], False, run=gh(pr), lin=Lin({**base, "prs": [pr["url"]]}))
    assert notes == ["[Ship] can't merge: checks failing: ci", "-go"] and not any(c[:3] == ["gh", "pr", "merge"] for c in calls), notes
    notes.clear(), calls.clear()
    runner.ship(CFG, {}, ["CXJ-1"], False, run=gh({**pr, "statusCheckRollup": []}), lin=Lin({**base, "prs": [pr["url"]]}))
    assert [c[:3] for c in calls] == [["gh", "pr", "view"], ["gh", "pr", "list"], ["gh", "pr", "edit"], ["gh", "pr", "merge"]], calls
    assert calls[2][-2:] == ["--base", "main"] and notes == [f"[Ship] merged {pr['url']} — Fix", "-go", "=Done"], notes
    notes.clear()
    runner.ship(CFG, {}, ["CXJ-1"], False, run=gh({**pr, "url": "https://github.com/o/web-app/pull/1", "statusCheckRollup": []}),
                lin=Lin({**base, "prs": ["https://github.com/o/web-app/pull/1"]}))
    assert notes == ["[Ship] this repo ships by hand", "-go"], notes
    notes.clear()
    runner.ship(CFG, {}, ["CXJ-1"], False, lin=Lin({**base, "status": "Done"}))
    assert notes == [], notes  # no longer In Review: nothing happens


def prune():
    tmp = Path(tempfile.mkdtemp())
    for n in ("CXJ-1", "CXJ-2", "CXJ-3", "notes"):
        (tmp / "workspaces" / n).mkdir(parents=True)
    (tmp / "logs").mkdir()
    old = time.time() - 15 * 86400
    for n in ("runner.log", "CXJ-1-1.log", "triage-1.log", "fresh.log"):
        (tmp / "logs" / n).write_text("x")
    for n in ("runner.log", "CXJ-1-1.log", "triage-1.log"):
        os.utime(tmp / "logs" / n, (old, old))
    removed = []

    def git(cmd, cwd=None):
        if "rev-parse" in cmd:
            return 0, "/repo/.git"
        removed.append(cmd[-1])
        return 0, ""
    real = runner.FLOW, runner.LOGS
    runner.FLOW, runner.LOGS = tmp, tmp / "logs"
    st = {"running": {"CXJ-2": {}}}
    try:
        runner.prune(CFG, st, False, git, lookup=lambda names: {"CXJ-1": "Done", "CXJ-2": "Canceled", "CXJ-3": "In Progress"})
        runner.prune(CFG, st, False, git, lookup=lambda names: 1 / 0)  # once a day: the second call does nothing
    finally:
        runner.FLOW, runner.LOGS = real
    assert removed == [str(tmp / "workspaces/CXJ-1")], removed  # CXJ-2 is running, CXJ-3 is open: kept
    assert sorted(p.name for p in (tmp / "logs").iterdir()) == ["fresh.log", "runner.log"]


def sorter():
    now = time.time()
    iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(s))
    tasks = [{"content": "tax return", "labels": ["in-linear"], "added_at": iso(now - 9e5)},
             {"content": "Flow — Sat 24 Oct", "labels": [], "added_at": iso(now - 9e5)},
             {"content": "typing still", "labels": [], "added_at": iso(now - 60)},
             {"content": "call the bank", "labels": [], "added_at": iso(now - 3600)}]
    assert [t["content"] for t in runner.unsorted(tasks, "in-linear", now)] == ["call the bank"]
    st = {}
    assert runner.sorter_due(CFG, st, inbox=lambda c: tasks[:2]) is False  # everything sorted: no model run
    assert runner.sorter_due(CFG, st, inbox=lambda c: tasks) is True and st["sorter_mode"] == "new tasks only"
    assert runner.sorter_due(CFG, st, inbox=lambda c: None) is True and st["sorter_mode"].startswith("timer")
    assert runner.sorter_due(CFG, {"last_triage": now}, inbox=lambda c: tasks) is False  # at most every triage_every_minutes


def reports():
    """the reports sorter starts only when a report is new, and a report it already had is never rerun"""
    st, started = {"reports_seen": ["CXJ-1"]}, []
    assert runner.reports_due(CFG, st, read=lambda c: ["CXJ-1"]) == []  # left labelled, unchanged: no model run
    assert runner.reports_due(CFG, st, read=lambda c: ["CXJ-1", "CXJ-2"]) == ["CXJ-1", "CXJ-2"]
    cfg = {**CFG, "reports": {"label": "Reported", "repo": "app"}}
    real = linear.reported, runner.agents.spawn
    linear.reported = lambda c: ["CXJ-1", "CXJ-2"]
    runner.agents.spawn = lambda cfg, name, ids, state, alive: started.append((name, ids)) or 42
    try:
        runner.gates(cfg, {"plan": []}, st, dry=False)
        runner.gates(cfg, {"plan": []}, st, dry=False)
    finally:
        linear.reported, runner.agents.spawn = real
    assert started == [("reports", ["CXJ-1", "CXJ-2"])] and st["reports_seen"] == ["CXJ-1", "CXJ-2"], (started, st)
    assert "/tmp" in runner.agents.prompts(cfg)["reports"]  # it is told where the app's code is


def daily():
    d = {"shipped": {"nodes": [{"identifier": "CXJ-1", "title": "Fix", "project": {"name": "P"}}]},
         "projects": {"nodes": [{"name": "P", "progress": 0.5, "status": {"name": "Planned"}, "description": "x\n[Planner] plan",
                                 "labels": {"nodes": []}}]},
         "awaiting": {"nodes": []}, "blocked": {"nodes": [{"identifier": "CXJ-2", "title": "Q"}]},
         "week": {"nodes": [{"identifier": "CXJ-1", "createdAt": "2020-01-01T00:00:00Z", "completedAt": "2026-10-24T00:00:00Z",
                             "canceledAt": None, "project": {"id": "p"}},
                            {"identifier": "CXJ-3", "createdAt": "2026-10-23T10:00:00Z", "completedAt": "2026-10-23T10:20:00Z",
                             "canceledAt": None, "project": None}]}}
    st = {"clash": {"u": {"line": "ops#1 Fix — database; 1 in flight nearby; checks none configured", "sig": ["x"]}},
          "errors": [{"at": time.time(), "where": "start", "item": "CXJ-5", "error": "RuntimeError('boom')"}],
          "retries": {"CXJ-6": {"attempt": 3, "due": float("inf"), "title": "T"}}}
    text = digest.build(CFG, st, d, now=time.mktime(time.strptime("2026-10-24 09", "%Y-%m-%d %H")))
    for want in ("CXJ-1 Fix (P)", "P: 50%", "project \"P\": plan written", "CXJ-2 Q", "CXJ-6 T", "⚠ ops#1", "start CXJ-5: 1×",
                 "Plan adherence (7d): 1/2", "After-the-fact issues (created→closed < 1h, 7d): 1 (CXJ-3)"):
        assert want in text, (want, text)


def janitor():
    """off = no board read, no model call; only reversible moves are made, merges/duplicates are proposals; never twice"""
    calls = []

    class Lin:
        def board(self, cfg):
            calls.append("board")
            return {"projects": [{"id": "p1", "name": "Checkout fixes", "issues": [{"id": "CXJ-1", "status": "Backlog"},
                                                                                {"id": "CXJ-9", "status": "Duplicate"}]},
                                 {"id": "p2", "name": "Checkout v2", "issues": [{"id": "CXJ-2", "status": "Todo"}]}],
                    "no_project": [{"id": "CXJ-3", "status": "Backlog"}]}
        def move(self, ident, pid): calls.append(("move", ident, pid))
        def relate(self, a, b): calls.append(("relate", a, b))
        def comment(self, ident, body): calls.append(("comment", ident, body))
    acts = [{"do": "move", "issue": "CXJ-3", "to": "Checkout v2", "why": "it is a v2 step"},
            {"do": "move", "issue": "CXJ-1", "to": "Nowhere", "why": "no such project"},
            {"do": "link", "issues": ["CXJ-2", "CXJ-1"], "why": "same bug"},
            {"do": "merge", "projects": ["Checkout fixes", "Checkout v2"], "into": "Checkout v2", "why": "same outcome"},
            {"do": "duplicate", "issue": "CXJ-1", "of": "CXJ-2", "why": "same words"}]
    seen = []
    judge = lambda cfg, b: seen.append(b) or acts
    st, on = {}, {**CFG, "janitor": True, "tracker": {**CFG["tracker"], "terminal_states": ["Done", "Canceled", "Duplicate"]}}
    for cfg in ({**CFG}, {**CFG, "janitor": False}, {**CFG, "janitor": "off"}):
        runner.janitor(cfg, st, False, judge, Lin())
    assert calls == [] and seen == [], calls  # off by default
    runner.janitor(on, st, False, judge, Lin())
    assert [c for c in calls if c != "board" and c[0] != "comment"] == [("move", "CXJ-3", "p2"), ("relate", "CXJ-1", "CXJ-2")], calls
    assert [c[1] for c in calls if c[0] == "comment"] == ["CXJ-3", "CXJ-1", "CXJ-2"] and "[Janitor] moved" in calls[2][2], calls
    assert all(i["status"] != "Duplicate" for p in seen[0]["projects"] for i in p["issues"])  # closed issues never shown
    assert st["janitor"]["proposals"] == ['merge project "Checkout fixes" into "Checkout v2": same outcome',
                                          "CXJ-1 duplicates CXJ-2 (mark it Duplicate): same words"], st["janitor"]
    calls.clear()
    runner.janitor(on, st, False, judge, Lin())
    assert calls == [] and len(seen) == 1  # once a day
    st["last_janitor"] = 0
    runner.janitor(on, st, False, judge, Lin())
    assert calls == ["board"] and len(seen) == 1  # board unchanged: no model call
    st["last_janitor"], st["janitor"]["key"] = 0, "changed"
    calls.clear()
    runner.janitor(on, st, False, judge, Lin())
    assert calls == ["board"] and len(seen) == 2, calls  # the same moves are never made twice (an undo by hand sticks)
    text = digest.build(CFG, st, {"shipped": {"nodes": []}, "projects": {"nodes": []}, "awaiting": {"nodes": []},
                                  "blocked": {"nodes": []}, "week": {"nodes": []}})
    assert 'merge project "Checkout fixes"' in text, text
    import importlib.util  # the replay eval's scoring: expected must all be there, anything else must be allowed
    spec = importlib.util.spec_from_file_location("janitor_eval", HERE.parent / "evals/janitor/run.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)
    case = {"expect": [{"do": "move", "issue": "A", "to": "P"}], "allow": [{"do": "link"}, {"do": "merge", "projects": ["X", "Y"]}]}
    assert ev.score(case, [{"do": "move", "issue": "A", "to": "P"}, {"do": "link", "issues": ["B", "C"]},
                           {"do": "merge", "projects": ["Y", "X"]}])[0]
    assert not ev.score(case, [])[0] and not ev.score(case, acts[:1] + [{"do": "move", "issue": "A", "to": "P"}])[0]
    assert ev.score({"expect": [], "allow": []}, [])[0]
    import agents  # majority vote: kept only when most runs agree, whatever each run's why says
    runs = iter([[acts[0], acts[2]], [{**acts[0], "why": "other words"}], [acts[3]]])
    assert [agents.key(a) for a in agents.judge(CFG, {}, 3, lambda c, b: next(runs))] == [("move", "CXJ-3", "Checkout v2")]
    out = {"do": "move", "issue": "CXJ-1", "to": "Checkout v2", "why": "x"}  # out of a project it also wants merged: dropped
    b = Lin().board(CFG)
    assert [agents.key(a) for a in agents.judge(CFG, b, 1, lambda c, b: [out, acts[3], acts[0]])] == [agents.key(acts[3]), agents.key(acts[0])]



def finish():
    """a project is completed only when it has issues and none open; off = nothing; never the same project twice"""
    calls = []

    class Lin:
        def finished(self, cfg):
            return [{"id": "p1", "name": "Old fixes"}]
        def complete_project(self, pid, body): calls.append((pid, body))
    on, st = {**CFG, "janitor": True}, {}
    runner.finish({**CFG, "janitor": False}, st, False, Lin())
    assert calls == [] and st == {}
    runner.finish(on, st, True, Lin())
    assert calls == [] and "last_finish" not in st  # dry run writes nothing
    runner.finish(on, st, False, Lin())
    assert [c[0] for c in calls] == ["p1"] and "[Janitor] marked Completed" in calls[0][1], calls
    assert st["janitor"]["completed"] == ['"Old fixes"']
    st["last_finish"] = 0
    runner.finish(on, st, False, Lin())
    assert len(calls) == 1 and st["janitor"]["completed"] == []  # reopened by hand: left alone
    # linear.finished: a project with no issues, or one still open, is not finished
    some, none = {"nodes": [{"id": "x"}]}, {"nodes": []}
    page = {"projects": {"pageInfo": {}, "nodes": [{"id": "a", "name": "A", "any": some, "open": none},
                                                   {"id": "b", "name": "B", "any": some, "open": some},
                                                   {"id": "c", "name": "C", "any": none, "open": none}]}}
    real = linear.gql
    linear.gql = lambda q, **v: page
    try:
        assert linear.finished(CFG) == [{"id": "a", "name": "A"}]
    finally:
        linear.gql = real

def run():
    assert labelled([{"identifier": "A", "labels": ["needs-plan"]}, {"identifier": "B", "labels": ["Bug", "plan"]},
                     {"identifier": "C", "labels": []}], ["plan", "approved"]) == ["B"]
    assert blocked({"blocked_by": [{"identifier": "X", "status": "Done"}, {"identifier": "Y", "status": "Todo"}]}, ["Done", "Canceled"]) == ["Y"]
    assert blocked({}, ["Done"]) == []
    todo = {"identifier": "T", "status": "Todo", "labels": ["agent"]}
    assert buildable([todo, {"identifier": "D", "status": "Done", "labels": ["agent"]},
                      {"identifier": "N", "status": "Todo", "labels": ["Bug"]}], ["Todo"], "agent") == [todo]
    assert linear.repo_of("**Repo:** none") == "none" and linear.repo_of("Repo: owner/name\n") == "name"
    real_log = runner.log
    runner.log = lambda msg: None  # the fakes' errors are expected; keep runner.log clean
    try:
        for test in (golden, apply_once, isolation, linear_auth, clash, clash_false_alarm, clash_pings, shipper, prune, sorter,
                     reports, daily, janitor, finish):
            test()
    finally:
        runner.log = real_log
    print("ok")
