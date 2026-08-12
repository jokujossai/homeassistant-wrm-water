#!/bin/sh
# Run the test suite. The pure-logic tests always run; the Home Assistant
# integration tests (tests/integration/) run when pytest-homeassistant-
# custom-component is importable and are skipped otherwise.
#
# For the full suite: python3 -m venv .venv &&
#   .venv/bin/pip install -r requirements_test.txt
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -m pytest "$@"
fi
exec python3 -m pytest "$@"
