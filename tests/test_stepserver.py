"""The step server's protocol logic, with a scripted engine instead of the real one."""
import pytest

from .kit import ROOT

from stepserver.cli import build_parser, resolve_weights
from stepserver.project import Project, ProjectError
from stepserver.serve import StepError, StepService


class Engine:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.info = {"needle_version": "test"}

    def alive(self):
        return True

    def step(self, query, system, tools, max_new_tokens=512):
        self.requests.append({"query": query, "system": system, "tools": [tool["name"] for tool in tools]})
        return {"response": self.response, "cache": "miss"}


@pytest.fixture(scope="module")
def project():
    return Project("fusion-mcp", root=ROOT)


def test_project_loads_the_catalogue(project):
    assert len(project.catalogue) >= 33 and project.max_tools == 5
    assert "create_hole" in project.tools_by_name
    with pytest.raises(ProjectError):
        Project("no-such-project", root=ROOT)


def test_step_returns_calls_with_whole_floats_folded(project):
    engine = Engine({"function_calls": [{"name": "create_hole", "arguments": {"diameter": 6.0, "x": 10.5}}],
                     "suppressed_calls": None, "reasoning": "'6' -> diameter"})
    service = StepService(project, engine, "base")
    out = service.step({"query": "6 mm hole", "system": "units: mm", "tools": ["create_hole", "undo"]})
    assert out["calls"] == [{"name": "create_hole", "arguments": {"diameter": 6, "x": 10.5}}]
    assert out["suppressed"] == [] and out["toolset"] == "request" and out["retrieval"] is False
    assert engine.requests == [{"query": "6 mm hole", "system": "units: mm", "tools": ["create_hole", "undo"]}]
    assert service.health()["tools"] == len(project.catalogue) and service.health()["tuned"] is False


def test_step_without_tools_passes_the_whole_catalogue(project):
    engine = Engine({"function_calls": []})
    out = StepService(project, engine, "base").step({"query": "hello"})
    assert out["calls"] == [] and out["toolset"] == "catalogue" and out["retrieval"] is True
    assert len(engine.requests[0]["tools"]) == len(project.catalogue)


@pytest.mark.parametrize("body", [[], {}, {"query": " "}, {"query": "x", "tools": ["no_such_tool"]},
                                  {"query": "x", "tools": []}, {"query": "x", "tools": ["undo", "undo"]},
                                  {"query": "x", "system": 3}])
def test_bad_requests_are_refused(project, body):
    with pytest.raises(StepError) as caught:
        StepService(project, Engine({}), "base").step(body)
    assert caught.value.status == 400


def test_an_engine_error_is_reported_as_an_empty_answer(project):
    engine = Engine({"function_calls": [], "success": False, "error": "tool call truncated"})
    out = StepService(project, engine, "base").step({"query": "x"})
    assert out["calls"] == [] and out["engine_error"] == "tool call truncated"


def test_command_line_and_weights(project, tmp_path):
    args = build_parser().parse_args(["serve", "--weights", "base", "--port", "0", "--no-warm", "--quiet"])
    assert args.project == "fusion-mcp" and args.agents == 4 and args.no_warm
    assert resolve_weights(project, "base") == "base" and resolve_weights(project, None) == "base"
    archive = tmp_path / "tuned-8L.cact"
    archive.write_bytes(b"x")
    assert resolve_weights(project, str(archive)) == str(archive.resolve())
    with pytest.raises(ProjectError):
        resolve_weights(project, str(tmp_path / "missing.cact"))
