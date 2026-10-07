#!/bin/sh
# Fusion Needle launcher for Linux and macOS.
# Finds the repo's .venv, else Python 3.12+, and starts the app.
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ -x "$HERE/FusionNeedle/FusionNeedle" ]; then        # a packaged build next to this file
    exec "$HERE/FusionNeedle/FusionNeedle" "$@"
fi

new_enough() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1
}

PY=""
for candidate in "$HERE/../.venv/bin/python" "$HERE/.venv/bin/python" python3.14 python3.13 python3.12 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && new_enough "$candidate"; then
        PY=$candidate
        break
    fi
done

if [ -z "$PY" ]; then
    cat <<'MSG'

  Fusion Needle needs Python 3.12 or newer, and none was found.

  Install it (python.org, your package manager, or `uv python install 3.12`),
  then set up the project once from the repository folder:

      python3.12 -m venv .venv
      .venv/bin/python -m pip install -e .

  and start this file again.

MSG
    [ -t 0 ] && { printf 'Press Enter to close. '; read -r _; }
    exit 1
fi

if ! "$PY" -c 'import stepserver' >/dev/null 2>&1; then
    cat <<'MSG'

  Note: this Python has no "stepserver" package, so the app cannot start the model itself.
  Set the project up once from the repository folder:

      python3.12 -m venv .venv
      .venv/bin/python -m pip install -e .

  The app starts anyway; in Settings you can point it at a model server that is already running.

MSG
fi

"$PY" "$HERE/fusion_needle.py" "$@"
status=$?
if [ "$status" -ne 0 ] && [ -t 0 ]; then
    printf '\n  Fusion Needle stopped with an error (see above). Press Enter to close. '
    read -r _
fi
exit "$status"
