"""CXJ Flow state layer: Linear read and written over its GraphQL API, no model in between.

The key is read from the macOS Keychain at run time (never stored in this repo):
  security add-generic-password -s linear-api-key -a cxj-flow -w
find_work() returns exactly what the old haiku dispatcher returned, only exact: status and labels come from Linear itself.
"""
import json, re, subprocess, time, urllib.error, urllib.request

API = "https://api.linear.app/graphql"
_key = None


class AuthError(Exception):
    """The Keychain item is missing or locked, or Linear refused the key. The tick pings once and says linear_ok=false."""


def key():
    global _key
    if _key is None:
        out = subprocess.run(["security", "find-generic-password", "-s", "linear-api-key", "-a", "cxj-flow", "-w"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode or not out.stdout.strip():
            raise AuthError(f"Keychain item linear-api-key/cxj-flow unreadable: {out.stderr.strip()[:200]}")
        _key = out.stdout.strip()
    return _key


def gql(query, **variables):
    req = urllib.request.Request(API, json.dumps({"query": query, "variables": variables}).encode(),
                                 {"Authorization": key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code in (401, 403) or "AUTHENTICATION_ERROR" in body:
            raise AuthError(f"Linear refused the key ({e.code}): {body}") from None
        raise RuntimeError(f"Linear HTTP {e.code}: {body}") from None
    if res.get("errors"):
        if any((x.get("extensions") or {}).get("code") == "AUTHENTICATION_ERROR" for x in res["errors"]):
            raise AuthError(f"Linear refused the key: {res['errors'][0].get('message')}")
        raise RuntimeError(f"Linear: {res['errors'][0].get('message')}")
    return res["data"]


def names(conn):
    return [n["name"] for n in (conn or {}).get("nodes") or []]


def page(data, field):
    """One page is enough for these filtered lists; say so loudly if that ever stops being true."""
    if (data[field].get("pageInfo") or {}).get("hasNextPage"):
        raise RuntimeError(f"Linear: more than 100 {field}, find_work needs paging")
    return data[field]["nodes"]


def repo_of(description):
    """`Repo: owner/name` (or **Repo:** [owner/name](url)) in a project description -> 'name', else 'none'."""
    m = re.search(r"Repo:\**\s*\[?(?:[\w.-]+/)?([\w.-]+)", description or "")
    return m.group(1) if m and m.group(1).lower() != "none" else "none"


WORK = """query($team: String!, $active: [String!], $build: String!, $handoff: String!, $go: String!, $plan: [String!]) {
  issues(first: 100, filter: {team: {key: {eq: $team}}, state: {name: {in: $active}}, labels: {some: {name: {eq: $build}}}}) {
    pageInfo { hasNextPage } nodes { identifier title branchName state { name } labels { nodes { name } } project { description }
      inverseRelations { nodes { type issue { identifier state { name } } } } } }
  ship: issues(first: 100, filter: {team: {key: {eq: $team}}, state: {name: {eq: $handoff}}, labels: {some: {name: {eq: $go}}}}) {
    pageInfo { hasNextPage } nodes { identifier state { name } labels { nodes { name } } } }
  plan: issues(first: 100, filter: {team: {key: {eq: $team}}, state: {type: {nin: ["completed", "canceled"]}}, labels: {some: {name: {in: $plan}}}}) {
    pageInfo { hasNextPage } nodes { identifier labels { nodes { name } } } }
  projects(first: 100, filter: {accessibleTeams: {some: {key: {eq: $team}}}, labels: {some: {name: {in: $plan}}}}) {
    pageInfo { hasNextPage } nodes { name labels { nodes { name } } } }
}"""


def read_work(cfg):
    """Raw lists in the old dispatcher's JSON shape (before runner.find_work's exact filters)."""
    t, lb = cfg["tracker"], cfg["labels"]
    d = gql(WORK, team=t["team"], active=t["active_states"], build=lb["build"], handoff=t["handoff_state"], go=lb["go"],
            plan=[lb["plan"], lb["approved"]])
    skip = {lb["idea"], lb["triage"], lb["decision"]}
    issues = []
    for i in page(d, "issues"):
        labels = names(i["labels"])
        if skip & set(labels) or i["title"].startswith(tuple(t["excluded_title_prefixes"])):
            continue
        issues.append({"identifier": i["identifier"], "title": i["title"], "branch": i["branchName"],
                       "status": i["state"]["name"], "labels": labels, "repo": repo_of((i["project"] or {}).get("description")),
                       "blocked_by": [{"identifier": r["issue"]["identifier"], "status": r["issue"]["state"]["name"]}
                                      for r in i["inverseRelations"]["nodes"] if r["type"] == "blocks"]})
    item = lambda n, ident: {"identifier": n[ident], "status": (n.get("state") or {}).get("name", ""), "labels": names(n["labels"])}
    return {"issues": issues, "ship": [item(n, "identifier") for n in page(d, "ship")],
            "plan": [item(n, "identifier") for n in page(d, "plan")],
            "projects": [item(n, "name") for n in page(d, "projects")]}


# ---------- one issue, and the few writes Flow makes ----------

ISSUE = """query($id: String!) { issue(id: $id) { id identifier title state { name type } labels { nodes { id name } }
  team { id states { nodes { id name } } }
  attachments { nodes { url } } comments(first: 50) { nodes { body } } } }"""


def issue(ident):
    i = gql(ISSUE, id=ident)["issue"]
    text = " ".join(c["body"] for c in i["comments"]["nodes"]) + " " + " ".join(a["url"] for a in i["attachments"]["nodes"])
    prs = list(dict.fromkeys(re.findall(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", text)))
    return {"uuid": i["id"], "identifier": i["identifier"], "title": i["title"], "status": i["state"]["name"],
            "labels": {l["name"]: l["id"] for l in i["labels"]["nodes"]},
            "states": {s["name"]: s["id"] for s in i["team"]["states"]["nodes"]}, "prs": prs}


PREFIX = ""  # runner sets it from WORKFLOW.md tracker.comment_prefix, e.g. "[Work Mac] "


def comment(ident, body):
    gql("mutation($id: String!, $b: String!) { commentCreate(input: {issueId: $id, body: $b}) { success } }",
        id=ident, b=PREFIX + body)


def remove_label(i, name):
    if name in i["labels"]:
        gql("mutation($id: String!, $l: String!) { issueRemoveLabel(id: $id, labelId: $l) { success } }", id=i["uuid"], l=i["labels"][name])


def set_state(i, name):
    gql("mutation($id: String!, $s: String!) { issueUpdate(id: $id, input: {stateId: $s}) { success } }", id=i["uuid"], s=i["states"][name])


def states(idents):
    """{identifier: state name} for the given identifiers (missing ones left out)."""
    if not idents:
        return {}
    nums = [int(x.rsplit("-", 1)[1]) for x in idents]
    team = idents[0].rsplit("-", 1)[0]
    d = gql("""query($t: String!, $n: [Float!]) { issues(first: 250, filter: {team: {key: {eq: $t}}, number: {in: $n}}) {
              nodes { identifier state { name } } } }""", t=team, n=nums)
    return {n["identifier"]: n["state"]["name"] for n in d["issues"]["nodes"]}


# ---------- the board, for the janitor ----------

BOARD = """query($team: String!) {
  projects(first: 60, filter: {accessibleTeams: {some: {key: {eq: $team}}}, status: {type: {nin: ["completed", "canceled"]}}}) {
    pageInfo { hasNextPage } nodes { id name description createdAt status { name }
      issues(first: 40, filter: {state: {type: {nin: ["completed", "canceled"]}}}) { nodes { identifier title state { name } } } } }
  loose: issues(first: 100, filter: {team: {key: {eq: $team}}, project: {null: true}, state: {type: {nin: ["completed", "canceled"]}}}) {
    pageInfo { hasNextPage } nodes { identifier title state { name } } }
}"""


def board(cfg):
    """Every open project with its open issues, plus open issues in no project: the facts the janitor judges."""
    d = gql(BOARD, team=cfg["tracker"]["team"])
    issue = lambda i: {"id": i["identifier"], "title": i["title"], "status": i["state"]["name"]}
    return {"projects": [{"id": p["id"], "name": p["name"], "status": p["status"]["name"], "created": p["createdAt"][:10],
                          "description": (p["description"] or "")[:700], "issues": [issue(i) for i in p["issues"]["nodes"]]}
                         for p in page(d, "projects")],
            "no_project": [issue(i) for i in page(d, "loose")]}


def uuid(ident):
    return gql("query($id: String!) { issue(id: $id) { id } }", id=ident)["issue"]["id"]


def move(ident, project_id):
    gql("mutation($id: String!, $p: String!) { issueUpdate(id: $id, input: {projectId: $p}) { success } }", id=uuid(ident), p=project_id)


def relate(a, b):
    gql("""mutation($a: String!, $b: String!) { issueRelationCreate(input: {issueId: $a, relatedIssueId: $b, type: related}) {
        success } }""", a=uuid(a), b=uuid(b))


# ---------- the daily view ----------

DAY = """query($team: String!, $since: DateTimeOrDuration!, $week: DateTimeOrDuration!) {
  shipped: issues(first: 100, filter: {team: {key: {eq: $team}}, completedAt: {gt: $since}}) {
    nodes { identifier title project { name } } }
  week: issues(first: 250, filter: {team: {key: {eq: $team}}, or: [{completedAt: {gt: $week}}, {canceledAt: {gt: $week}}]}) {
    nodes { identifier createdAt completedAt canceledAt project { id } } }
  projects(first: 50, filter: {accessibleTeams: {some: {key: {eq: $team}}}, status: {type: {in: ["planned", "started"]}}}) {
    nodes { name progress status { name } description labels { nodes { name } } } }
  awaiting: issues(first: 50, filter: {team: {key: {eq: $team}}, state: {type: {nin: ["completed", "canceled"]}},
      comments: {some: {body: {contains: "[Planner] plan"}}}}) { nodes { identifier title labels { nodes { name } } } }
  blocked: issues(first: 50, filter: {team: {key: {eq: $team}}, state: {type: {eq: "started"}},
      comments: {some: {body: {contains: "[Flow] blocked"}}}}) { nodes { identifier title } }
}"""


def day(cfg, now=None):
    now = now or time.time()
    iso = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s))
    return gql(DAY, team=cfg["tracker"]["team"], since=iso(now - 86400), week=iso(now - 7 * 86400))
