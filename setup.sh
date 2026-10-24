#!/bin/bash
# CXJ Flow setup. Run in a normal Terminal window on the machine that will run the agents (not over SSH:
# macOS keeps the GitHub and Claude logins in the keychain, which SSH sessions can't read).
#   ./setup.sh            check + create labels + dry run + install the schedule
#   ./setup.sh --stop     remove the schedule
# Keep your WORKFLOW.md + context/ in a private folder/repo instead: FLOW_HOME=~/my-flow ./setup.sh
set -e
FLOW="$(cd "$(dirname "$0")" && pwd)"
CONF="${FLOW_HOME:-$FLOW}"; CONF="${CONF/#\~/$HOME}"
cd "$CONF"
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
security find-generic-password -s linear-api-key -a cxj-flow -w >/dev/null 2>&1 || { echo "STOP: no Linear API key in the Keychain. Make one in Linear (Settings → Security & access) and run: security add-generic-password -s linear-api-key -a cxj-flow -w"; exit 1; }
echo "1/5 tools ok (claude, gh, python yaml, Linear key)"

cfg() { python3 -c "import yaml,sys; d=yaml.safe_load(open('WORKFLOW.md').read().split('---')[1]); print(eval('d'+sys.argv[1]) or '')" "$1"; }
TEAM=$(cfg "['tracker']['team']")
[ -n "$TEAM" ] || { echo "STOP: WORKFLOW.md has no tracker.team. Open Claude Code here and say \"set me up\"."; exit 1; }
grep -q "TODO:" context/COMPANY.md && echo "   note: context/COMPANY.md still has TODOs — agents work better once it's filled in."
echo "2/5 config ok (Linear team $TEAM)"

LINEAR=$(cfg "['tools']['linear']"); TODOIST=$(cfg "['tools']['todoist']")
LABELS=$(cfg "['labels']"); DONE=$(cfg "['todoist']['done_label']")
TOOLS=("$LINEAR"); [ -n "$TODOIST" ] && TOOLS+=("$TODOIST")
claude -p "Using the Linear tools: in team $TEAM, create any of these issue labels that don't exist yet (leave existing ones alone): $LABELS.
Also create the PROJECT labels for plan and approved from that list if they don't exist (same names), so a whole project can be planned.
$( [ -n "$TODOIST" ] && echo "Using the Todoist tools: create the personal label '$DONE' if it doesn't exist.")
Reply with one line: what you created." --model sonnet --permission-mode dontAsk --allowedTools "${TOOLS[@]}"
echo "3/5 labels ready"

FLOW_HOME="$CONF" python3 "$FLOW/flow/runner.py" --dry-run && echo "4/5 dry run ok (lines above = what it would start)"

if [ "$(uname)" != "Darwin" ]; then
  MIN=$(cfg "['polling']['interval_minutes']"); T=$(cfg "['digest']['time']")
  echo "5/5 not a Mac — add these to 'crontab -e':"
  echo "*/$MIN * * * * FLOW_HOME=$CONF python3 $FLOW/flow/runner.py >> $CONF/logs/cron.log 2>&1"
  echo "${T#*:} ${T%%:*} * * * FLOW_HOME=$CONF python3 $FLOW/flow/digest.py >> $CONF/logs/cron.log 2>&1"
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
  <key>ProgramArguments</key><array><string>/bin/zsh</string><string>-lc</string><string>python3 '$FLOW/flow/$2'</string></array>
  <key>EnvironmentVariables</key><dict><key>FLOW_HOME</key><string>$CONF</string></dict>
  $3
  <key>StandardOutPath</key><string>$CONF/logs/launchd.out</string>
  <key>StandardErrorPath</key><string>$CONF/logs/launchd.err</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/$1.plist
}
plist $LABEL runner.py "<key>StartInterval</key><integer>$SECS</integer>"
plist $LABEL-digest digest.py \
  "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer></dict>"
echo "5/5 CXJ Flow is on — checks Linear every $((SECS/60)) min, daily view at $T. Log: logs/runner.log"
echo "Stop any time: ./setup.sh --stop"
