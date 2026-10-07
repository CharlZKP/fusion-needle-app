"""A custom model from another Hugging Face repository: what is accepted, what is asked, where it is kept."""
import hashlib
import json
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from .fake_model import FakeModel

from app import modelstore
from app.model import ModelClient, ModelManager
from app.server import App, ModelPort
from app.session import ActionError
from app.settings import Settings

DATA = bytes(range(256)) * 1024                              # 256 KiB
OTHER = b"another model " * 4096
SHA = hashlib.sha256(DATA).hexdigest()
TOKEN = "hf_PRIVATEtoken123"
OFFICIAL = "official-20L.cact"


class Hub:
    """A stand-in for huggingface.co: file downloads, the file listing, a private repository and a redirect."""

    def __init__(self):
        self.requests = []                                   # (path, Authorization header)
        self.elsewhere = []                                  # the same, on the host a download is redirected to
        hub = self
        manifest = {"format": 1, "default": "tuned-20L.cact", "files": [
            {"file": "tuned-20L.cact", "bytes": len(DATA), "sha256": SHA},
            {"file": "tuned-8L.cact", "bytes": len(OTHER), "sha256": hashlib.sha256(OTHER).hexdigest()}]}
        official = {"format": 1, "default": OFFICIAL, "files": [{"file": OFFICIAL, "bytes": len(DATA), "sha256": SHA}]}
        files = {
            "/acme/official/resolve/main/manifest.json": json.dumps(official).encode(),
            "/acme/official/resolve/main/" + OFFICIAL: DATA,
            "/lab/tuned/resolve/main/manifest.json": json.dumps(manifest).encode(),
            "/lab/tuned/resolve/main/tuned-20L.cact": DATA,
            "/lab/tuned/resolve/main/tuned-8L.cact": OTHER,
            "/lab/tuned/resolve/v2/manifest.json": json.dumps(manifest).encode(),
            "/lab/tuned/resolve/v2/tuned-20L.cact": DATA,
            "/lab/plain/resolve/main/only.cact": DATA,
            "/lab/many/resolve/main/a.cact": DATA,
            "/lab/many/resolve/main/b.cact": OTHER,
            "/lab/other-manifest/resolve/main/manifest.json": b'{"name": "something else"}',
            "/lab/other-manifest/resolve/main/only.cact": DATA,
            "/lab/secret/resolve/main/only.cact": DATA,
            "/lab/wrong-size/resolve/main/only.cact": DATA + b"more than listed",
        }
        listings = {
            "/api/models/lab/plain/tree/main": [
                {"type": "file", "path": "README.md", "size": 120},
                {"type": "directory", "path": "old", "size": 0},
                {"type": "file", "path": "old/deep.cact", "size": 5},
                {"type": "file", "path": "only.cact", "size": 130, "lfs": {"oid": "f" * 64, "size": len(DATA)}}],
            "/api/models/lab/many/tree/main": [{"type": "file", "path": "a.cact", "size": len(DATA)},
                                               {"type": "file", "path": "b.cact", "size": len(OTHER)},
                                               {"type": "file", "path": "notes.txt", "size": 3}],
            "/api/models/lab/other-manifest/tree/main": [{"type": "file", "path": "only.cact", "size": len(DATA)}],
            "/api/models/lab/secret/tree/main": [{"type": "file", "path": "only.cact", "size": len(DATA)}],
            "/api/models/lab/wrong-size/tree/main": [{"type": "file", "path": "only.cact", "size": len(DATA)}],
            "/api/models/lab/empty/tree/main": [{"type": "file", "path": "README.md", "size": 120}],
            "/api/models/lab/moved/tree/main": [{"type": "file", "path": "only.cact", "size": len(DATA)}],
        }

        def handler(log, serve_moved):
            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.0"

                def do_GET(self):
                    log.append((self.path, self.headers.get("Authorization")))
                    if serve_moved:
                        body = DATA if self.path == "/cdn/only.cact" else None
                    elif self.path.startswith(("/lab/secret/", "/api/models/lab/secret/")) and \
                            self.headers.get("Authorization") != f"Bearer {TOKEN}":
                        self.send_error(401)
                        return
                    elif self.path == "/lab/moved/resolve/main/only.cact":
                        self.send_response(302)
                        self.send_header("Location", hub.cdn_url + "/cdn/only.cact")
                        self.end_headers()
                        return
                    elif self.path in listings:
                        body = json.dumps(listings[self.path]).encode()
                    else:
                        body = files.get(self.path)
                    if body is None:
                        self.send_error(404)
                        return
                    start = int((self.headers.get("Range") or "bytes=0-")[6:].rstrip("-"))
                    self.send_response(206 if start else 200)
                    self.send_header("Content-Length", str(len(body) - start))
                    self.end_headers()
                    self.wfile.write(body[start:])

                def log_message(self, *_args):
                    pass

            return Handler

        self.servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler(self.requests, False)),
                        ThreadingHTTPServer(("127.0.0.1", 0), handler(self.elsewhere, True))]
        for server in self.servers:
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.servers[0].server_address[1]}"
        self.cdn_url = f"http://localhost:{self.servers[1].server_address[1]}"      # another host name

    def stop(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    fake = Hub()
    monkeypatch.setenv("FUSION_NEEDLE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FUSION_NEEDLE_HF_REPO", "acme/official")
    monkeypatch.setenv("FUSION_NEEDLE_HF_REVISION", "main")
    monkeypatch.setenv("FUSION_NEEDLE_HF_ENDPOINT", fake.url)
    monkeypatch.setattr(modelstore.time, "sleep", lambda _seconds: None)
    yield fake
    fake.stop()


@pytest.fixture
def app(hub, tmp_path):
    made = App(Settings(tmp_path / "settings.json"), start_model=False)
    yield made
    made.session.close()


# ---- what a user may paste -------------------------------------------------------------------

ACCEPTED = [
    ("lab/tuned", ("lab/tuned", "main", "")),
    ("  lab/tuned \n", ("lab/tuned", "main", "")),
    ("Lab-1/my_model.v2", ("Lab-1/my_model.v2", "main", "")),
    ("https://huggingface.co/lab/tuned", ("lab/tuned", "main", "")),
    ("https://huggingface.co/lab/tuned/", ("lab/tuned", "main", "")),
    ("https://huggingface.co/lab/tuned/tree/main", ("lab/tuned", "main", "")),
    ("https://huggingface.co/lab/tuned/tree/v1.2", ("lab/tuned", "v1.2", "")),
    ("https://huggingface.co/lab/tuned/tree/0123abcd0123abcd0123abcd0123abcd0123abcd",
     ("lab/tuned", "0123abcd0123abcd0123abcd0123abcd0123abcd", "")),
    ("https://huggingface.co/lab/tuned/resolve/main/tuned-20L.cact", ("lab/tuned", "main", "tuned-20L.cact")),
    ("https://huggingface.co/lab/tuned/blob/v2/tuned-8L.cact", ("lab/tuned", "v2", "tuned-8L.cact")),
]

REJECTED = [
    "", "   ", None, 7, "tuned", "lab/tuned/extra", "/lab/tuned", "lab/", "lab//tuned", "lab/tuned.", "-lab/tuned",
    "http://huggingface.co/lab/tuned",                                  # not https
    "https://example.com/lab/tuned", "https://huggingface.co.evil.example/lab/tuned",
    "https://evil.example/huggingface.co/lab/tuned", "https://huggingface.co" + "@" + "evil.example/lab/tuned",
    "https://user:pw" + "@" + "huggingface.co/lab/tuned", "https://huggingface.co:444/lab/tuned",
    "ftp://huggingface.co/lab/tuned", "file:///etc/passwd", "//huggingface.co/lab/tuned",
    "huggingface.co/lab/tuned", "www.huggingface.co/lab/tuned", "https://www.huggingface.co/lab/tuned",
    "HTTPS://HUGGINGFACE.CO/lab/tuned",
    "https://huggingface.co/lab/tuned?download=true", "https://huggingface.co/lab/tuned#files",
    "https://huggingface.co/lab/tuned/resolve/main/tuned-20L.cact?download=true",
    "https://huggingface.co/lab/../tuned", "https://huggingface.co/lab/tuned/..",
    "https://huggingface.co/lab/tuned/tree/..", "https://huggingface.co/lab/tuned/resolve/main/../x.cact",
    "https://huggingface.co/lab/tuned/resolve/../../x.cact", "https://huggingface.co/lab/tuned/resolve/main/..cact",
    "https://huggingface.co/lab/tuned/resolve/main/a..b.cact",
    "https://huggingface.co/lab%2Ftuned", "https://huggingface.co/lab/tuned/resolve/main/sub%2Fx.cact",
    "https://huggingface.co/lab/tuned/resolve/main/%2e%2e/x.cact", "lab/tuned%00",
    "https://huggingface.co/lab/tuned/resolve/main/sub/x.cact",         # not at the top of the repository
    "https://huggingface.co/lab/tuned/resolve/main/model.safetensors", "https://huggingface.co/lab/tuned/blob/main/README.md",
    "https://huggingface.co/lab/tuned/resolve/main/x.cact.exe", "https://huggingface.co/lab/tuned/resolve/main/.cact",
    "https://huggingface.co/lab/tuned/tree/refs/pr/1", "https://huggingface.co/lab/tuned/commits/main",
    "https://huggingface.co/lab/tuned/tree", "https://huggingface.co/lab/tuned/resolve/main",
    *(f"https://huggingface.co/{kind}/lab/tuned" for kind in modelstore._OTHER_KINDS), "spaces/tuned",
    "https://huggingface.co/lab", "https://huggingface.co/", "https://huggingface.co",
    "lab\\tuned", "lab/tu ned", "lab/tuned\nother/repo", "lab/tünéd", "lab/" + "x" * 200, "a" * 500,
    "javascript:alert(1)", "<script>/x", "lab/tuned;rm", "lab/tuned'", 'lab/"tuned"', "lab/tuned|x", "$(id)/x",
]


@pytest.mark.parametrize("text, expected", ACCEPTED)
def test_ids_and_links_that_are_accepted(text, expected):
    found = modelstore.parse_source(text)
    assert (found["repo"], found["revision"], found["file"]) == expected


@pytest.mark.parametrize("text", REJECTED)
def test_everything_else_is_refused_with_a_plain_message(text):
    with pytest.raises(modelstore.StoreError) as refusal:
        modelstore.parse_source(text)
    message = str(refusal.value)
    assert message and "Traceback" not in message and len(message) < 500


def test_the_configured_endpoint_is_accepted_and_no_other_http_host(hub):
    assert modelstore.parse_source(hub.url + "/lab/tuned/tree/v2")["revision"] == "v2"
    with pytest.raises(modelstore.StoreError):
        modelstore.parse_source(hub.cdn_url + "/lab/tuned")
    for bad in ({"repo": "../x"}, {"repo": "lab/tuned", "revision": "a/b"}, {"repo": "lab/tuned", "file": "../x.cact"},
                {"repo": "lab/tuned", "file": "sub/x.cact"}, {"repo": "lab/tuned", "file": "x.bin"}, {"repo": None}):
        with pytest.raises(modelstore.StoreError):
            modelstore.check_spec(bad.get("repo"), bad.get("revision", ""), bad.get("file", ""))


# ---- what the repository holds ---------------------------------------------------------------

def test_a_repository_with_our_manifest_is_used_through_it(hub):
    found = modelstore.resolve_custom(modelstore.parse_source("lab/tuned"))
    assert found["verified"] and found["file"] == "tuned-20L.cact" and found["bytes"] == len(DATA)
    assert found["sha256"] == SHA and [item["file"] for item in found["choices"]] == ["tuned-20L.cact", "tuned-8L.cact"]
    named = modelstore.resolve_custom(modelstore.parse_source(hub.url + "/lab/tuned/blob/main/tuned-8L.cact"))
    assert named["file"] == "tuned-8L.cact" and named["bytes"] == len(OTHER) and named["verified"]
    with pytest.raises(modelstore.StoreError, match="no file missing.cact"):
        modelstore.resolve_custom({"repo": "lab/tuned", "revision": "main", "file": "missing.cact"})
    assert not any(path.endswith(".cact") for path, _auth in hub.requests)       # asking downloads no model


def test_without_a_manifest_the_only_model_file_is_found_from_the_listing(hub):
    found = modelstore.resolve_custom(modelstore.parse_source("lab/plain"))
    assert found["file"] == "only.cact" and found["bytes"] == len(DATA)          # the real size, not the pointer's
    assert found["verified"] is False and found["sha256"] == ""                  # nothing to check against
    assert [item["file"] for item in found["choices"]] == ["only.cact"]          # no README, no file in a folder
    other = modelstore.resolve_custom(modelstore.parse_source("lab/other-manifest"))
    assert other["verified"] is False and other["file"] == "only.cact"           # a manifest.json of another kind


def test_with_several_model_files_the_user_has_to_pick(hub):
    found = modelstore.resolve_custom(modelstore.parse_source("lab/many"))
    assert found["file"] == "" and found["bytes"] == 0
    assert found["choices"] == [{"file": "a.cact", "bytes": len(DATA)}, {"file": "b.cact", "bytes": len(OTHER)}]
    picked = modelstore.resolve_custom({"repo": "lab/many", "revision": "main", "file": "b.cact"})
    assert picked["file"] == "b.cact" and picked["bytes"] == len(OTHER)


def test_what_cannot_be_used_says_why(hub):
    with pytest.raises(modelstore.StoreError, match="no .cact model file"):
        modelstore.resolve_custom(modelstore.parse_source("lab/empty"))
    with pytest.raises(modelstore.StoreError, match="HTTP 404"):
        modelstore.resolve_custom(modelstore.parse_source("lab/nothing-here"))
    with pytest.raises(modelstore.StoreError, match="private"):
        modelstore.resolve_custom(modelstore.parse_source("lab/secret"))
    with pytest.raises(modelstore.StoreError, match="private") as refusal:
        modelstore.resolve_custom(modelstore.parse_source("lab/secret"), "hf_WRONG")
    assert "hf_WRONG" not in str(refusal.value)
    hub.stop()
    with pytest.raises(modelstore.StoreError, match="could not be reached"):
        modelstore.resolve_custom(modelstore.parse_source("lab/plain"))


# ---- downloading it --------------------------------------------------------------------------

def test_a_custom_model_goes_into_its_own_folder_and_is_checked_against_the_manifest(hub, tmp_path):
    official = modelstore.ensure_model()
    found = modelstore.resolve_custom(modelstore.parse_source("lab/tuned"))
    path = modelstore.ensure_custom(found)
    assert path == tmp_path / "config" / "models" / "custom" / "lab--tuned@main" / "tuned-20L.cact"
    assert path.read_bytes() == DATA and official.read_bytes() == DATA and official != path
    assert official == tmp_path / "config" / "models" / "acme--official" / OFFICIAL
    assert modelstore.custom_record(found)["sha256"] == SHA
    other = modelstore.ensure_custom(modelstore.resolve_custom(modelstore.parse_source(hub.url + "/lab/tuned/tree/v2")))
    assert other.parent.name == "lab--tuned@v2" and other != path               # per revision
    with pytest.raises(modelstore.StoreError, match="does not match the manifest"):
        modelstore.ensure_custom({**found, "file": "tuned-8L.cact", "bytes": len(OTHER), "sha256": "0" * 64})
    assert not (path.parent / "tuned-8L.cact").exists()
    before = len(hub.requests)
    assert modelstore.ensure_custom(found) == path and modelstore.ensure_model() == official    # offline from here
    assert len(hub.requests) == before


def test_without_a_manifest_the_checksum_is_recorded_and_a_changed_file_is_refused(hub):
    found = modelstore.resolve_custom(modelstore.parse_source("lab/plain"))
    path = modelstore.ensure_custom(found)
    record = modelstore.custom_record(found)
    assert record["sha256"] == SHA and record["verified_against_manifest"] is False and record["bytes"] == len(DATA)
    assert modelstore.ensure_custom(found) == path
    path.write_bytes(DATA[:-1] + b"!")                                          # same size, other content
    with pytest.raises(modelstore.StoreError, match="not the file that was downloaded"):
        modelstore.ensure_custom(found)
    wrong = modelstore.resolve_custom(modelstore.parse_source("lab/wrong-size"))
    with pytest.raises(modelstore.StoreError, match="does not match the size"):
        modelstore.ensure_custom(wrong)
    assert not (modelstore.custom_dir(wrong) / "only.cact").exists()
    for broken in (None, {}, {"repo": "lab/plain"}, {**found, "bytes": 0}, {**found, "file": "../only.cact"}):
        with pytest.raises(modelstore.StoreError):
            modelstore.ensure_custom(broken if broken is None else {**broken, "revision": "other"})


def test_the_access_token_opens_a_private_repository_and_is_not_sent_to_another_host(hub):
    found = modelstore.resolve_custom(modelstore.parse_source("lab/secret"), TOKEN)
    notes = []
    path = modelstore.ensure_custom(found, token=TOKEN, note=notes.append)
    assert path.read_bytes() == DATA and TOKEN not in " ".join(notes)
    assert all(auth == f"Bearer {TOKEN}" for target, auth in hub.requests if "secret" in target)
    moved = modelstore.resolve_custom(modelstore.parse_source("lab/moved"), TOKEN)
    assert modelstore.ensure_custom(moved, token=TOKEN).read_bytes() == DATA
    assert ("/lab/moved/resolve/main/only.cact", f"Bearer {TOKEN}") in hub.requests
    assert hub.elsewhere == [("/cdn/only.cact", None)]                           # redirected: no token there


# ---- the app ---------------------------------------------------------------------------------

def settle(app, timeout=30.0):
    """Wait until the model that was just (re)started is up or has failed."""
    deadline = time.time() + timeout
    time.sleep(0.05)
    while app.model.state == "starting" and time.time() < deadline:
        time.sleep(0.02)


def test_the_swap_is_refused_without_the_confirmation_and_changes_nothing(app, hub):
    wanted = {"repo": "lab/tuned", "revision": "main", "file": "tuned-20L.cact"}
    for body in (wanted, {**wanted, "confirm": "true"}, {**wanted, "confirm": 1}):
        with pytest.raises(ActionError, match="confirmation"):
            app.dispatch("model_source_use", body)
    assert app.settings["model"]["source"] == "official" and hub.requests == []
    assert not (modelstore.models_dir().parent / "custom").exists()


def test_checking_shows_what_would_be_used_and_changes_nothing(app, hub):
    shown = app.dispatch("model_source_check", {"text": hub.url + "/lab/tuned/tree/main"})
    assert (shown["repo"], shown["revision"], shown["file"], shown["bytes"]) == ("lab/tuned", "main", "tuned-20L.cact", len(DATA))
    assert shown["verified"] is True and "127.0.0.1" in shown["host"]
    several = app.dispatch("model_source_check", {"text": "lab/many"})
    assert several["file"] == "" and len(several["choices"]) == 2
    assert app.dispatch("model_source_check", {"text": "lab/many", "file": "b.cact"})["bytes"] == len(OTHER)
    for text in ("https://example.com/lab/tuned", "lab/tuned?x=1", "", "lab/nothing-here"):
        with pytest.raises(ActionError):
            app.dispatch("model_source_check", {"text": text})
    with pytest.raises(ActionError, match="choose the model file"):
        app.dispatch("model_source_use", {"repo": "lab/many", "revision": "main", "confirm": True})
    assert app.settings["model"]["source"] == "official"
    assert not any(path.endswith(".cact") for path, _auth in hub.requests)


def test_swap_to_a_custom_model_and_back_with_one_click(app, hub, catalogue, tmp_path, monkeypatch):
    fake = FakeModel(catalogue).start()
    spawned = []

    def spawn(self, weight_args, generation):
        spawned.append(weight_args[1])
        return ModelClient(fake.url)

    monkeypatch.setattr(ModelManager, "_spawn", spawn)
    app.start_model = True
    models = tmp_path / "config" / "models"
    try:
        app.model.start_async().join(30)
        assert app.model.state == "ready" and spawned == [str(models / "acme--official" / OFFICIAL)]
        assert app.state()["model"]["source"] == "official" and app.state()["app"]["custom_model"] is True
        assert ModelPort(app.model).name() == "fake-20L.cact"

        out = app.dispatch("model_source_use", {"repo": "lab/secret", "revision": "main", "file": "only.cact",
                                                "token": TOKEN, "confirm": True})
        assert out["source"] == "custom"
        settle(app)
        assert app.model.state == "ready", app.model.detail
        assert spawned[-1] == str(models / "custom" / "lab--secret@main" / "only.cact")
        state = app.state()
        assert state["model"]["source"] == "custom" and state["model"]["custom"]["repo"] == "lab/secret"
        assert state["model"]["custom_checked"] is False and state["settings"]["model"]["hf_token_set"] is True
        assert TOKEN not in json.dumps(state) and TOKEN not in "\n".join(app.model.log)
        assert ModelPort(app.model).name() == "fake-20L.cact [custom: lab/secret@main]"      # in every history row
        with zipfile.ZipFile(app.export_diagnostics()["path"]) as archive:
            texts = {name: archive.read(name) for name in archive.namelist()}
        assert all(TOKEN.encode() not in text for text in texts.values())
        info = json.loads(next(text for name, text in texts.items() if name.endswith("info.json")))
        assert info["engine"]["source"] == "custom" and info["engine"]["custom"]["repo"] == "lab/secret"

        before = len(hub.requests)
        assert app.dispatch("model_source_official", {})["source"] == "official"
        settle(app)
        assert app.model.state == "ready" and spawned[-1] == str(models / "acme--official" / OFFICIAL)
        assert len(hub.requests) == before                                       # nothing is downloaded again
        assert (models / "custom" / "lab--secret@main" / "only.cact").is_file()  # and the custom file is kept
        assert app.settings["model"]["custom"]["repo"] == "lab/secret"           # remembered for the next time
    finally:
        app.model.stop()
        fake.stop()


def test_a_custom_model_that_cannot_be_fetched_is_a_readable_error_and_official_is_one_step_away(app, hub, monkeypatch):
    monkeypatch.setattr(ModelManager, "_spawn", lambda *args: pytest.fail("must not start a server"))
    app.settings.update({"model": {"source": "custom", "custom": {
        "repo": "lab/gone", "revision": "main", "file": "only.cact", "bytes": 10, "sha256": ""}}})
    app.model.start_async().join(30)
    assert app.model.state == "error" and "HTTP 404" in app.model.detail
    assert app.state()["model"]["source"] == "custom"
    app.dispatch("model_source_official", {})
    assert app.state()["model"]["source"] == "official" and app.settings["model"]["source"] == "official"
