#!/bin/sh
# Entry point for the T12 one-round review orchestrator. All logic lives
# in review/run.py so it sits under the Python test and coverage gates;
# this wrapper only resolves the jeltz checkout so `review.run` imports
# from any cwd. Exit codes: 0 accepted, 10 requires changes, 20 escalate
# to a human, 1 operational failure, 2 usage error.
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PYTHONPATH="${root}${PYTHONPATH:+:${PYTHONPATH}}" \
    exec python3 -c 'import sys; from review.run import main; sys.exit(main())' "$@"
