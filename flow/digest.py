#!/usr/bin/env python3
"""CXJ Flow morning digest: one Todoist task listing everything waiting on the owner. Run daily by launchd."""
import subprocess, sys, time

from runner import FLOW, LOGS, load_workflow


def main():
    cfg, _ = load_workflow()
    lb, owner, team, today = cfg["labels"], cfg["owner"] or "the owner", cfg["tracker"]["team"], time.strftime("%a %d %b")
    prompt = f"""You write {owner}'s CXJ Flow morning digest. Read Linear (team {team}), then create ONE Todoist task. Change nothing in Linear.
Collect, numbering items 1, 2, 3… across all sections:
A. "Plan it?" — open issues labelled `{lb['idea']}` or `{lb['triage']}`, without `{lb['plan']}` / `{lb['approved']}` and without a "[Planner] plan" comment. One line each: the problem, its weight if written, the recommendation.
B. "Approve plan?" — open issues with a "[Planner] plan" comment, sub-issues lacking `{lb['build']}`, and no `{lb['approved']}` label. One line each: goal + number of tasks + weight.
C. "Go?" — issues in {cfg['tracker']['handoff_state']}. One line each: what changes, for whom (from the last "[Flow]" comment or PR title), plus the PR link.
D. "Stuck?" — issues in {cfg['tracker']['running_state']} whose latest comment starts "[Flow] blocked:", and issues in {cfg['tracker']['handoff_state']} whose latest comment starts "[Ship] can't merge". One line each: the question. Tell {owner}: answer in a comment, then move it back to Todo.
Rules: plain English, max ~15 words per line, skip empty sections. If all four are empty, create nothing and reply "nothing today".
Otherwise check Todoist for a task titled "Flow — {today}"; if it exists update its description instead of creating another.
Create (or update) in the Todoist Inbox: title "Flow — {today}: N need your yes", due today, priority 1, description = the numbered sections, ending with:
'In Linear add the label: {lb['plan']} (plan it) · {lb['approved']} (build the plan) · {lb['go']} (ship it). Picked up within {cfg['polling']['interval_minutes']} min.'
Reply with just the task title, or "nothing today"."""
    out = subprocess.run(["claude", "-p", prompt, "--model", cfg["models"]["digest"], "--permission-mode", "dontAsk",
                          "--allowedTools", cfg["tools"]["linear"], cfg["tools"]["todoist"]],
                         capture_output=True, text=True, timeout=900, cwd=FLOW)
    msg = (out.stdout.strip() or out.stderr.strip())[-500:]
    LOGS.mkdir(exist_ok=True)
    with open(LOGS / "digest.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} rc={out.returncode} {msg}\n")
    print(msg)
    sys.exit(out.returncode)


if __name__ == "__main__":
    main()
