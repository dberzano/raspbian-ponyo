#!/bin/bash
source /opt/python/globalvenv/bin/activate
echo "executing script with $(which python) ($(python -V)): $*" >&2
exec python "$@"