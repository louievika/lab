#!/bin/sh
# Run a Big Dad hook with a real Python 3. On Windows, `python3` is often the Microsoft
# Store stub (it fails slowly), so Windows tries `python` first. The working choice is
# remembered (only these three names are ever accepted from the cache). BIGDAD_PYTHON=<path> wins if it
# is a working Python 3.8+. Extra arguments go to the script. Never blocks: with no Python, the
# session-start hook says so once (a systemMessage) and the status line shows it; every other hook is silent.
export PYTHONUTF8=1
# this script's folder, whether the path uses / or \ (Windows can pass either)
bs='\'
dir=.
for d in "${0%/*}" "${0%"$bs"*}"; do
  if [ "$d" != "$0" ] && { [ "$dir" = . ] || [ ${#d} -gt ${#dir} ]; }; then dir=$d; fi
done
name=$1
script="$dir/$name"
[ $# -gt 0 ] && shift
if [ -n "${BIGDAD_PYTHON:-}" ] && "$BIGDAD_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
  exec "$BIGDAD_PYTHON" "$script" "$@"
fi
cache="${BIGDAD_HOME:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bigdad}/python-cmd"
py=""
[ -r "$cache" ] && read -r py < "$cache"
case "$py" in
  python3|python|"py -3")
    if command -v "${py%% *}" >/dev/null 2>&1; then exec $py "$script" "$@"; fi ;;
esac
case "${OSTYPE:-}" in
  msys*|cygwin*|win*) order='python|py -3|python3' ;;
  *) order='python3|python|py -3' ;;
esac
IFS='|'
for py in $order; do
  IFS=' '
  if $py -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    mkdir -p "${cache%/*}" 2>/dev/null && printf '%s\n' "$py" > "$cache" 2>/dev/null
    exec $py "$script" "$@"
  fi
  IFS='|'
done
IFS=' '
nopy="Big Dad's hook could not find Python 3 (tried python3, python, py -3). Install Python 3.8+ or set BIGDAD_PYTHON=<path>; until then Big Dad does nothing."
case "$name" in
  session_start.py) printf '{"systemMessage":"\360\237\245\233 %s"}\n' "$nopy" ;;
  sentinel.py)
    case "${1:-}" in
      statusline) printf '%s\n' "Big Dad: no Python 3 found" ;;
      session-start|"") printf '{"systemMessage":"\360\237\245\233 %s"}\n' "$nopy" ;;
    esac ;;
esac
exit 0
