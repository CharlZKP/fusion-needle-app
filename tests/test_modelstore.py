"""The model download: manifest, resume, checksum, offline use, and the app's first start."""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .fake_model import FakeModel

from app import modelstore
from app.model import ModelClient, ModelManager
from app.settings import Settings

DATA = bytes(range(256)) * 4096                              # 1 MiB
NAME = "demo-20L.cact"


class Repo:
    """A stand-in for huggingface.co: /<owner>/<name>/resolve/<revision>/<file>, with Range support."""

    def __init__(self, data=DATA, sha=None):
        self.requests = []                                   # (path, Range header)
        self.cut_after = None                                # close the connection after this many body bytes, once
        manifest = {"format": 1, "default": NAME,
                    "files": [{"file": NAME, "layers": 20, "bytes": len(data),
                               "sha256": sha or hashlib.sha256(data).hexdigest()}]}
        repo = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                repo.requests.append((self.path, self.headers.get("Range")))
                name = self.path.rsplit("/", 1)[1]
                if not self.path.startswith("/acme/demo/resolve/main/"):
                    self.send_error(404)
                    return
                if name == "manifest.json":
                    body, status, start = json.dumps(manifest).encode(), 200, 0
                elif name == NAME:
                    start = int((self.headers.get("Range") or "bytes=0-")[6:].rstrip("-"))
                    body, status = data[start:], 206 if start else 200
                else:
                    self.send_error(404)
                    return
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if name == NAME and repo.cut_after is not None:
                    body, repo.cut_after = body[:repo.cut_after], None
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    fake = Repo()
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FUSION_NEEDLE_HF_REPO", "acme/demo")
    monkeypatch.setenv("FUSION_NEEDLE_HF_REVISION", "main")
    monkeypatch.setenv("FUSION_NEEDLE_HF_ENDPOINT", fake.url)
    monkeypatch.setattr(modelstore.time, "sleep", lambda _seconds: None)
    yield fake
    fake.stop()


def test_first_use_downloads_and_later_use_is_offline(repo, tmp_path):
    seen = []
    path = modelstore.ensure_model(progress=lambda done, total, name: seen.append((done, total, name)))
    assert path == tmp_path / "config" / "models" / "acme--demo" / NAME
    assert path.read_bytes() == DATA and not path.with_name(NAME + ".part").exists()
    assert seen[-1] == (len(DATA), len(DATA), NAME) and len(seen) > 1
    assert modelstore.local_manifest()["default"] == NAME
    repo.stop()                                              # no network from here on
    before = len(repo.requests)
    assert modelstore.ensure_model() == path and modelstore.ensure_model(verify=True) == path
    assert len(repo.requests) == before


def test_a_broken_download_resumes_with_a_range_request(repo):
    repo.cut_after = 300_000
    notes = []
    path = modelstore.ensure_model(note=notes.append)
    assert path.read_bytes() == DATA
    ranges = [header for target, header in repo.requests if target.endswith(NAME)]
    assert ranges == [None, "bytes=300000-"]
    assert any("interrupted" in note for note in notes)


def test_a_cancelled_download_keeps_the_part_file_and_continues_later(repo):
    def stop_early(done, total, name):
        if done >= 400_000:
            raise modelstore.Cancelled()

    with pytest.raises(modelstore.Cancelled):
        modelstore.ensure_model(progress=stop_early)
    part = modelstore.models_dir() / (NAME + ".part")
    kept = part.stat().st_size
    assert 400_000 <= kept < len(DATA)
    assert modelstore.ensure_model().read_bytes() == DATA
    assert repo.requests[-1] == (f"/acme/demo/resolve/main/{NAME}", f"bytes={kept}-")


def test_a_file_that_does_not_match_the_manifest_is_refused(tmp_path, monkeypatch):
    fake = Repo(sha="0" * 64)
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FUSION_NEEDLE_HF_REPO", "acme/demo")
    monkeypatch.setenv("FUSION_NEEDLE_HF_REVISION", "main")
    monkeypatch.setenv("FUSION_NEEDLE_HF_ENDPOINT", fake.url)
    try:
        with pytest.raises(modelstore.StoreError, match="does not match the manifest"):
            modelstore.ensure_model()
        folder = modelstore.models_dir()
        assert not (folder / NAME).exists() and not (folder / (NAME + ".part")).exists()
        assert modelstore.local_manifest() is None
    finally:
        fake.stop()


def test_manifest_file_names_cannot_be_paths():
    entry = {"file": "../evil.cact", "bytes": 1, "sha256": "0" * 64}
    with pytest.raises(modelstore.StoreError):
        modelstore.check_manifest({"files": [entry]})
    with pytest.raises(modelstore.StoreError):
        modelstore.check_manifest({"files": [{**entry, "file": "model.exe"}]})


def test_an_unset_repository_says_so_without_touching_the_network(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FUSION_NEEDLE_HF_REPO", "OWNER/fusion-needle")
    monkeypatch.setenv("FUSION_NEEDLE_HF_ENDPOINT", "http://127.0.0.1:9")
    with pytest.raises(modelstore.StoreError, match="not set yet"):
        modelstore.ensure_model()


def test_the_app_downloads_the_model_on_first_start_and_shows_progress(repo, catalogue, tmp_path, monkeypatch):
    fake = FakeModel(catalogue).start()
    spawned, details = [], []
    settings = Settings(tmp_path / "settings.json")
    assert settings["model"]["weights"] == "auto" and settings["save_history"] is False

    def spawn(self, weight_args, generation):
        details.append(self.detail)
        spawned.append(weight_args)
        return ModelClient(fake.url)

    monkeypatch.setattr(ModelManager, "_spawn", spawn)
    try:
        manager = ModelManager(settings)
        manager.start_async().join(30)
        assert manager.state == "ready", manager.detail
        expected = tmp_path / "config" / "models" / "acme--demo" / NAME
        assert spawned == [["--weights", str(expected)]] and expected.read_bytes() == DATA
        assert details[0].startswith("downloading the model") and "100%" in details[0]
        manager.stop()
    finally:
        fake.stop()


def test_a_failed_download_is_a_readable_model_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FUSION_NEEDLE_HF_REPO", "acme/demo")
    monkeypatch.setenv("FUSION_NEEDLE_HF_ENDPOINT", "http://127.0.0.1:9")       # nothing listens there
    monkeypatch.setattr(ModelManager, "_spawn", lambda *args: pytest.fail("must not start a server"))
    manager = ModelManager(Settings(tmp_path / "settings.json"))
    manager.start_async().join(30)
    assert manager.state == "error" and "could not be downloaded" in manager.detail and "offline" in manager.detail
