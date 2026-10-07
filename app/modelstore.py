"""The published model: where it is downloaded from and where it is kept on this machine.

The repository does not contain the model. The weights live in a Hugging Face
model repository; the app downloads them once into the per-user data folder
(next to the settings) and works offline afterwards. `scripts/download_model.py`
calls the same code.

    https://huggingface.co/<HF_REPO>/resolve/<HF_REVISION>/manifest.json
    https://huggingface.co/<HF_REPO>/resolve/<HF_REVISION>/<file>.cact

`manifest.json` lists every weight file with its size and SHA-256. A download
is resumable (`<file>.part`, HTTP Range) and is only moved into place when the
checksum matches. Standard library only.

A user can also point the app at another Hugging Face model repository (Settings,
"Model source"). Such a model is the user's own choice and is not checked by this
project. It is kept apart from the published one, in

    <per-user data folder>/models/custom/<owner>--<name>@<revision>/

so switching back to the published model needs no new download. `parse_source`
accepts what people paste (an id or a huggingface.co link), `resolve_custom` asks
the repository what it holds, `ensure_custom` downloads and re-checks the file.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, paths

# ---- the one place that names the model repository -----------------------------
HF_REPO = "CharlZKP/fusion-needle3"            # "<owner>/<name>" on huggingface.co
HF_REVISION = "main"    # a branch, a tag or a commit hash
# --------------------------------------------------------------------------------
# The oldest published model this version of the app is made for (its tool list has to match the
# app's). A copy downloaded by an earlier version is replaced on the next start with a connection.
MIN_MODEL_VERSION = "0.2.0"
HF_ENDPOINT = "https://huggingface.co"
MANIFEST_NAME = "manifest.json"
CHUNK = 256 * 1024
ATTEMPTS = 5
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.cact$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class StoreError(Exception):
    """The model could not be made available; the message is for the user."""


class Cancelled(Exception):
    """Raised by a progress callback to stop a download (the partial file is kept)."""


def source() -> dict:
    """Repository, revision and endpoint; each can be overridden by an environment variable."""
    return {
        "repo": os.environ.get("FUSION_NEEDLE_HF_REPO") or HF_REPO,
        "revision": os.environ.get("FUSION_NEEDLE_HF_REVISION") or HF_REVISION,
        "endpoint": (os.environ.get("FUSION_NEEDLE_HF_ENDPOINT") or HF_ENDPOINT).rstrip("/"),
    }


def configured() -> bool:
    repo = source()["repo"]
    return bool(re.match(r"^[\w.-]+/[\w.-]+$", repo)) and not repo.startswith("OWNER/")


def models_dir() -> Path:
    """<per-user data folder>/models/<owner>--<name>/"""
    return paths.config_dir() / "models" / source()["repo"].replace("/", "--")


def url_for(name: str) -> str:
    where = source()
    return (f"{where['endpoint']}/{where['repo']}/resolve/{urllib.parse.quote(where['revision'], safe='')}/"
            f"{urllib.parse.quote(name)}")


class _TokenStaysHome(urllib.request.HTTPRedirectHandler):
    """A download is redirected to a file server on another host: the access token is not sent there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_TokenStaysHome)


def _request(url: str, headers: dict | None = None, timeout: float = 30.0, token: str = ""):
    """``token``: a Hugging Face access token (private repositories). It is never put in a URL, a message or a log."""
    sent = {"User-Agent": f"fusion-needle/{__version__}", **(headers or {})}
    if token:
        sent["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=sent)
    return _OPENER.open(request, timeout=timeout)                 # noqa: S310 - the endpoint is configuration


def check_manifest(data) -> dict:
    """The manifest as a dict, or StoreError. File names are plain names: never a path."""
    if not isinstance(data, dict) or not isinstance(data.get("files"), list) or not data["files"]:
        raise StoreError("the model manifest lists no files")
    for entry in data["files"]:
        if not (isinstance(entry, dict) and isinstance(entry.get("file"), str) and _FILE_RE.match(entry["file"])
                and isinstance(entry.get("bytes"), int) and entry["bytes"] > 0
                and isinstance(entry.get("sha256"), str) and _SHA_RE.match(entry["sha256"])):
            raise StoreError("the model manifest has an entry without a valid file name, size or sha256")
    return data


def outdated(manifest: dict) -> bool:
    """True for a manifest of the published model that is older than MIN_MODEL_VERSION.
    Another repository (set through the environment) has its own version numbers and is left alone."""
    if source()["repo"] != HF_REPO:
        return False
    def number(text) -> tuple:
        return tuple(int(part) for part in re.findall(r"\d+", str(text or ""))[:3])
    return number(manifest.get("version")) < number(MIN_MODEL_VERSION)


def local_manifest(directory: Path | None = None) -> dict | None:
    path = (directory or models_dir()) / MANIFEST_NAME
    try:
        with open(path, encoding="utf-8") as handle:
            return check_manifest(json.load(handle))
    except (OSError, ValueError, StoreError):
        return None


def fetch_manifest() -> dict:
    if not configured():
        raise StoreError(
            f"the model repository is not set yet (app/modelstore.py says {source()['repo']!r}). "
            "Until it is, choose a .cact file or 'base' as the weights (Settings, Model).")
    url = url_for(MANIFEST_NAME)
    try:
        with _request(url) as response:
            return check_manifest(json.loads(response.read(1 << 20).decode("utf-8")))
    except urllib.error.HTTPError as failure:
        raise StoreError(f"the model manifest could not be read ({url}: HTTP {failure.code})") from failure
    except (urllib.error.URLError, OSError, ValueError) as failure:
        reason = getattr(failure, "reason", failure)
        raise StoreError(
            f"the model could not be downloaded ({url}: {reason}). It is downloaded once and kept in "
            f"{models_dir()}; after that the app works offline.") from failure


def pick(manifest: dict, variant: str | None = None) -> dict:
    """The manifest entry to use: ``variant`` (a file name or a depth such as '8L'), else the default."""
    files = manifest["files"]
    wanted = variant or manifest.get("default")
    if wanted:
        for entry in files:
            if entry["file"] == wanted or entry["file"].endswith(f"-{wanted}.cact"):
                return entry
        if variant:
            raise StoreError(f"the model repository has no file for {variant!r} "
                             f"(it has: {', '.join(entry['file'] for entry in files)})")
    return files[0]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def progress_text(done: int, total: int, name: str) -> str:
    share = f" ({done * 100 // total}%)" if total else ""
    return f"downloading the model {name}: {done / 1e6:.1f} of {total / 1e6:.1f} MB{share}"


def _fetch_into(part: Path, entry: dict, progress, url: str | None = None, token: str = "") -> None:
    """One attempt: append to ``part`` from where it stops. Raises OSError / URLError on a broken connection."""
    url = url or url_for(entry["file"])
    total = entry["bytes"]
    have = part.stat().st_size if part.exists() else 0
    if have > total:
        part.unlink()
        have = 0
    if have == total:
        return
    headers = {"Range": f"bytes={have}-"} if have else {}
    try:
        response = _request(url, headers, timeout=60, token=token)
    except urllib.error.HTTPError as failure:
        if failure.code == 416 and have:                 # our partial file does not fit the server's file
            part.unlink()
            return _fetch_into(part, entry, progress, url, token)
        raise
    with response:
        resumed = have and response.status == 206
        if not resumed:
            have = 0                                     # the server sent the whole file
        with open(part, "ab" if resumed else "wb") as handle:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                have += len(block)
                if progress is not None:
                    progress(have, total, entry["file"])
    if have < total:
        raise ConnectionError(f"the connection closed after {have} of {total} bytes")


def download(entry: dict, directory: Path, progress=None, note=None, url: str | None = None, token: str = "") -> Path:
    """Download one manifest entry into ``directory``; resumes a ``.part`` file; checks the SHA-256.

    ``url`` / ``token``: a file of another repository (see ``ensure_custom``). An entry with an empty
    ``sha256`` comes from a repository without a manifest: only its size can be checked.
    """
    url = url or url_for(entry["file"])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / entry["file"]
    part = directory / (entry["file"] + ".part")
    failure: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            _fetch_into(part, entry, progress, url, token)
            failure = None
            break
        except Cancelled:
            raise
        except urllib.error.HTTPError as error:
            raise StoreError(f"{url}: HTTP {error.code}") from error
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
            failure = error
            if note is not None:
                note(f"model download interrupted (attempt {attempt} of {ATTEMPTS}): {getattr(error, 'reason', error)}")
            time.sleep(min(2.0 * attempt, 8.0))
    if failure is not None:
        raise StoreError(f"the model download did not finish: {getattr(failure, 'reason', failure)}. "
                         "Start again to continue where it stopped.") from failure
    size = part.stat().st_size
    found = sha256_file(part) if size == entry["bytes"] else ""
    if not found or (entry["sha256"] and found != entry["sha256"]):
        part.unlink(missing_ok=True)
        listed = "the manifest" if entry["sha256"] else "the size the repository lists"
        raise StoreError(f"{entry['file']}: the downloaded file does not match {listed} "
                         f"({size} bytes, sha256 {found or 'not computed'}); it was removed. Start again.")
    os.replace(part, target)
    return target


def ensure_model(variant: str | None = None, progress=None, note=None, force: bool = False,
                 verify: bool = False) -> Path:
    """Path of the model file, downloading it when this machine does not have it yet.

    Offline use: once the file and its manifest are in the data folder, no
    request is made. ``verify`` re-hashes the local file; ``force`` asks the
    repository again (a new manifest, and a new file when its checksum changed).
    ``progress(done, total, file_name)`` is called while downloading and may
    raise ``Cancelled``.
    """
    directory = models_dir()
    manifest = None if force else local_manifest(directory)
    fresh = None
    if manifest is not None and outdated(manifest):
        try:
            fresh = fetch_manifest()
            manifest = None
        except StoreError as failure:                     # offline: the older model still works for most tools
            if note is not None:
                note(f"a newer model is published, but it could not be fetched ({failure}); using the one here")
    if manifest is not None:
        try:
            entry = pick(manifest, variant)
        except StoreError:
            entry = None                                  # an older manifest without that file: ask again
        if entry is not None:
            path = directory / entry["file"]
            if path.is_file() and path.stat().st_size == entry["bytes"] and (
                    not verify or sha256_file(path) == entry["sha256"]):
                return path
    manifest = fresh or fetch_manifest()
    entry = pick(manifest, variant)
    path = directory / entry["file"]
    if not (path.is_file() and path.stat().st_size == entry["bytes"] and sha256_file(path) == entry["sha256"]):
        if note is not None:
            note(f"downloading {url_for(entry['file'])} ({entry['bytes'] / 1e6:.1f} MB) into {directory}")
        path = download(entry, directory, progress, note)
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / (MANIFEST_NAME + ".tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    os.replace(temp, directory / MANIFEST_NAME)
    return path


# ---- a model from another repository: the user's own choice, not checked by this project ----------
CUSTOM_FOLDER = "custom"
DEFAULT_REVISION = "main"
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?"
_REPO_RE = re.compile(rf"^{_NAME}/{_NAME}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PASTED_RE = re.compile(r"^[A-Za-z0-9._/:-]{1,400}$")            # no spaces, %, ?, #, @ or backslashes at all
# first path parts of huggingface.co that are not the owner of a model
_OTHER_KINDS = ("data" + "sets", "spaces")              # the hub's other kinds of repository, under their own prefix
_NOT_AN_OWNER = {*_OTHER_KINDS, "api", "docs", "models", "collections", "organizations", "settings",
                 "login", "join", "blog", "papers", "posts", "tasks", "huggingface.co", "www.huggingface.co"}
_HOW = ("Paste the id of a Hugging Face model (owner/name) or its link "
        "(https://huggingface.co/owner/name, also with /tree/<revision> or /resolve/<revision>/<file>.cact).")


def check_spec(repo, revision="", file="") -> dict:
    """``{"repo", "revision", "file"}`` with nothing in it but plain names, or StoreError. ``file`` may be ""."""
    if (not isinstance(repo, str) or not _REPO_RE.match(repo) or ".." in repo
            or repo.split("/")[0].lower() in _NOT_AN_OWNER):
        raise StoreError(f"That is not the id of a Hugging Face model. {_HOW}")
    revision = revision or DEFAULT_REVISION
    if not isinstance(revision, str) or not _REVISION_RE.match(revision) or ".." in revision:
        raise StoreError("That revision cannot be used. Use a branch, a tag or a commit hash made of letters, "
                         "digits, dots, dashes and underscores (no slashes).")
    file = file or ""
    if file and (not isinstance(file, str) or not _FILE_RE.match(file) or ".." in file):
        raise StoreError("The file must be a .cact model file at the top of the repository, "
                         "with a plain name (letters, digits, dots, dashes, underscores).")
    return {"repo": repo, "revision": revision, "file": file}


def parse_source(text) -> dict:
    """What a user pasted -> ``{"repo", "revision", "file"}``; StoreError with a plain message otherwise.

    Accepted: ``owner/name``, ``https://huggingface.co/owner/name``, ``.../tree/<revision>`` and a file link
    ``.../resolve/<revision>/<file>.cact`` or ``.../blob/<revision>/<file>.cact``. Only huggingface.co over
    https (or the endpoint this build is configured for) is accepted, and nothing with a query, a fragment,
    an encoded character or a path that leads anywhere else.
    """
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise StoreError(f"Nothing was entered. {_HOW}")
    if not _PASTED_RE.match(text):
        raise StoreError("That address has characters a Hugging Face model link never has "
                         f"(spaces, %, ?, #, @ or \\). {_HOW}")
    if "://" not in text and not text.lower().startswith(("huggingface.co", "www.")):
        if text.count("/") != 1:
            raise StoreError(f"That is not the id of a Hugging Face model. {_HOW}")
        return check_spec(text)
    rest = None
    for base in dict.fromkeys((HF_ENDPOINT, source()["endpoint"])):
        if text.startswith(base + "/"):
            rest = text[len(base) + 1:]
            break
    if rest is None:
        if text.lower().startswith("http://"):
            raise StoreError("Only https links to huggingface.co are accepted.")
        raise StoreError(f"Only links to huggingface.co are accepted (https://huggingface.co/owner/name). {_HOW}")
    parts = rest[:-1].split("/") if rest.endswith("/") else rest.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise StoreError(f"That link does not lead to a model. {_HOW}")
    if parts[0].lower() in _OTHER_KINDS:
        raise StoreError("That link leads to another kind of Hugging Face page, not to a model.")
    if len(parts) == 2:
        return check_spec("/".join(parts))
    if len(parts) == 4 and parts[2] == "tree":
        return check_spec("/".join(parts[:2]), parts[3])
    if len(parts) == 5 and parts[2] in ("resolve", "blob"):
        if not parts[4].endswith(".cact"):
            raise StoreError("That link is not a .cact model file. Link to the model repository or to its .cact file.")
        return check_spec("/".join(parts[:2]), parts[3], parts[4])
    raise StoreError(f"That link does not lead to a model or to a .cact file at the top of one. {_HOW}")


def custom_dir(spec: dict) -> Path:
    """Where a custom model is kept: its own folder per repository and revision, never the published model's."""
    spec = check_spec(spec.get("repo"), spec.get("revision"), "")
    return (paths.config_dir() / "models" / CUSTOM_FOLDER
            / f"{spec['repo'].replace('/', '--')}@{spec['revision']}")


def _custom_url(spec: dict, name: str) -> str:
    return (f"{source()['endpoint']}/{spec['repo']}/resolve/{urllib.parse.quote(spec['revision'], safe='')}/"
            f"{urllib.parse.quote(name)}")


def _read_json(url: str, token: str, limit: int = 4 << 20):
    with _request(url, token=token) as response:
        return json.loads(response.read(limit).decode("utf-8"))


def _no_access(spec: dict, code: int) -> StoreError:
    if code in (401, 403):
        return StoreError(f"Hugging Face refused access to {spec['repo']} (HTTP {code}). Either it does not exist, "
                          "or it is private: then enter an access token that may read it.")
    if code == 404:
        return StoreError(f"Hugging Face has no model {spec['repo']} with the revision {spec['revision']!r} "
                          "(HTTP 404). Check the spelling.")
    return StoreError(f"Hugging Face answered HTTP {code} for {spec['repo']}. Try again later.")


def _unreachable(failure) -> StoreError:
    return StoreError(f"Hugging Face could not be reached ({getattr(failure, 'reason', failure)}). "
                      "Check the internet connection and try again.")


def _listed_models(spec: dict, token: str) -> list[dict]:
    """The .cact files at the top of a repository without a manifest, from the public file listing."""
    url = (f"{source()['endpoint']}/api/models/{spec['repo']}/tree/"
           f"{urllib.parse.quote(spec['revision'], safe='')}")
    try:
        listing = _read_json(url, token)
    except urllib.error.HTTPError as failure:
        raise _no_access(spec, failure.code) from failure
    except ValueError as failure:
        raise StoreError(f"Hugging Face did not list the files of {spec['repo']} in a form this app knows.") from failure
    except (urllib.error.URLError, OSError) as failure:
        raise _unreachable(failure) from failure
    found = []
    for item in listing if isinstance(listing, list) else []:
        if not isinstance(item, dict) or item.get("type") not in (None, "file"):
            continue
        name = item.get("path")
        large = item.get("lfs") if isinstance(item.get("lfs"), dict) else {}
        size = large.get("size") or item.get("size")
        if isinstance(name, str) and _FILE_RE.match(name) and ".." not in name and isinstance(size, int) and size > 0:
            found.append({"file": name, "bytes": size, "sha256": ""})
    return found


def resolve_custom(spec: dict, token: str = "") -> dict:
    """Ask the repository what it holds. Nothing is downloaded but the manifest or the file listing.

    -> ``{"repo", "revision", "file", "bytes", "sha256", "verified", "choices": [{"file", "bytes"}], "folder"}``.
    A repository with a ``manifest.json`` in this project's format is used through it (``verified``: the
    download is checked against its SHA-256). Without one, the file named in the link is used, or the only
    .cact file; with several, ``file`` is "" and the user picks from ``choices``. Then there is no checksum
    to check against (``sha256`` is "").
    """
    spec = check_spec(spec.get("repo"), spec.get("revision"), spec.get("file"))
    manifest = None
    try:
        manifest = check_manifest(_read_json(_custom_url(spec, MANIFEST_NAME), token, 1 << 20))
    except urllib.error.HTTPError as failure:
        if failure.code != 404:
            raise _no_access(spec, failure.code) from failure
    except (ValueError, StoreError):
        manifest = None                                  # a manifest.json of some other kind: not ours
    except (urllib.error.URLError, OSError) as failure:
        raise _unreachable(failure) from failure
    if manifest is not None:
        files = [{"file": entry["file"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
                 for entry in manifest["files"]]
        wanted = spec["file"] or pick(manifest)["file"]
    else:
        files = _listed_models(spec, token)
        if not files:
            raise StoreError(f"{spec['repo']} ({spec['revision']}) has no .cact model file at its top level.")
        wanted = spec["file"] or (files[0]["file"] if len(files) == 1 else "")
    entry = next((item for item in files if item["file"] == wanted), None)
    if wanted and entry is None:
        raise StoreError(f"{spec['repo']} ({spec['revision']}) has no file {wanted}. "
                         f"It has: {', '.join(item['file'] for item in files)}.")
    return {"repo": spec["repo"], "revision": spec["revision"], "file": entry["file"] if entry else "",
            "bytes": entry["bytes"] if entry else 0, "sha256": entry["sha256"] if entry else "",
            "verified": manifest is not None,
            "choices": [{"file": item["file"], "bytes": item["bytes"]} for item in files],
            "folder": str(custom_dir(spec))}


def _record_path(directory: Path, name: str) -> Path:
    return directory / (name + ".json")


def custom_record(custom: dict) -> dict | None:
    """What was written down when the custom model was downloaded (its SHA-256 above all), or None."""
    try:
        spec = check_spec(custom.get("repo"), custom.get("revision"), custom.get("file"))
        with open(_record_path(custom_dir(spec), spec["file"]), encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, StoreError, AttributeError):
        return None
    if isinstance(record, dict) and isinstance(record.get("sha256"), str) and _SHA_RE.match(record["sha256"]):
        return record
    return None


def ensure_custom(custom: dict, token: str = "", progress=None, note=None) -> Path:
    """Path of the custom model the user confirmed (``{"repo", "revision", "file", "bytes", "sha256"}``).

    The file is downloaded once into its own folder. Its SHA-256 is written next to it; every later start
    hashes the file again and refuses one that has changed. ``sha256`` is the manifest's checksum when the
    repository has a manifest, else "" (then only the size can be checked on download).
    """
    if not isinstance(custom, dict):
        raise StoreError("no custom model is set (Settings, Model source)")
    spec = check_spec(custom.get("repo"), custom.get("revision"), custom.get("file"))
    if not spec["file"]:
        raise StoreError("no custom model file is set (Settings, Model source)")
    expected = custom.get("sha256") if isinstance(custom.get("sha256"), str) and _SHA_RE.match(custom["sha256"]) else ""
    directory = custom_dir(spec)
    path = directory / spec["file"]
    record = custom_record(spec)
    if path.is_file() and record is not None:
        if sha256_file(path) != record["sha256"] or (expected and record["sha256"] != expected):
            raise StoreError(f"the custom model file {path} is not the file that was downloaded (its checksum "
                             "differs). Delete it to download it again, or switch back to the official model.")
        return path
    size = custom.get("bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise StoreError("the size of the custom model is not known; choose it again (Settings, Model source)")
    url = _custom_url(spec, spec["file"])
    if note is not None:
        note(f"downloading the custom model {url} ({size / 1e6:.1f} MB) into {directory}")
    path = download({"file": spec["file"], "bytes": size, "sha256": expected}, directory, progress, note,
                    url=url, token=token)
    written = {"repo": spec["repo"], "revision": spec["revision"], "file": spec["file"], "bytes": size,
               "sha256": sha256_file(path), "verified_against_manifest": bool(expected),
               "downloaded": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    temp = directory / (spec["file"] + ".json.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(written, handle, indent=2)
        handle.write("\n")
    os.replace(temp, _record_path(directory, spec["file"]))
    return path
