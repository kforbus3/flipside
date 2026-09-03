"""One archive containing everything that cannot be rebuilt, and putting it back.

The documentation used to list six paths and say "put the files back". That is a
runbook only if somebody reads it before the disaster, keeps it in step as the
list grows -- it grew by four entries this release alone -- and does not miss
one at three in the morning. The list is the wrong artefact; the archive is.

What is in it, and why each thing is unrecoverable without it:

  rauc-keys/            The update signing key. RAUC installs a bundle only if
                        it is signed by the certificate already inside the
                        image, so losing key.pem means no further updates for
                        any machine already deployed, permanently. Nothing else
                        here comes close.
  users.json            Accounts, roles, password hashes.
  sessions/apitokens    Live credentials. Restoring them keeps automation
                        working across a rebuild.
  fleet/                Group membership, live machine state, rollouts. Group
                        membership in particular is somebody's deliberate work
                        and exists nowhere else.
  hosts/assignments     Per-machine image, hostname and action.
  deployments.jsonl     What was imaged, and whether it came back.
  audit.jsonl           The record of who did what.
  .secrets-store.json   Secrets-manager address and token.
  server/.env, webui/.env
                        Server and UI configuration, secrets included.

Images and bundles are deliberately absent: they are large and they are
rebuildable from the repository, which is the difference that decides what
belongs in a backup.

**The archive is as sensitive as the signing key**, because it contains it,
along with every token and password hash on the server. It is written 0600 and
the API refuses to produce one for anyone below admin. There is no encryption
here on purpose -- half-implemented crypto is worse than an honest warning, and
the file is going to somewhere that has its own answer for encryption at rest.
"""

from __future__ import annotations

import io
import json
import hashlib
import os
import shutil
import tarfile
import time
from typing import Any

from app import __version__
from app.config import settings

# (path relative to the project, required?) -- required only decides whether a
# restore complains about its absence, never whether it proceeds.
CONTENTS: list[tuple[str, bool]] = [
    ("output/rauc-keys", True),
    ("output/users.json", True),
    ("output/.sessions.json", False),
    ("output/.api-tokens.json", False),
    ("output/fleet", False),
    ("output/hosts/assignments.json", False),
    ("output/deployments.jsonl", False),
    ("output/audit.jsonl", False),
    ("output/.secrets-store.json", False),
    ("output/jobs/index.json", False),
    ("server/.env", False),
    ("webui/.env", False),
]

MANIFEST = "flipside-backup.json"


def _project(path: str) -> str:
    return os.path.join(settings.project_dir, path)


def _iter_files() -> list[str]:
    """Every file the archive should carry, relative to the project root."""
    out: list[str] = []
    for rel, _required in CONTENTS:
        full = _project(rel)
        if os.path.isfile(full):
            out.append(rel)
        elif os.path.isdir(full):
            for root, _dirs, names in os.walk(full):
                for name in sorted(names):
                    absolute = os.path.join(root, name)
                    # Temp files from an interrupted atomic write describe a
                    # state that never existed; restoring one would be worse
                    # than not having it.
                    if name.endswith(".tmp"):
                        continue
                    out.append(os.path.relpath(absolute, settings.project_dir))
    return sorted(out)


def create() -> tuple[bytes, dict[str, Any]]:
    """Build the archive in memory and return (bytes, manifest).

    In memory because it is kilobytes: the whole point of the contents list is
    that it excludes the gigabytes. Streaming would add a temp file to clean up
    for no benefit at this size.
    """
    files = _iter_files()
    manifest: dict[str, Any] = {
        "flipside_backup": 1,
        "version": __version__,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": {},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in files:
            full = _project(rel)
            try:
                with open(full, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            manifest["files"][rel] = {
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": oct(os.stat(full).st_mode & 0o777),
            }
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            info.mtime = int(os.path.getmtime(full))
            # Modes are recorded in the manifest and reapplied on restore, but
            # inside the archive everything is 0600: a tar extracted by hand
            # somewhere else must not leave the signing key world-readable
            # because of what its mode used to be.
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))

        payload = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo(name=MANIFEST)
        info.size = len(payload)
        info.mtime = int(time.time())
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(payload))

    return buf.getvalue(), manifest


class RestoreError(Exception):
    pass


def inspect(data: bytes) -> dict[str, Any]:
    """Read the manifest out of an archive without writing anything.

    Separate from restoring on purpose: "what is in this file and when was it
    taken" is the question asked before deciding to restore, and answering it
    should not be able to change anything.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            try:
                member = tar.extractfile(MANIFEST)
            except KeyError:
                # extractfile raises for a missing member rather than returning
                # None, and the raw KeyError reads as an internal fault
                # ("filename not found") for what is really a plain tarball
                # somebody handed over by mistake.
                member = None
            if member is None:
                raise RestoreError("this archive is not a Flipside backup "
                                   f"(no {MANIFEST} inside it)")
            manifest = json.load(member)
    except (tarfile.TarError, OSError) as exc:
        raise RestoreError(f"not a readable archive: {exc}")
    except ValueError as exc:
        raise RestoreError(f"the manifest is unreadable: {exc}")
    if manifest.get("flipside_backup") != 1:
        raise RestoreError("this archive is not a Flipside backup")
    return manifest


def restore(data: bytes, *, keep_current: bool = True) -> dict[str, Any]:
    """Put an archive's contents back, after checking all of it.

    Everything is verified and staged before anything is written. A restore that
    fails halfway is the worst outcome available here -- a server with half of
    one backup and half of its previous state is neither, and the operator finds
    out by using it.
    """
    manifest = inspect(data)
    recorded = manifest.get("files") or {}
    staged: dict[str, bytes] = {}

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for rel, meta in recorded.items():
            # The names come from the archive and are joined onto the project
            # directory, so they are contained rather than trusted. A tar whose
            # members are ../../etc/shadow is the oldest trick there is, and
            # this endpoint is reachable by an admin who may only have been
            # handed a file by someone else.
            if os.path.isabs(rel) or ".." in rel.split("/"):
                raise RestoreError(f"the archive contains an unsafe path: {rel}")
            resolved = os.path.realpath(os.path.join(settings.project_dir, rel))
            root = os.path.realpath(settings.project_dir)
            if not resolved.startswith(root + os.sep):
                raise RestoreError(f"the archive would write outside the project: {rel}")
            member = tar.extractfile(rel)
            if member is None:
                raise RestoreError(f"the manifest names {rel}, which is not in the archive")
            payload = member.read()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != meta.get("sha256"):
                raise RestoreError(f"{rel} does not match its recorded checksum; "
                                   "the archive is damaged")
            staged[rel] = payload

    # Everything has been read and checked. Only now does anything move.
    safety = ""
    if keep_current:
        safety = os.path.join(settings.output_dir,
                              f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz")
        try:
            current, _ = create()
            with open(safety, "wb") as f:
                f.write(current)
            os.chmod(safety, 0o600)
        except OSError:
            # Not fatal: a fresh server has nothing to preserve, and refusing to
            # restore because the *old* state could not be saved would be the
            # wrong way round.
            safety = ""

    written = []
    for rel, payload in staged.items():
        full = os.path.join(settings.project_dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".restore.tmp"
        with open(tmp, "wb") as f:
            f.write(payload)
        mode = recorded[rel].get("mode", "0o600")
        try:
            os.chmod(tmp, int(mode, 8))
        except (ValueError, OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, full)
        written.append(rel)

    return {"restored": sorted(written), "from": manifest.get("created"),
            "version": manifest.get("version"), "safety_copy": safety}


def reload_stores() -> None:
    """Drop in-memory caches so a restore takes effect without a restart.

    Several stores read their file once and keep it. After a restore those
    caches describe the state that was just replaced, and the UI would show the
    old fleet and the old users until somebody restarted the container -- which
    reads as a restore that silently did nothing.
    """
    from app import apitokens, sessions
    from app.fleet import fleet
    from app.rollouts import rollouts

    fleet.reload()
    rollouts.reload()
    # sessions and apitokens memoise their file into a module-level `_cache`;
    # users.py reads through on every call and so needs nothing. Cleared by name
    # rather than through a helper because there is nothing else to it, and a
    # helper on each module would exist for this one caller.
    sessions._cache = None
    apitokens._cache = None


def disk_space_for_safety_copy() -> bool:
    try:
        return shutil.disk_usage(settings.output_dir).free > 64 * 1024 * 1024
    except OSError:
        return False
