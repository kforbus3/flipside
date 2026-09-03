"""Software bills of materials: download one, or search every one at once.

The search is the reason this is a router rather than two static file routes.
"Which of our images carry the vulnerable openssl" is asked under time pressure,
usually by someone who is not going to mount thirty images to find out, and the
answer has to come from one request. Every artifact's package list is a sorted
TSV beside it, so answering means reading a few hundred kilobytes of text --
which is cheap enough that it needs no index, and an index is a thing that goes
stale without saying so.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.security import Principal, require_viewer

router = APIRouter(tags=["sbom"])

# What each format is called on disk, and what to serve it as. The media types
# are the registered ones for each specification, so a scanner pointed at these
# URLs recognises what it is being given without sniffing the body.
FORMATS = {
    "spdx": ("spdx.json", "application/spdx+json"),
    "cyclonedx": ("cdx.json", "application/vnd.cyclonedx+json"),
    "packages": ("packages.tsv", "text/tab-separated-values"),
}


def _artifact_path(name: str) -> str:
    """Resolve an artifact name to a path inside output/, or refuse.

    The name arrives from a browser and is joined onto a directory, so it is
    contained rather than trusted: realpath after the join, then a prefix check.
    os.path.join ignores the base entirely for an absolute path, and encoded
    ../ arrives already decoded — either would otherwise read any file the
    process can.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "invalid name")
    root = os.path.realpath(settings.output_dir)
    for sub in ("", "bundles"):
        candidate = os.path.realpath(os.path.join(root, sub, name))
        if candidate.startswith(root + os.sep) and os.path.isfile(candidate):
            return candidate
    raise HTTPException(404, f"no such image or bundle: {name}")


@router.get("/sbom/{name}")
async def download(name: str, format: str = Query("spdx"),
                   _: Principal = Depends(require_viewer)):
    """The SBOM beside one image or bundle, in the requested format."""
    if format not in FORMATS:
        raise HTTPException(400, f"format must be one of {', '.join(FORMATS)}")
    artifact = _artifact_path(name)
    suffix, media = FORMATS[format]
    path = f"{artifact}.{suffix}"
    if not os.path.isfile(path):
        raise HTTPException(404, f"{name} has no SBOM. It was built before SBOMs "
                                 "existed, or its build could not read the package "
                                 "list; rebuild it to get one.")
    return FileResponse(path, media_type=media,
                        filename=f"{name}.{suffix}")


def _scan(pattern: str, version_contains: str) -> list[dict]:
    """Every artifact carrying a package matching `pattern`.

    Reads the TSVs rather than the JSON documents: they are the same facts in a
    tenth of the bytes, and this walks every artifact on the server.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise HTTPException(400, f"not a usable search pattern: {exc}")

    root = settings.output_dir
    results: list[dict] = []
    for directory in (root, os.path.join(root, "bundles")):
        if not os.path.isdir(directory):
            continue
        for fn in sorted(os.listdir(directory)):
            if not fn.endswith(".packages.tsv"):
                continue
            artifact = fn[: -len(".packages.tsv")]
            hits = []
            try:
                with open(os.path.join(directory, fn)) as f:
                    for line in f:
                        parts = line.rstrip("\n").split("\t")
                        if len(parts) < 3 or not rx.search(parts[0]):
                            continue
                        if version_contains and version_contains not in parts[1]:
                            continue
                        hits.append({"package": parts[0], "version": parts[1],
                                     "arch": parts[2]})
            except OSError:
                continue
            if hits:
                results.append({
                    "artifact": artifact,
                    "kind": "bundle" if directory.endswith("bundles") else "image",
                    "matches": hits[:50],
                    "match_count": len(hits),
                })
    return results


@router.get("/sbom")
async def search(package: str = Query(..., min_length=1),
                 version: str = Query(""),
                 _: Principal = Depends(require_viewer)):
    """Which images and bundles contain a package, and at what version.

    `package` is a regular expression, so `^openssl$` and `libssl` are both
    reasonable things to ask. `version` is a plain substring, because the useful
    question is almost always "which ones are still on 3.0.x".
    """
    results = await run_in_threadpool(_scan, package, version)
    # Named so the answer distinguishes "nothing matched" from "nothing has an
    # SBOM to match against" — which look identical and mean opposite things.
    with_sbom = 0
    for directory in (settings.output_dir, os.path.join(settings.output_dir, "bundles")):
        if os.path.isdir(directory):
            with_sbom += sum(1 for f in os.listdir(directory) if f.endswith(".packages.tsv"))
    return {"results": results, "searched": with_sbom,
            "artifacts": sum(1 for r in results)}
