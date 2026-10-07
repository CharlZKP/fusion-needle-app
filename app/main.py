"""Start the app: model server, local UI server, then a window on it."""
from __future__ import annotations

import argparse
import atexit
import os
import signal
import sys
import threading
import webbrowser

from . import APP_NAME, __version__, paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fusion-needle", description=f"{APP_NAME}: drive Autodesk Fusion step "
                                     "by step with a Needle 3 fine-tune.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--port", type=int, default=0, help="port of the local UI (default: a free one)")
    parser.add_argument("--window", choices=["auto", "native", "browser", "none"], default=None,
                        help="auto: pywebview when installed, else the default browser; none: only serve")
    parser.add_argument("--headless", action="store_true", help="same as --window none")
    parser.add_argument("--config-dir", default=None, help="settings folder (default: per-user config dir)")
    parser.add_argument("--project", default=None, help="project under projects/ (this launch only)")
    parser.add_argument("--fusion-url", default=None, help="Fusion MCP URL (this launch only)")
    parser.add_argument("--weights", default=None, help="'auto' (the published model), 'base' or a .cact path (this launch only)")
    parser.add_argument("--model-url", default=None, help="use a step server that is already running")
    parser.add_argument("--model-token", default=None, help="its STEPSERVER_TOKEN (or $STEPSERVER_TOKEN)")
    parser.add_argument("--no-model", action="store_true", help="do not start or connect a model (UI only)")
    return parser


def _apply_overrides(settings, args):
    """Command-line values apply to this launch and are not written to the settings file."""
    data = settings.data
    if args.project:
        data["project"] = args.project
    if args.fusion_url:
        data["fusion_url"] = args.fusion_url
    if args.weights:
        data["model"]["mode"], data["model"]["weights"] = "spawn", args.weights
    if args.model_url:
        data["model"]["mode"], data["model"]["url"] = "url", args.model_url
        data["model"]["token"] = args.model_token or os.environ.get("STEPSERVER_TOKEN", "")


def _console_close_handler(app):
    """Windows: closing the console window gives no Python signal; write the pending row first."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)
        def handler(_event):
            app.shutdown()
            return 0

        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, 1)
        return handler                                   # keep a reference alive
    except Exception:
        return None


def _native_window(url: str) -> bool:
    try:
        import webview                                   # pywebview, optional
    except Exception:
        return False
    try:
        webview.create_window(APP_NAME, url, width=1240, height=860, min_size=(900, 600))
        webview.start()
        return True
    except Exception as failure:
        print(f"{APP_NAME}: no native window ({failure}); using the browser", file=sys.stderr, flush=True)
        return False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config_dir:
        os.environ["FUSION_NEEDLE_CONFIG_DIR"] = args.config_dir

    from .server import App, serve
    from .settings import Settings

    settings = Settings()
    _apply_overrides(settings, args)
    app = App(settings, start_model=not args.no_model)
    httpd = serve(app, args.port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    app.start()
    atexit.register(app.shutdown)
    keep = _console_close_handler(app)                   # noqa: F841

    def on_signal(_number, _frame):
        app.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass

    print(f"{APP_NAME} {__version__}: {url}", flush=True)
    print(f"  settings {settings.path}\n  log      {app.log.path}", flush=True)
    window = "none" if args.headless else (args.window or settings["window"])
    try:
        shown = False
        if window in ("auto", "native"):
            shown = _native_window(url)                  # blocks until the window is closed
            if shown:
                app.stop_event.set()
            elif window == "native":
                print(f"{APP_NAME}: pywebview is not installed (pip install pywebview); using the browser",
                      file=sys.stderr, flush=True)
        if not shown and window != "none":
            if not webbrowser.open(url):
                print(f"{APP_NAME}: open {url} in a browser", flush=True)
        if not shown:
            if window != "none":
                print("  close with the Quit button in the page, or Ctrl+C here", flush=True)
            while not app.stop_event.wait(0.5):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        threading.Event().wait(0.2)                      # let the HTTP thread finish its last answer
    return 0


if __name__ == "__main__":
    sys.exit(main())
