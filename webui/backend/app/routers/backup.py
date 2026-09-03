"""Take a backup, look inside one, put one back.

Admin only, all three. The archive contains the update signing key, every
password hash, every live session and API token, and the secrets-manager token
— it is as sensitive as the most sensitive thing in it, and downloading it is
the single most consequential read this API offers.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app import backup as backup_mod
from app.security import Principal, require_admin

router = APIRouter(prefix="/backup", tags=["backup"])

# A backup is kilobytes. Anything larger is not one, and reading it into memory
# to find that out is the mistake.
MAX_UPLOAD = 64 * 1024 * 1024


@router.get("")
async def download(request: Request, principal: Principal = Depends(require_admin)):
    """The whole of this server's unrecoverable state, as one file."""
    data, manifest = await run_in_threadpool(backup_mod.create)
    name = f"flipside-backup-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    # Audited explicitly: the middleware only records non-GET calls, and this is
    # a GET that hands over the signing key. It is exactly the kind of thing the
    # log exists for — the same reasoning as the LUKS passphrase reveal.
    from app import audit
    audit.record(actor=principal.name, role=principal.role, method="GET",
                 path="/api/backup", status=200,
                 ip=request.client.host if request.client else "",
                 summary=f"downloaded a backup of {len(manifest['files'])} files")
    return Response(
        content=data, media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{name}"',
                 # It contains a private key; nothing should keep a copy.
                 "Cache-Control": "no-store"})


@router.get("/manifest")
async def manifest(_: Principal = Depends(require_admin)):
    """What a backup taken right now would contain, without producing one.

    So the contents can be checked against expectations before trusting the
    backup — a list that has quietly stopped including something is the failure
    a backup has, and it is only ever noticed during a restore.
    """
    _data, m = await run_in_threadpool(backup_mod.create)
    return {"version": m["version"], "created": m["created"],
            "files": [{"path": p, **meta} for p, meta in sorted(m["files"].items())],
            "bytes": sum(f["size"] for f in m["files"].values())}


async def _read(upload: UploadFile) -> bytes:
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "that file is far too large to be a backup")
    return data


@router.post("/inspect")
async def inspect(file: UploadFile = File(...), _: Principal = Depends(require_admin)):
    """Read an archive's manifest without writing anything.

    Deliberately a separate call from the restore: "what is in this file and
    when was it taken" is the question asked *before* deciding, and answering it
    must not be able to change anything.
    """
    data = await _read(file)
    try:
        m = await run_in_threadpool(backup_mod.inspect, data)
    except backup_mod.RestoreError as exc:
        raise HTTPException(400, str(exc))
    return {"version": m.get("version"), "created": m.get("created"),
            "files": sorted((m.get("files") or {}).keys())}


@router.post("/restore")
async def restore(request: Request, file: UploadFile = File(...),
                  principal: Principal = Depends(require_admin)):
    """Put an archive back. Everything is verified before anything is written.

    A restore replaces the user database, so it can replace the account making
    the request — including its password. That is the point of a restore and is
    not guarded against; it is why the current state is copied aside first, and
    why the response says where that copy is.
    """
    data = await _read(file)
    try:
        result = await run_in_threadpool(backup_mod.restore, data)
    except backup_mod.RestoreError as exc:
        raise HTTPException(400, str(exc))
    # Without this the UI keeps showing the state that was just replaced until
    # somebody restarts the container, which reads as a restore that did nothing.
    await run_in_threadpool(backup_mod.reload_stores)
    request.state.audit_summary = (
        f"restored {len(result['restored'])} files from a backup taken "
        f"{result.get('from')}")
    return {
        **result,
        "note": ("Sessions and users came from the archive. If the backup "
                 "predates your account or your current password, log in with "
                 "the credentials that were current when it was taken."),
        # Two things a restore cannot do from inside the running process, said
        # here rather than left to be discovered:
        "caveats": [
            "The .env files were written to disk but this process read its "
            "settings at startup, so anything in them — SECRET_KEY, OIDC, "
            "CONTROL_URL, audit forwarding — takes effect on the next restart.",
            "Files added since the backup was taken are left in place. A "
            "restore puts back what the archive contains; it does not delete "
            "what it does not mention.",
        ],
    }
