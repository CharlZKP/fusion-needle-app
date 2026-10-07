"""Fixtures of the app's tests. Values and helpers are in kit.py (see there for why)."""
import json

import pytest

from .fake_fusion import FakeFusion
from .fake_model import FakeModel
from .kit import ANSWERS, DESKTOP, PROJECT, Port

from app.backend import Backend  # noqa: E402  (kit puts  on sys.path)
from app.guard import Guard, Rules  # noqa: E402
from app.phrases import Examples  # noqa: E402
from app.history import SessionLog  # noqa: E402
from app.mcp import FusionClient  # noqa: E402
from app.session import Session  # noqa: E402


@pytest.fixture(scope="session")
def catalogue():
    with open(PROJECT / "tools" / "catalogue.json", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="session")
def backend():
    return Backend(PROJECT)


@pytest.fixture(scope="session")
def rules():
    return Rules.load(PROJECT)


@pytest.fixture(scope="session")
def examples():
    return Examples.load(DESKTOP / "app" / "ui" / "examples.json")


@pytest.fixture
def fusion():
    fake = FakeFusion().start()
    yield fake
    fake.stop()


@pytest.fixture
def make_session(fusion, backend, catalogue, rules, examples, tmp_path):
    made = []

    def make(answers=None, **settings):
        model = FakeModel(catalogue, ANSWERS if answers is None else answers)
        log = SessionLog(tmp_path / "history")
        options = {"toolset": "catalogue", "confirm_over_calls": 3, "save_history": True}
        options.update(settings)
        session = Session(fusion=FusionClient(fusion.url), model=Port(model), backend=backend, log=log,
                          settings=options, validator=None, export_dir=str(tmp_path / "exports"),
                          synchronous=True, guard=Guard(rules), examples=examples)
        session.fake_model = model
        made.append(session)
        return session

    yield make
    for session in made:
        session.close()
