#!/bin/bash
# CXJ Flow setup. Run in a normal Terminal window on the machine that will run the agents (not over SSH:
# macOS keeps the GitHub and Claude logins in the keychain, which SSH sessions can't read).
#   ./setup.sh            check + create labels + dry run + install the schedule
#   ./setup.sh --stop     remove the schedule
set -e
FLOW="$(cd "$(dirname "$0")" && pwd)"
cd "$FLOW"
LABEL=dev.cxjflow

if [ "$1" = "--stop" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$LABEL-digest" 2>/dev/null || true
  rm -f ~/Library/LaunchAgents/$LABEL.plist ~/Library/LaunchAgents/$LABEL-digest.plist
  echo "CXJ Flow stopped."; exit 0
fi

command -v claude >/dev/null || { echo "STOP: install Claude Code first: https://claude.com/claude-code"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "STOP: GitHub CLI not logged in. Run: gh auth login"; exit 1; }
python3 -c "import yaml" 2>/dev/null || python3 -m pip install -q --user pyyaml
echo "1/5 tools ok (claude, gh, python yaml)"

cfg() { python3 -c "import yaml,sys; d=yaml.safe_load(open('WORKFLOW.md').read().split('---')[1]); print(eval('d'+sys.argv[1]) or '')" "$1"; }
TEAM=$(cfg "['tracker']['team']")
[ -n "$TEAM" ] || { echo "STOP: WORKFLOW.md has no tracker.team. Open Claude Code here and say \"set me up\"."; exit 1; }
grep -q "TODO:" context/COMPANY.md && echo "   note: context/COMPANY.md still has TODOs — agents work better once it's filled in."
echo "2/5 config ok (Linear team $TEAM)"

LINEAR=$(cfg "['tools']['linear']"); TODOIST=$(cfg "['tools']['todoist']")
LABELS=$(cfg "['labels']"); DONE=$(cfg "['todoist']['done_label']")
TOOLS=("$LINEAR"); [ -n "$TODOIST" ] && TOOLS+=("$TODOIST")
claude -p "Using the Linear tools: in team $TEAM, create any of these issue labels that don't exist yet (leave existing ones alone): $LABELS.
$( [ -n "$TODOIST" ] && echo "Using the Todoist tools: create the personal label '$DONE' if it doesn't exist.")
Reply with one line: what you created." --model sonnet --permission-mode dontAsk --allowedTools "${TOOLS[@]}"
echo "3/5 labels ready"

python3 flow/runner.py --dry-run && echo "4/5 dry run ok (lines above = what it would start)"

if [ "$(uname)" != "Darwin" ]; then
  MIN=$(cfg "['polling']['interval_minutes']"); T=$(cfg "['digest']['time']")
  echo "5/5 not a Mac — add these to 'crontab -e':"
  echo "*/$MIN * * * * cd $FLOW && python3 flow/runner.py >> logs/cron.log 2>&1"
  [ -n "$TODOIST" ] && echo "${T#*:} ${T%%:*} * * * cd $FLOW && python3 flow/digest.py >> logs/cron.log 2>&1"
  exit 0
fi

mkdir -p logs ~/Library/LaunchAgents
SECS=$(( $(cfg "['polling']['interval_minutes']") * 60 ))
T=$(cfg "['digest']['time']"); H=$((10#${T%%:*})); M=$((10#${T#*:}))
plist() { # label, script, schedule xml
  cat > ~/Library/LaunchAgents/$1.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>-lc</string><string>cd '$FLOW' &amp;&amp; python3 flow/$2</string></array>
  $3
  <key>StandardOutPath</key><string>$FLOW/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$FLOW/logs/launchd.err</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$1.plist
}
plist $LABEL runner.py "<key>StartInterval</key><integer>$SECS</integer>"
[ -n "$TODOIST" ] && plist $LABEL-digest digest.py \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer></dict>"
echo "5/5 CXJ Flow is on — checks Linear every $((SECS/60)) min$( [ -n "$TODOIST" ] && echo ", digest at $T"). Log: logs/runner.log"
echo "Stop any time: ./setup.sh --stop"
