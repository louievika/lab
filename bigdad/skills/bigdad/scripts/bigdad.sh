#!/bin/sh
# One command on every OS: sh <skill folder>/scripts/bigdad.sh <cmd> [args]
# Finds a real Python 3.8+ and runs bigdad.py with it. Same choice rules as hooks/run.sh:
# BIGDAD_PYTHON=<path> wins; then the remembered choice; then Windows tries python, py -3, python3
# (python3 there is often the slow Microsoft Store stub) and every other OS tries python3, python, py -3.
export PYTHONUTF8=1
# this script's folder, whether the path uses / or \ (Windows can pass either)
bs='\'
dir=.
for d in "${0%/*}" "${0%"$bs"*}"; do
  if [ "$d" != "$0" ] && { [ "$dir" = . ] || [ ${#d} -gt ${#dir} ]; }; then dir=$d; fi
done
script="$dir/bigdad.py"
ok='import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'
if [ -n "${BIGDAD_PYTHON:-}" ] && "$BIGDAD_PYTHON" -c "$ok" >/dev/null 2>&1; then
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
  *) case "$(uname -s 2>/dev/null)" in
       MINGW*|MSYS*|CYGWIN*) order='python|py -3|python3' ;;
       *) order='python3|python|py -3' ;;
     esac ;;
esac
IFS='|'
for py in $order; do
  IFS=' '
  if $py -c "$ok" >/dev/null 2>&1; then
    mkdir -p "${cache%/*}" 2>/dev/null && printf '%s\n' "$py" > "$cache" 2>/dev/null
    exec $py "$script" "$@"
  fi
  IFS='|'
done
IFS=' '
echo "Big Dad: no Python 3.8+ found (tried python3, python, py -3). Install Python 3.8+ or set BIGDAD_PYTHON=<path>." >&2
exit 1
