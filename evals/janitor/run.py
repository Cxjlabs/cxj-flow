#!/usr/bin/env python3
"""Janitor replay eval: real board cases, each with the verdict the owner signed off, replayed through the janitor's prompt.

Cases live with your company files (they hold real board data): $FLOW_HOME/evals/janitor/cases.json, or a path argument.
A case passes when every `expect` action is in the answer and every answer action is in `expect` or `allow`
(an `allow` entry with only "do" allows any action of that kind). The janitor stays off until this passes.

  python3 evals/janitor/run.py [cases.json] [--runs N]
"""
import json, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "flow"))
import agents, runner


key = agents.key


def score(case, got):
    want = {key(a) for a in case["expect"]}
    kinds = {a["do"] for a in case["allow"] if len(a) == 1}
    ok = want | {key(a) for a in case["allow"] if len(a) > 1}
    keys = {key(a) for a in got}
    missing = want - keys
    extra = [a for a in got if key(a) not in ok and a["do"] not in kinds]
    return not missing and not extra, missing, extra


def main():
    argv, runs = sys.argv[1:], 1
    if "--runs" in argv:
        i = argv.index("--runs")
        runs = int(argv[i + 1])
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("-")]
    path = Path(args[0]) if args else agents.FLOW / "evals/janitor/cases.json"
    cases = json.loads(path.read_text())["cases"]
    cfg, _ = runner.load_workflow()
    terminal = cfg["tracker"]["terminal_states"]
    jobs = [(c, r) for c in cases for r in range(runs)]
    with ThreadPoolExecutor(4) as pool:  # each judge() runs 3 votes in parallel too
        answers = list(pool.map(lambda j: agents.judge(cfg, runner.tidy(json.loads(json.dumps(j[0]["board"])), terminal)), jobs))
    failed = 0
    for (c, r), got in zip(jobs, answers):
        ok, missing, extra = score(c, got)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {c['name']}" + (f" (run {r + 1})" if runs > 1 else ""))
        for m in missing:
            print(f"     missing: {m}")
        for a in extra:
            print(f"     extra:   {key(a)}: {a.get('why', '')}")
        if not ok or "-v" in sys.argv:
            for a in got:
                print(f"     said:    {key(a)}: {a.get('why', '')}")
    print(f"{len(jobs) - failed}/{len(jobs)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
