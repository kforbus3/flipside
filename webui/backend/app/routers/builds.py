import json
import os

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app import orchestrator as orch
from app import secretstore
from app.jobs import _ProgressEvent as ProgressEvent, jobs
from app.security import (Principal, create_stream_token, require_operator,
                          require_viewer, verify_stream_token)

router = APIRouter(tags=["builds"])

# The 1 MiB BIOS-GRUB partition plus alignment padding, and a floor so the
# overlay partition isn't degenerate.
_BOOT_MIB = 512
_ESP_MIB = 128
_MIN_OVERLAY_MIB = 256
# Mirrors build-image.sh: Ubuntu's linux-image-generic hard-depends on
# linux-firmware and linux-modules-extra, which Debian never installs. The
# builder raises the slot to this floor, so validate against the same number.
_MIN_ROOT_MIB = {"ubuntu": 5120, "debian": 2560}

# Architectures the builder can produce a bootable image for. amd64 keeps the
# hybrid BIOS+UEFI boot; arm64 is UEFI-only. Anything else would build and then
# fail to boot, so it is rejected here rather than discovered on the bench.
_ARCHES = ("amd64", "arm64")


def _require_ready() -> None:
    """Fail a build request up front, with the fix, rather than deep in the log."""
    problems = orch.preflight()
    if problems:
        raise HTTPException(503, " ".join(problems))


def _validate_build(opts: dict) -> None:
    arch = opts.get("arch", "amd64")
    if arch not in _ARCHES:
        raise HTTPException(400, f"arch must be one of {', '.join(_ARCHES)}")
    # Profile and desktop environment, refused here — before a job exists —
    # with the choices that ARE available, exactly as build-image.sh refuses
    # them. The mapping lives in the orchestrator so this and the build
    # command can never disagree about what is buildable.
    profile = str(opts.get("profile") or "minimal")
    if profile not in orch.PROFILES:
        raise HTTPException(400, f"profile must be one of {', '.join(orch.PROFILES)}")
    secure_boot = str(opts.get("secure_boot") or "auto")
    if secure_boot not in orch.SECURE_BOOT_MODES:
        raise HTTPException(
            400, f"secure_boot must be one of {', '.join(orch.SECURE_BOOT_MODES)}")
    # `on` on arm64 would reach the builder and fail there, an hour into a
    # debootstrap, over a package name. Refused here, with the reason.
    if secure_boot == "on" and arch not in ("amd64", "arm64"):
        raise HTTPException(400, f"no signed shim is packaged for {arch}")
    desktop = str(opts.get("desktop") or "").strip()
    if desktop and profile != "desktop":
        raise HTTPException(
            400, "a desktop environment only means anything with profile=desktop "
                 f"(profile is '{profile}')")
    min_root = _MIN_ROOT_MIB.get(opts.get("distro", "debian"), 2560)
    if profile == "desktop":
        distro = opts.get("distro", "debian")
        envs = orch.DESKTOP_ENVS.get(distro)
        if not envs:
            raise HTTPException(400, "distro must be debian or ubuntu")
        if (desktop or "gnome") not in envs:
            raise HTTPException(
                400, f"no '{desktop}' desktop for {distro} — "
                     f"available: {', '.join(envs)}")
        # The builder raises the slot to its desktop floor, so validate the
        # image size against the slot it will actually build.
        min_root = max(min_root, orch.MIN_ROOT_DESKTOP_MIB)
    size = opts.get("image_size", "auto")
    try:
        # 0 / "auto" = smallest possible; the image expands on first boot.
        image_mib = 0 if size in ("auto", 0, "0", "", None) else int(size) * 1024
        root_mib = max(int(opts.get("root_size", 3072)), min_root)
    except (TypeError, ValueError):
        raise HTTPException(400, "image_size and root_size must be numbers (or image_size 'auto')")
    if root_mib < 1024:
        raise HTTPException(400, "root_size must be at least 1024 MiB")
    need = 2 * root_mib + _BOOT_MIB + _ESP_MIB + 2 + _MIN_OVERLAY_MIB
    if image_mib and image_mib < need:
        raise HTTPException(
            400,
            f"image_size too small: two {root_mib} MiB root slots + boot + overlay "
            f"need at least {need} MiB (≈{-(-need // 1024)} GiB)",
        )
    if opts.get("encrypt"):
        # store_passphrase means "generate one and file it in the secrets
        # manager", which is the other way of satisfying this requirement.
        if not opts.get("luks_passphrase") and not opts.get("store_passphrase"):
            raise HTTPException(400, "encrypt requires a LUKS passphrase")
        if opts.get("store_passphrase") and not secretstore.is_configured():
            raise HTTPException(
                400, "no secrets manager is configured — set one up under Secrets "
                     "Manager, or enter a LUKS passphrase for this build")
        if opts.get("unlock") == "tang" and not opts.get("tang_url"):
            raise HTTPException(400, "unlock=tang requires a Tang URL")


@router.get("/preflight")
async def preflight(_: Principal = Depends(require_viewer)):
    """Whether the UI can actually drive the builder, and what to fix if not."""
    problems = orch.preflight()
    return {"ready": not problems, "problems": problems,
            "host_project_dir": orch.host_project_dir()}


# GET /overlay and the rest of the file management it grew into live in
# routers/overlay.py.


@router.post("/builds")
async def start_build(opts: dict = Body(...), _: Principal = Depends(require_operator)):
    _require_ready()
    _validate_build(opts)
    if jobs.running(type="image"):
        raise HTTPException(409, "An image build is already running")
    # Settle the output name before anything is keyed on it. Both the passphrase
    # filing below and the build command derive from it, and they must not be
    # able to disagree -- resolve_output_name writes the answer into opts so
    # they read rather than recompute.
    try:
        out_name = orch.resolve_output_name(opts)
    except orch.NameInUse as exc:
        raise HTTPException(409, {
            "detail": f"An image named {exc.name} is already in the library. "
                      f"Building over it would replace the image a deployed machine "
                      f"was made from, and with it the LUKS passphrase filed under "
                      f"that name.",
            "name": exc.name,
            "suggestion": exc.suggestion,
        })
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    # The customization script travels through the output directory, which the
    # builder already mounts. Written before the job starts so a failure to
    # write it is reported here rather than as a confusing build error.
    script = str(opts.get("run_script") or "").strip()
    script_path = os.path.join(orch.settings.output_dir, ".build-script.sh")
    try:
        if script:
            if not script.startswith("#!"):
                script = "#!/bin/bash\nset -euo pipefail\n" + script
            with open(script_path, "w") as f:
                f.write(script if script.endswith("\n") else script + "\n")
            os.chmod(script_path, 0o755)
        elif os.path.exists(script_path):
            os.remove(script_path)
    except OSError as exc:
        raise HTTPException(500, f"could not stage the customization script: {exc}")
    stored_at = await _store_generated_passphrase(opts)
    cmd, label, env = orch.build_image_cmd(opts)
    job = await jobs.start(type="image", label=label, cmd=cmd, now=orch.now(), env=env)
    # The name is returned because the build may not have got the one that was
    # asked for: with no name given, a free one is chosen rather than replacing
    # what is already there, and the operator should see which.
    return {**job.public(), "passphrase_stored_at": stored_at, "image_name": out_name}


async def _store_generated_passphrase(opts: dict) -> str:
    """Generate this build's LUKS passphrase and file it, before anything runs.

    Order is the whole point. Storing after a successful build would mean a
    write that fails -- an expired token, a sealed store, a network blip -- has
    already produced an encrypted image nobody holds the recovery key for.
    Storing first can only leave an unused secret behind if the build then
    fails, which costs nothing and is visible in the store's own listing.

    The passphrase is put back into `opts` and travels to the builder in
    LUKS_PASS like any other, so the builder needs no store access of its own.
    """
    if not (opts.get("encrypt") and opts.get("store_passphrase")):
        return ""
    image = orch.image_output_name(opts)
    passphrase = secretstore.generate_passphrase()
    meta = {
        "distro": opts.get("distro", "debian"),
        "suite": opts.get("suite", "trixie"),
        "arch": opts.get("arch", "amd64"),
        "hostname": opts.get("hostname", ""),
        "unlock": opts.get("unlock", "keyfile"),
    }
    try:
        path = await run_in_threadpool(secretstore.store_passphrase, image, passphrase, meta)
    except secretstore.SecretStoreError as exc:
        raise HTTPException(
            502, f"the build was not started: its LUKS passphrase could not be stored "
                 f"in the secrets manager ({exc}). Nothing is built until the recovery "
                 f"key has somewhere to live.")
    opts["luks_passphrase"] = passphrase
    return path


@router.post("/imager/build")
async def start_imager(body: dict = Body(default={}), _: Principal = Depends(require_operator)):
    _require_ready()
    if jobs.running(type="imager"):
        raise HTTPException(409, "An imager build is already running")
    arch = str(body.get("arch") or "amd64")
    if arch not in _ARCHES:
        raise HTTPException(400, f"arch must be one of {', '.join(_ARCHES)}")
    cmd, label = orch.build_imager_cmd(arch)
    job = await jobs.start(type="imager", label=label, cmd=cmd, now=orch.now())
    return job.public()


@router.get("/jobs")
async def list_jobs(_: Principal = Depends(require_viewer)):
    return jobs.list()


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, _: Principal = Depends(require_viewer)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {**job.public(), "log": jobs.log_text(job)}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: Principal = Depends(require_operator)):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    await jobs.cancel(job)
    return job.public()


@router.get("/jobs/{job_id}/stream-token")
async def stream_token(job_id: str, principal: Principal = Depends(require_viewer)):
    if not jobs.get(job_id):
        raise HTTPException(404, "Job not found")
    return {"token": create_stream_token(job_id, subject=principal.name)}


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str, token: str = ""):
    # EventSource cannot set Authorization headers; a short-lived scoped token
    # (from /stream-token) authorizes exactly this job's stream.
    verify_stream_token(token, job_id)
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    async def gen():
        async for item in jobs.subscribe(job):
            if isinstance(item, ProgressEvent):
                yield f"event: progress\ndata: {json.dumps(item.data)}\n\n"
            else:
                yield f"data: {item}\n\n"
        yield f"event: end\ndata: {job.status}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
