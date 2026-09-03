#!/usr/bin/env python3
"""A Docker API proxy that only passes the calls Flipside actually makes.

The web UI drives builds by starting sibling containers through the Docker
socket, and the socket was bind-mounted into it raw. That is the whole Docker
API: `exec` into any container on the host, read any container's environment,
create one with `/` bind-mounted, pull and run any image from anywhere. A web
application with a remote-code-execution bug and a raw Docker socket is a web
application with host root, and every security review says so.

This sits in front of it and passes an allowlist: the container lifecycle calls
the orchestrator makes, image builds, and nothing else. Requests are logged, so
what the UI asks the daemon to do is finally visible.

**What this does not fix, stated plainly.** The image builder must run
privileged -- it attaches loop devices, mounts filesystems and runs debootstrap
-- and a privileged container can reach the host kernel however it likes. So
anyone able to *start a build* can still reach host root by construction, and no
proxy can change that. What the allowlist removes is everything else: reaching
into unrelated containers on the same host, running an arbitrary image,
mounting host paths outside the project, and enumerating whatever else the host
is running. Treat the operator role as host-root-equivalent regardless, and give
Flipside its own host. See docs/DEPLOYMENT.md.

No dependencies: a security control whose supply chain is larger than the thing
it protects is a poor trade, and the whole of it should be readable in one
sitting.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import socketserver
import sys
import threading

UPSTREAM = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
LISTEN = os.environ.get("PROXY_SOCKET", "/shared/docker.sock")
# Host path the web UI is allowed to bind-mount from. Everything it legitimately
# mounts is inside the project directory or its output; anything else is a
# request to read or write part of the host, which is not something a build
# needs.
PROJECT_DIR = os.environ.get("HOST_PROJECT_DIR", "")
# Which container to ask when HOST_PROJECT_DIR is not set (see project_root).
WEBUI_CONTAINER = os.environ.get("WEBUI_CONTAINER", "debian-ab-webui")
# Image names a container may be created from. The builder and imager tags are
# built locally by the UI itself; the compose stack's images are built from the
# repo too. A container created from anything else is not a build.
IMAGE_ALLOW = re.compile(os.environ.get(
    "IMAGE_ALLOW",
    r"^(debian-ab-(builder|imager|webui|dnsmasq|http))(:[\w.\-]+)?$"))
# Only these images may be created with elevated privileges: the builder and the
# imager genuinely need loop devices and mounts. Nothing else does, and a
# privileged container is a host-root container.
PRIVILEGED_ALLOW = re.compile(r"^debian-ab-(builder|imager)(:[\w.\-]+)?$")

log = logging.getLogger("dockerproxy")

# (method, path pattern). Anchored, and matched against the path with its query
# string stripped -- a rule that matched a prefix would let `/containers/json`
# through as `/containers/json/../../whatever`, and the Docker API is happy to
# normalise that upstream.
#
# Deliberately absent, and each for a reason:
#   /containers/{id}/exec, /exec/*      running a command in any container
#   /containers/{id}/attach             the same, over a hijacked connection
#   /images/create                      pulling an arbitrary image from anywhere
#   /commit, /push                      exfiltrating a container as an image
#   /volumes, /networks (write)         reshaping what other containers can see
#   /secrets, /configs, /swarm, /nodes  cluster credentials
#   /plugins                            code loaded into the daemon itself
RULES: list[tuple[str, re.Pattern[str]]] = [
    ("GET",    re.compile(r"^/(v[\d.]+/)?_ping$")),
    ("HEAD",   re.compile(r"^/(v[\d.]+/)?_ping$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?version$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?info$")),

    # Containers: create, run, watch, clean up. `json` is the list the compose
    # plugin needs to find the stack's own containers.
    ("GET",    re.compile(r"^/(v[\d.]+/)?containers/json$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?containers/create$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+/json$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+/start$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+/stop$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+/kill$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+/wait$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+/restart$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+/logs$")),
    ("DELETE", re.compile(r"^/(v[\d.]+/)?containers/[\w.\-]+$")),

    # Images: build them, list them, and remove the ones we built.
    ("POST",   re.compile(r"^/(v[\d.]+/)?build$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?images/json$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?images/[\w.\-/:]+/json$")),
    ("DELETE", re.compile(r"^/(v[\d.]+/)?images/[\w.\-/:]+$")),

    # The compose plugin reads these to work out what it already has.
    ("GET",    re.compile(r"^/(v[\d.]+/)?networks$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?networks/[\w.\-]+$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?networks/create$")),
    # compose attaches its containers to the stack network after creating them.
    ("POST",   re.compile(r"^/(v[\d.]+/)?networks/[\w.\-]+/connect$")),
    ("POST",   re.compile(r"^/(v[\d.]+/)?networks/[\w.\-]+/disconnect$")),
    ("DELETE", re.compile(r"^/(v[\d.]+/)?networks/[\w.\-]+$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?volumes$")),
    ("GET",    re.compile(r"^/(v[\d.]+/)?events$")),
]

# Requests whose body has to be looked at before they are allowed through.
INSPECT = re.compile(r"^/(v[\d.]+/)?containers/create$")


class Denied(Exception):
    pass


def allowed(method: str, path: str) -> bool:
    bare = path.split("?", 1)[0]
    return any(m == method and p.match(bare) for m, p in RULES)


def check_create(body: bytes) -> None:
    """Validate a container-create payload, or raise Denied.

    This is where the proxy earns most of its keep. The path allowlist alone
    would let a compromised UI create a container from any image with `/`
    bind-mounted read-write, which is host root by a different door than the one
    that was just closed.
    """
    try:
        spec = json.loads(body or b"{}")
    except ValueError:
        raise Denied("container create body is not JSON")
    if not isinstance(spec, dict):
        raise Denied("container create body is not an object")

    image = str(spec.get("Image") or "")
    if not IMAGE_ALLOW.match(image):
        raise Denied(f"image {image!r} is not one this proxy will run")

    host = spec.get("HostConfig") or {}
    if not isinstance(host, dict):
        raise Denied("HostConfig is not an object")

    if host.get("Privileged") and not PRIVILEGED_ALLOW.match(image):
        raise Denied(f"{image!r} may not run privileged")

    # Binds arrive as "src:dst" or "src:dst:opts", and also as Mounts[] in
    # newer clients. Both are checked: allowing one and forgetting the other is
    # the whole hole, and the docker CLI picks between them by version.
    for bind in host.get("Binds") or []:
        source = str(bind).split(":", 1)[0]
        _check_source(source)
    for mount in host.get("Mounts") or []:
        if isinstance(mount, dict) and mount.get("Type") == "bind":
            _check_source(str(mount.get("Source") or ""))

    # A container in the host's PID or network namespace, or with the host's
    # devices, is not confined by any of the above.
    for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode"):
        if str(host.get(key) or "").startswith("host"):
            raise Denied(f"{key}=host is not allowed")
    if host.get("Devices") and not PRIVILEGED_ALLOW.match(image):
        raise Denied(f"{image!r} may not be given host devices")
    for cap in host.get("CapAdd") or []:
        if str(cap).upper() in ("ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE") \
                and not PRIVILEGED_ALLOW.match(image):
            raise Denied(f"{image!r} may not add {cap}")


_project_root_cache: list[str] = []


def project_root() -> str:
    """The host path the project lives at, discovered rather than configured.

    HOST_PROJECT_DIR wins if it is set, but it usually is not: the web UI works
    its own out by inspecting its container's mounts over the socket, precisely
    so nobody has to write the path down twice. This proxy asks the daemon the
    same question about the same container, so the two cannot disagree -- a
    hand-configured value that drifts from the real mount would refuse every
    legitimate build with a message about a path that looks correct.

    Cached after the first success. Before the web UI container exists there is
    nothing to ask, and until then binds are refused rather than guessed at.
    """
    if PROJECT_DIR:
        return PROJECT_DIR
    if _project_root_cache:
        return _project_root_cache[0]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect(UPSTREAM)
        with sock:
            sock.sendall(f"GET /containers/{WEBUI_CONTAINER}/json HTTP/1.1\r\n"
                         "Host: docker\r\nConnection: close\r\n\r\n".encode())
            raw = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        body = raw.partition(b"\r\n\r\n")[2]
        # Connection: close, so the daemon answers without chunking; if it did
        # chunk, json.loads fails and we fall through to refusing binds, which
        # is the safe direction.
        spec = json.loads(body)
        for mount in spec.get("Mounts") or []:
            if mount.get("Destination") == "/project" and mount.get("Source"):
                _project_root_cache.append(str(mount["Source"]))
                log.info("discovered project root %s from %s",
                         _project_root_cache[0], WEBUI_CONTAINER)
                return _project_root_cache[0]
    except (OSError, ValueError, KeyError) as exc:
        log.debug("could not discover the project root: %s", exc)
    return ""


def _check_source(source: str) -> None:
    if not source:
        return
    # The Docker socket, by any of its names. Handing it to a container is
    # handing that container everything this proxy exists to withhold.
    if source.rstrip("/").endswith("docker.sock"):
        raise Denied("the Docker socket may not be mounted into a container")
    if not source.startswith("/"):
        return                      # a named volume, not a host path
    configured = project_root()
    if not configured:
        raise Denied("the project's host path is not known yet, so no bind can "
                     "be checked against it; set HOST_PROJECT_DIR if it cannot "
                     "be discovered")
    root = os.path.normpath(configured)
    resolved = os.path.normpath(source)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise Denied(f"{source!r} is outside the project directory")


def read_headers(sock: socket.socket) -> tuple[bytes, str, str, dict[str, str]]:
    """Read up to the end of the request headers. Returns (raw, method, path, headers)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        # A request line and headers are small. Anything claiming otherwise is
        # not a Docker client, and buffering it would be the denial of service.
        if len(buf) > 256 * 1024:
            raise Denied("request headers are implausibly large")
    if not buf:
        raise Denied("empty request")
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2:
        raise Denied("malformed request line")
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return rest, parts[0], parts[1], headers


def relay(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        # Half-close so the other direction can finish: a build streams its
        # output long after the request body has ended.
        try:
            b.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def refuse(sock: socket.socket, reason: str, status: str = "403 Forbidden") -> None:
    body = json.dumps({"message": f"refused by the Flipside Docker proxy: {reason}"}).encode()
    sock.sendall(b"HTTP/1.1 " + status.encode() + b"\r\n"
                 b"Content-Type: application/json\r\n"
                 b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                 b"Connection: close\r\n\r\n" + body)


class Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        try:
            rest, method, path, headers = read_headers(client)
        except Denied as exc:
            log.warning("malformed request: %s", exc)
            try:
                refuse(client, str(exc), "400 Bad Request")
            except OSError:
                pass
            return
        except OSError:
            return

        bare = path.split("?", 1)[0]
        if not allowed(method, path):
            log.warning("DENY %s %s (not on the allowlist)", method, bare)
            try:
                refuse(client, f"{method} {bare} is not on the allowlist")
            except OSError:
                pass
            return

        body = b""
        if INSPECT.match(bare):
            length = int(headers.get("content-length") or 0)
            # Only ever a small JSON document. `docker build` sends the whole
            # build context as its body, which is why that path is relayed
            # without inspection rather than read into memory first.
            if length > 1024 * 1024:
                log.warning("DENY %s %s (create body of %d bytes)", method, bare, length)
                refuse(client, "container create body is implausibly large")
                return
            body = rest
            while len(body) < length:
                chunk = client.recv(min(65536, length - len(body)))
                if not chunk:
                    break
                body += chunk
            try:
                check_create(body)
            except Denied as exc:
                log.warning("DENY %s %s: %s", method, bare, exc)
                try:
                    refuse(client, str(exc))
                except OSError:
                    pass
                return
            rest = body

        log.info("ALLOW %s %s", method, bare)
        try:
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            upstream.connect(UPSTREAM)
        except OSError as exc:
            log.error("upstream %s unreachable: %s", UPSTREAM, exc)
            try:
                refuse(client, f"the Docker daemon is unreachable: {exc}",
                       "502 Bad Gateway")
            except OSError:
                pass
            return

        with upstream:
            # Replay the request exactly as it arrived. Reconstructing it from
            # the parsed pieces would drop headers the daemon cares about and
            # is a second parser to keep in step with the first.
            raw_head = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
            for key, value in headers.items():
                raw_head += f"{key}: {value}\r\n".encode("latin-1")
            raw_head += b"\r\n"
            try:
                upstream.sendall(raw_head + rest)
            except OSError:
                return
            up = threading.Thread(target=relay, args=(client, upstream), daemon=True)
            up.start()
            relay(upstream, client)
            up.join(timeout=5)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 128
    # Without this a restart hits "Address already in use" on the socket file,
    # which for a unix socket means a leftover inode rather than a busy port --
    # and the container then crash-loops on a file it could simply remove.
    allow_reuse_address = True


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    if not os.path.exists(UPSTREAM):
        log.error("no Docker socket at %s", UPSTREAM)
        return 1
    if not PROJECT_DIR:
        log.info("HOST_PROJECT_DIR is unset; the project root will be read from "
                 "%s's own mounts on the first bind that needs checking.",
                 WEBUI_CONTAINER)
    try:
        os.unlink(LISTEN)
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(LISTEN), exist_ok=True)
    server = Server(LISTEN, Handler)
    # The web UI runs as root in its container today, but the socket should not
    # depend on that staying true; 0660 with the shared group is enough.
    os.chmod(LISTEN, 0o660)
    log.info("listening on %s, forwarding allowlisted calls to %s", LISTEN, UPSTREAM)
    log.info("images allowed: %s", IMAGE_ALLOW.pattern)
    log.info("privileged allowed for: %s", PRIVILEGED_ALLOW.pattern)
    log.info("host binds confined to: %s",
             PROJECT_DIR or f"(discovered from {WEBUI_CONTAINER} on first use)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
