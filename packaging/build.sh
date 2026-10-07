#!/bin/sh
# Build the single-folder executable for this machine's OS (Linux or macOS).
#
#   packaging/build.sh [--with-engine]
#
#   --with-engine   copy the Needle engine library from this machine's cache into the build,
#                   so the first start needs no download of it (run `needle fetch` first)
#
# Nothing is installed into the repo's .venv: PyInstaller runs from a throwaway uv
# environment that only *reads* the repo's site-packages.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DESKTOP=$(dirname "$HERE")
ROOT=$DESKTOP
PY=${FN_PYTHON:-$ROOT/.venv/bin/python}
WITH_ENGINE=0
for arg in "$@"; do
    case $arg in
        --with-engine) WITH_ENGINE=1 ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done
[ -x "$PY" ] || { echo "no Python at $PY (set FN_PYTHON)" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "uv is needed to run PyInstaller without installing it into .venv" >&2; exit 1; }

VERSION=$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
SITE=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
cd "$DESKTOP"
# The build Python is a fresh one of the same minor version; the repo's packages are only on its path.
PYTHONPATH="$SITE:$DESKTOP${PYTHONPATH:+:$PYTHONPATH}" \
    uv run --no-project --python "$VERSION" --with "pyinstaller>=6.6" \
    pyinstaller --noconfirm --clean --distpath "$DESKTOP/dist" --workpath "$DESKTOP/build" \
    "$HERE/fusion_needle.spec"

OUT="$DESKTOP/dist/FusionNeedle"
if [ "$WITH_ENGINE" = 1 ]; then
    ENGINE=$("$PY" -c 'from needle.agent import fetch; import os; print(os.path.join(fetch.cache_dir(3), fetch.lib_name(3)))')
    [ -f "$ENGINE" ] || { echo "no engine at $ENGINE: run \`needle fetch\` first" >&2; exit 1; }
    name=$(basename "$ENGINE")
    # needle looks for libneedle3.<ext> inside its own package folder before the cache
    cp "$ENGINE" "$OUT/_internal/needle/${name%.*}3.${name##*.}"
    echo "engine copied into the build"
fi
echo "built $OUT"
