"""The step server: a small local JSON API in front of the Needle engine.

    GET  /health      liveness, model and catalogue info
    GET  /v1/tools    the tool catalogue
    POST /v1/step     {"query", "system"?, "tools": [names]?} -> tool calls

The desktop app starts it as a child process (``python -m stepserver.cli serve``).
"""

__version__ = "0.2.0"

# The cactus-needle release this code is written against. A .cact archive is
# tied to the engine version, so this pin moves together with pyproject.toml.
NEEDLE_VERSION = "3.1.1"
