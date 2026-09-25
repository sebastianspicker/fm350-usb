#!/bin/sh
# Runs the helper's own tests under the real target interpreter: the system
# /usr/bin/python3 (3.9.6), isolated (-I -S) exactly like the installed
# LaunchDaemon. See helper_unittest_py39.py (stdlib unittest only -- no
# pytest, since -I/-S give it no access to the project's venv/site-packages).
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON3=/usr/bin/python3

if [ ! -x "$PYTHON3" ]; then
    echo "run_helper_tests_py39.sh: $PYTHON3 not found or not executable" >&2
    exit 1
fi

echo "-> py_compile under $PYTHON3 -I -S"
"$PYTHON3" -I -S -m py_compile "$HERE/../src/fm350mac/helper/fm350mac_helper.py"

echo "-> unittest under $PYTHON3 -I -S"
"$PYTHON3" -I -S "$HERE/helper_unittest_py39.py" "$@"
