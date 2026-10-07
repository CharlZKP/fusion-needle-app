#!/usr/bin/env python3
"""Download the Fusion Needle model from Hugging Face into the per-user data folder.

    python scripts/download_model.py              # the default file of the repository
    python scripts/download_model.py --variant 8L # another depth listed in the manifest
    python scripts/download_model.py --list       # what the repository offers
    python scripts/download_model.py --verify     # re-hash the local file

The app does the same on its first start; this script is for preparing a machine
in advance or for a machine that is online only now. The repository and the
revision are set in app/modelstore.py (HF_REPO, HF_REVISION); --repo and
--revision override them for one run. Standard library only.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import modelstore  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--variant", default=None, help="a file name or a depth such as 20L or 8L (default: the manifest's default)")
    parser.add_argument("--repo", default=None, help="Hugging Face model repository, <owner>/<name>")
    parser.add_argument("--revision", default=None, help="branch, tag or commit")
    parser.add_argument("--force", action="store_true", help="ask the repository again even when a local copy exists")
    parser.add_argument("--verify", action="store_true", help="check the SHA-256 of the local copy")
    parser.add_argument("--list", action="store_true", help="print the files of the manifest and exit")
    args = parser.parse_args(argv)
    if args.repo:
        os.environ["FUSION_NEEDLE_HF_REPO"] = args.repo
    if args.revision:
        os.environ["FUSION_NEEDLE_HF_REVISION"] = args.revision
    where = modelstore.source()
    print(f"repository  {where['endpoint']}/{where['repo']}  (revision {where['revision']})", file=sys.stderr)
    print(f"folder      {modelstore.models_dir()}", file=sys.stderr)
    last = [-1]

    def progress(done, total, name):
        share = done * 100 // total if total else 0
        if share != last[0]:
            last[0] = share
            sys.stderr.write("\r" + modelstore.progress_text(done, total, name) + "   ")
            sys.stderr.flush()

    try:
        if args.list:
            manifest = modelstore.fetch_manifest()
            for entry in manifest["files"]:
                mark = "*" if entry["file"] == manifest.get("default") else " "
                print(f"{mark} {entry['file']}  {entry['bytes'] / 1e6:.1f} MB  sha256 {entry['sha256']}")
            return 0
        path = modelstore.ensure_model(args.variant, progress=progress,
                                       note=lambda text: print(text, file=sys.stderr),
                                       force=args.force, verify=args.verify)
    except modelstore.StoreError as failure:
        print(f"\nerror: {failure}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; run it again to continue where it stopped", file=sys.stderr)
        return 130
    if last[0] >= 0:
        sys.stderr.write("\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
