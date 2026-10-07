"""Entry point of Fusion Needle (launchers and the PyInstaller build start here).

In a frozen build ``sys.executable`` is this program, and both stepserver and needle
start their workers with ``sys.executable -m <module>`` or
``sys.executable <file.py> --child``. Those two forms are served here so the
one executable can also be the model server and its engine workers.
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _dispatch_child() -> bool:
    argv = sys.argv
    if len(argv) >= 3 and argv[1] == "-m":
        module = argv[2]
        sys.argv = [module] + argv[3:]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return True
    if len(argv) >= 2 and argv[1].lower().endswith(".py") and os.path.isfile(argv[1]):
        script = argv[1]
        sys.argv = [script] + argv[2:]
        runpy.run_path(script, run_name="__main__")
        return True
    return False


def main() -> int:
    if getattr(sys, "frozen", False):
        import multiprocessing

        multiprocessing.freeze_support()
        if _dispatch_child():
            return 0
    elif HERE not in sys.path:
        sys.path.insert(0, HERE)
    if sys.version_info < (3, 12):
        sys.stderr.write("Fusion Needle needs Python 3.12 or newer (this is %d.%d).\n" % sys.version_info[:2])
        return 1
    from app.main import main as run

    return run()


if __name__ == "__main__":
    sys.exit(main())
