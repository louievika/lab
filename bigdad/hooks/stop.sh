#!/bin/sh
# Gate for Big Dad's opt-in hard stop (PreToolUse, every tool). OFF by default: when it's off this
# exits at once (one sh start, ~13 ms) and Python never starts. On = BIGDAD_AGENT_STOP_AT set, or the
# switch file written by `bigdad.py agent-stop N`.
case "${BIGDAD_AGENT_STOP_AT:-}" in
  ""|0|off|OFF) [ -s "${BIGDAD_HOME:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bigdad}/agent-stop-at" ] || exit 0 ;;
esac
# this script's folder, whether the path uses / or \ (Windows can pass either)
bs='\'
dir=.
for d in "${0%/*}" "${0%"$bs"*}"; do
  if [ "$d" != "$0" ] && { [ "$dir" = . ] || [ ${#d} -gt ${#dir} ]; }; then dir=$d; fi
done
exec sh "$dir/run.sh" agent_stop.py
