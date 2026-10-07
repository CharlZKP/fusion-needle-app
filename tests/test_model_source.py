"""The model source (official / custom Hugging Face model): what holds in every build.

The download itself lives in app/modelstore.py, which only builds that download their model have; the
tests of that part are next to it (test_custom_model.py in those builds).
"""
import json
import zipfile

import pytest

from .kit import DESKTOP  # noqa: F401  (puts  on sys.path and the config dir in a temp folder)

from app import server as server_module
from app.server import App
from app.session import ActionError
from app.settings import DEFAULTS, SECRET_KEYS, Settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    made = App(Settings(tmp_path / "settings.json"), start_model=False)
    yield made
    made.session.close()


def test_switching_to_a_custom_model_needs_the_explicit_confirmation(app):
    before = json.dumps(app.settings.data, sort_keys=True)
    wanted = {"repo": "someone/their-model", "revision": "main", "file": "their-20L.cact"}
    for body in (wanted, {**wanted, "confirm": "yes"}, {**wanted, "confirm": 1}, {**wanted, "confirm": False}):
        with pytest.raises(ActionError, match="confirmation"):
            app.dispatch("model_source_use", body)
    assert json.dumps(app.settings.data, sort_keys=True) == before
    assert app.state()["model"]["source"] == "official"


def test_the_settings_form_cannot_set_the_model_source_or_the_access_token(app):
    app.apply_settings({"confirm_over_calls": 2, "model": {
        "source": "custom", "hf_token": "hf_SECRET", "weights": DEFAULTS["model"]["weights"],
        "custom": {"repo": "someone/their-model", "revision": "main", "file": "x.cact", "bytes": 5, "sha256": ""}}})
    model = app.settings["model"]
    assert app.settings["confirm_over_calls"] == 2                      # the rest of the form is applied
    assert model["source"] == "official" and model["custom"]["repo"] == "" and model["hf_token"] == ""


def test_a_build_without_a_model_store_says_so_and_hides_the_control(app, monkeypatch):
    monkeypatch.setattr(server_module, "modelstore", None)
    state = app.state()
    assert state["app"]["custom_model"] is False and state["model"]["source"] == "official"
    for action, body in (("model_source_check", {"text": "someone/their-model"}), ("model_source_official", {}),
                         ("model_source_use", {"repo": "someone/their-model", "file": "x.cact", "confirm": True})):
        with pytest.raises(ActionError, match="does not download models"):
            app.dispatch(action, body)


def test_the_access_token_is_a_secret(app):
    assert ("model", "hf_token") in SECRET_KEYS
    app.settings.update({"model": {"hf_token": "hf_SECRETSECRET"}})
    public = app.settings.public()["model"]
    assert "hf_token" not in public and public["hf_token_set"] is True
    assert "hf_SECRETSECRET" not in json.dumps(app.state())
    out = app.export_diagnostics()
    with zipfile.ZipFile(out["path"]) as archive:
        for name in archive.namelist():
            assert b"hf_SECRETSECRET" not in archive.read(name), name
        info = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith("info.json"))))
    assert info["engine"]["source"] == "official"                        # the export says which model was in use


def test_a_stored_source_this_version_does_not_know_falls_back_to_official(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": {"source": "somewhere", "custom": "not a dict"}}), encoding="utf-8")
    settings = Settings(path)
    assert settings["model"]["source"] == "official" and settings["model"]["custom"]["repo"] == ""
