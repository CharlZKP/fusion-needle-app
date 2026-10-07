"""Command line of the step server: ``python -m stepserver.cli serve [options]``."""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from . import NEEDLE_VERSION, __version__
from .engine import BASE, EngineError
from .project import DEFAULT_PROJECT, Project, ProjectError


def resolve_weights(project: Project, spec: str | None, run_id: str | None = None, layers: int | None = None) -> str:
    """'base', a path to a .cact archive, or an archive in projects/<project>/models/<run-id>/."""
    if spec in (None, "") and run_id:
        folder = project.models_dir(run_id)
        found = sorted(folder.glob("*.cact"))
        if not found:
            raise ProjectError(f"no .cact archive in {folder}")

        def depth(path: Path) -> int:
            match = re.search(r"-(\d+)L\.cact$", path.name)
            return int(match.group(1)) if match else 0

        if layers:
            found = [path for path in found if depth(path) == layers]
            if not found:
                raise ProjectError(f"no {layers}-layer archive in {folder}")
        return str(max(found, key=depth).resolve())
    if spec in (None, "", BASE):
        return BASE
    for candidate in (Path(spec), project.root / spec, project.dir / spec):
        if candidate.is_file():
            return str(candidate.resolve())
    raise ProjectError(f"weights not found: {spec}")


def cmd_serve(args) -> int:
    from . import serve

    project = Project(args.project)
    weights = resolve_weights(project, args.weights, args.run_id, args.layers)
    return serve.serve(project, weights, host=args.host, port=args.port, agents=args.agents,
                       cors_origin=args.cors_origin, quiet=args.quiet, warm=not args.no_warm,
                       max_new_tokens=args.max_new_tokens)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stepserver", description="Local JSON API in front of the Needle engine.")
    parser.add_argument("--version", action="version", version=f"stepserver {__version__} (cactus-needle {NEEDLE_VERSION})")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("serve", help="serve /health, /v1/tools and /v1/step")
    p.add_argument("--project", "-p", default=os.environ.get("STEPSERVER_PROJECT") or DEFAULT_PROJECT,
                   help=f"folder under projects/ (default {DEFAULT_PROJECT})")
    p.add_argument("--weights", "-w", default=None, help="a .cact archive, or 'base' for the untuned Needle 3")
    p.add_argument("--run-id", default=None, help="with no --weights: use an archive in projects/<project>/models/<run-id>/")
    p.add_argument("--layers", type=int, default=None, help="depth to pick with --run-id (default: the deepest)")
    p.add_argument("--agents", type=int, default=4,
                   help="agents kept warm, one per distinct tools+system (default 4; each tuned agent is a process)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--cors-origin", default=None, help="send CORS headers for this origin")
    p.add_argument("--max-new-tokens", type=int, default=512, help="token budget of one answer (default 512)")
    p.add_argument("--no-warm", action="store_true", help="skip the warm-up step at start")
    p.add_argument("--quiet", action="store_true", help="no access log")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except (ProjectError, EngineError) as failure:
        print(f"stepserver: error: {failure}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stepserver: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
