"""Model manager: weights choice and an external server."""
import json

import pytest
from .fake_model import FakeModel
from .kit import DESKTOP

from app import model as model_module
from app import paths
from app.model import ModelClient, ModelError, ModelManager, weights_args
from app.settings import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "settings.json")


def test_settings_roundtrip_and_secrets_stay_out_of_the_ui(tmp_path):
    settings = Settings(tmp_path / "cfg" / "settings.json")
    settings.update({"fusion_url": "http://127.0.0.1:1/mcp", "confirm_over_calls": 2.0, "bogus": 1,
                     "model": {"mode": "url", "token": "SECRET-TOKEN", "agents": "many"}})
    again = Settings(tmp_path / "cfg" / "settings.json")
    assert again["fusion_url"] == "http://127.0.0.1:1/mcp" and again["confirm_over_calls"] == 2
    assert again["model"]["token"] == "SECRET-TOKEN" and again["model"]["agents"] == 4
    assert "bogus" not in again.data
    public = again.public()
    assert "token" not in public["model"] and public["model"]["token_set"] is True
    assert "SECRET-TOKEN" not in str(public)


def test_weights_args(tmp_path, monkeypatch):
    assert weights_args("fusion-mcp", "base") == ["--weights", "base"]
    assert weights_args("fusion-mcp", "") == ["--weights", "base"]
    assert weights_args("fusion-mcp", "D:\\models\\tuned.cact") == ["--weights", "D:\\models\\tuned.cact"]
    (tmp_path / "projects" / "p" / "models" / "run-7").mkdir(parents=True)
    monkeypatch.setenv("FUSION_NEEDLE_ROOT", str(tmp_path))
    assert weights_args("p", "run-7") == ["--run-id", "run-7"]
    with pytest.raises(ModelError):
        weights_args("p", "no-such-run")


def test_external_server_with_token(settings, catalogue):
    fake = FakeModel(catalogue, token="s3cret").start()
    try:
        settings.update({"model": {"mode": "url", "url": fake.url, "token": "wrong"}})
        manager = ModelManager(settings)
        manager.start_async().join(20)
        assert manager.state == "error" and "token" in manager.detail
        settings.update({"model": {"token": "s3cret"}})
        manager.restart().join(20)
        assert manager.state == "ready" and len(manager.catalogue) == len(catalogue)
        assert manager.snapshot()["model"] == "fake-20L.cact" and manager.process is None
        manager.stop()
        assert manager.state == "stopped"
    finally:
        fake.stop()


def test_settings_of_an_older_version_still_load(tmp_path):
    """A settings file written when protected model bundles existed: its keys are dropped, nothing fails."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "project": "fusion-mcp", "confirm_over_calls": 2,
        "model": {"mode": "bundle", "bundle_dir": "C:/models/model.bundle", "license_key": "OLD-KEY-123",
                  "license_server": "https://lic.example", "weights": "run-7", "token": "tok"}}), encoding="utf-8")
    settings = Settings(path)
    assert settings.load_error == "" and settings["confirm_over_calls"] == 2
    assert settings["model"]["mode"] == "auto" and settings["model"]["weights"] == "run-7"
    assert not [key for key in settings["model"] if "license" in key or "bundle" in key]
    assert "OLD-KEY-123" not in json.dumps(settings.public()) and settings.public()["model"]["token_set"] is True
    manager = ModelManager(settings)
    assert manager.effective_mode() == "spawn"
    settings.update({"window": "browser"})                  # the next save no longer writes the old keys
    assert "OLD-KEY-123" not in path.read_text(encoding="utf-8") and "bundle" not in path.read_text(encoding="utf-8")
    settings.update({"model": {"mode": "bundle"}})          # an old page or script cannot bring the mode back
    assert settings["model"]["mode"] == "auto"


