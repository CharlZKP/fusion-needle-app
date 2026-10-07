#!/bin/sh
# Fusion Needle launcher for macOS: double-click it in Finder.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$HERE/start-fusion-needle.sh" "$@"
