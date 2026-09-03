import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app import audit, metrics, oidc, sessions, users
from app.config import settings
from app.security import Principal, client_ip, oauth2_scheme, require_admin, require_viewer

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/methods")
async def methods():
    """What the login page should render. Open on purpose: it names ways to
    log in, which the login form itself already reveals."""
    return {"password": True,
            "oidc": {"enabled": oidc.enabled(),
                     "display_name": settings.oidc_display_name}}

# Sliding-window throttle on failed logins, keyed by username+IP so one
# guessed-at account does not lock the console for everyone else, and one
# address hammering many usernames still pays per pair it tries.
_MAX_FAILURES = 5
_WINDOW_SECONDS = 60
_MAX_KEYS = 10000
_failures: dict[str, deque[float]] = {}


def _throttled(key: str, now: float) -> bool:
    window = _failures.get(key)
    return (window is not None and len(window) == _MAX_FAILURES
            and now - window[0] < _WINDOW_SECONDS)


def _note_failure(key: str, now: float) -> None:
    # Bounded: someone spraying random usernames grows this dict, and an auth
    # throttle that can exhaust memory is a worse denial of service than the
    # logins it throttles.
    if len(_failures) >= _MAX_KEYS and key not in _failures:
        _failures.clear()
    _failures.setdefault(key, deque(maxlen=_MAX_FAILURES)).append(now)


@router.post("/login")
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    username = form.username.strip().lower()
    ip = client_ip(request)
    now = time.monotonic()
    if _throttled(f"{username}|{ip}", now):
        metrics.inc("flipside_login_attempts_total", outcome="throttled")
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many failed logins; try again in a minute")
    rec = users.verify(username, form.password)
    if not rec:
        _note_failure(f"{username}|{ip}", now)
        # The attempted username is worth keeping; the password never is.
        audit.record(actor=username or "-", role="", method="POST",
                     path="/api/auth/login", status=401, ip=ip,
                     summary="login failed")
        metrics.inc("flipside_login_attempts_total", outcome="failure")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Incorrect username or password")
    metrics.inc("flipside_login_attempts_total", outcome="success")
    token = sessions.create(rec["username"], ip=ip)
    audit.record(actor=rec["username"], role=rec.get("role", ""), method="POST",
                 path="/api/auth/login", status=200, ip=ip, summary="logged in")
    return {"access_token": token, "token_type": "bearer",
            "username": rec["username"], "role": rec.get("role", "viewer")}


@router.get("/check")
async def check(principal: Principal = Depends(require_viewer)):
    # The UI reads its role from here, so what it offers and what the API
    # enforces cannot drift apart.
    return {"ok": True, "username": principal.name, "role": principal.role}


@router.post("/logout")
async def logout(request: Request, token: str = Depends(oauth2_scheme),
                 principal: Principal = Depends(require_viewer)):
    # Revokes the presented session server-side; forgetting the token in the
    # browser alone would leave it valid for whoever else has seen it.
    revoked = sessions.revoke_raw(token)
    request.state.audit_summary = "logged out"
    return {"ok": revoked}


@router.get("/sessions")
async def list_sessions(token: str = Depends(oauth2_scheme),
                        _: Principal = Depends(require_admin)):
    return {"sessions": sessions.list_sessions(current_raw=token)}


@router.delete("/sessions/{session_id}")
async def revoke_session(session_id: str, request: Request,
                         _: Principal = Depends(require_admin)):
    if not sessions.revoke_id(session_id):
        raise HTTPException(404, "No such session")
    request.state.audit_summary = f"revoked session {session_id}"
    return {"revoked": session_id}
