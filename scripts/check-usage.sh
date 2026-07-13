#!/usr/bin/env bash
# Launcher: locate a WORKING Python 3 interpreter and run check-usage.py with the same arguments.
# The logic lives in check-usage.py; this wrapper only exists so the documented Unix command
# (check-usage.sh) keeps working. On Windows use check-usage.cmd instead.
#
# Each candidate is validated with `-c 'import sys'` before use, so under Git Bash on Windows the
# Microsoft Store "python3" app-execution-alias stub (which just prints an install message and
# exits nonzero) is skipped rather than exec'd.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
SCRIPT="$DIR/check-usage.py"

for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c "import sys" >/dev/null 2>&1; then
    exec "$py" "$SCRIPT" "$@"
  fi
done

# No usable interpreter: still honor the "JSON on every exit path" contract.
echo "usage-governor: no working python3/python interpreter found on PATH" >&2
echo '{"available": false, "reason": "no working python interpreter found on PATH"}'
exit 2
