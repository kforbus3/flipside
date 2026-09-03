# Web Management UI

A browser-based control panel that ties the whole system together — build images,
manage the image library, configure and run the provisioning server, and **watch
machines get imaged live**. It orchestrates the builder/imager/server containers
through the Docker socket.

## Features

- **Build wizard** — pick distribution (Debian or Ubuntu), release, hostname,
  user, sizes, compression, and extra packages — plus a **Profile**
  (Minimal / Server / Desktop) and, for Desktop, an environment select whose
  options follow the chosen distro — then start a build and watch a
  **progress bar and live log** in the browser (with cancel). The bar tracks 14
  named phases reported by the builder, so a long step like debootstrap or
  compression is identifiable rather than just slow. Navigating away and back
  reattaches to the running build.
- **One-click imager build**, for the architecture selected in the build form.
  The imager is a kernel the target machine executes, so an amd64 imager cannot
  netboot an arm64 machine; the page says so when the one you need is missing.
- **Image library** — list, download, and delete built images, with distro/
  release/encryption metadata and SHA256 for each, and a **Deploy** button that
  points the provisioning server at an image.
- **Image files** — create, upload, edit, move and delete the files copied into
  every image, from the browser. See below.
- **Job history** — past builds and their full logs survive UI restarts
  (persisted under `output/jobs/`).
- **Turnkey provisioning** — the UI lists the host's network interfaces; pick the
  one facing the machines and the server IP, subnet and DHCP lease range are
  derived from it. DHCP and TFTP are bound to that interface alone, so the
  imaging network is self-contained and the host's other networks never see it.
  A readiness check blocks Start until the imager is built and an image chosen.
- **Per-machine images** — assign specific machines a specific image by MAC,
  with an optional label and its own post-imaging action. Everything else on the
  switch gets the default image. Machines already seen by the monitor have an
  *assign image…* link, so you can plug in a fleet and target from what shows up
  rather than collecting MAC addresses first.
- **Imaging** — a live view of machines writing an image right now, with per-machine
  progress reported by the imager itself. A machine that finishes drops off shortly
  after; one that stops reporting is marked stalled and then removed, so the page
  only ever shows current work. The Provisioning page's list is the narrower
  question of who is on the network and still needs an image. If machines show
  `booting imager` there but never reach this page, the imager's reports are not
  arriving — see `WEBUI_ADDR` in [DEPLOYMENT.md](DEPLOYMENT.md).
- **Fleet** — every machine this server has imaged, kept on disk and never expired.
  Machines report once when imaging finishes and again when they boot the image, so
  a machine that imaged and then failed to boot shows as **never-booted** rather
  than being indistinguishable from a success.
- **Updates** — build a signed RAUC bundle from an image you have already built,
  and see which versions the fleet is running. Installing a bundle writes the slot
  a machine is not running on and reboots into it, with automatic rollback if that
  slot fails to come up. Bundles can be deleted here too; the one marked **latest**
  is what a machine running plain `ab-update` installs, and deleting it moves that
  pointer rather than leaving the fleet fetching a file that is gone. See
  [UPDATES.md](UPDATES.md).
- **Secrets manager** *(optional)* — connect OpenBao or HashiCorp Vault and have
  encrypted builds generate their own LUKS recovery passphrase and file it under
  the image's name, instead of somebody inventing one and keeping it in a note.
  See below.
- **Disk usage** at a glance on the dashboard.

## Image files

`overlay.d/` holds your own files — the ones copied over the image's root
filesystem, keeping their paths, so `/etc/hosts` here is `/etc/hosts` on every
machine imaged from it. It is a directory in the repository, and it is also
editable from the **Image Files** page: create a file and type its contents,
upload one, change its mode, move it, or remove it.

The same manager opens in a dialog from the Build page's *Customize the
filesystem* panel, so files can be added mid-build without losing the form.

Worth knowing:

- **The mode is part of the file.** It is preserved into the image, so a script
  shipped `0644` is a script that does not run on the machine — nothing warns
  you, it simply sits there. The list has a one-click 0644/0755 toggle, and the
  editor flags a path that looks like a program but is not executable.
- **Deleting the last file in a directory removes the directory too.** An empty
  `overlay.d/etc/netplan` would otherwise be copied into every image as an empty
  `/etc/netplan`, which for netplan means a machine that boots with no network
  configuration at all.
- **Binary files are fine** — upload them and they are copied verbatim. They
  just cannot be edited in the browser; download them instead.
- Files over 1 MiB are shipped but not editable inline. Uploads are capped at
  256 MiB.
- Nothing here is committed: the directory is gitignored except its README.

If the repository is mounted read-only into the UI container, the page says so
and falls back to showing the host path — the files are the same directory
either way.

## Secrets manager

An encrypted image needs a LUKS passphrase, and for every unlock method except
`passphrase` it is *only* a recovery key — machines unlock from the TPM, from
Tang, or from a keyfile, so nothing types it again until a TPM is cleared by a
firmware update and a machine stops at the initramfs prompt.

Under **Secrets Manager**, point the UI at an OpenBao or HashiCorp Vault KV v2
mount (token or AppRole; namespace, private CA and mount point are all
configurable). Then, on an encrypted build, tick *Generate a random passphrase
and store it* — on by default for `tpm2`, `tang` and `keyfile`, off for
`passphrase`, which somebody has to type at every boot.

What happens then:

- The backend generates 256 bits of randomness and **writes it to the store
  before the build starts**. If the store will not take it, no image is built —
  the reverse order can leave an encrypted image whose recovery key was never
  persisted, and that is not recoverable.
- The passphrase reaches the builder in `LUKS_PASS`, as a typed one does. **The
  builder container never talks to the store**, so nothing privileged gains
  network access or a store credential.
- Building an **update bundle** from that image reads the passphrase back
  automatically instead of prompting for it.
- Encrypted images with a stored passphrase show a key icon in the image library;
  clicking it reveals the passphrase, because the moment you need it is a machine
  stopped at an initramfs prompt.

Two things to get right:

- **The credential needs write access**, not just read. A read-only policy passes
  the connection test and fails the first build.
- **Rebuilding the same image name files a new passphrase.** KV v2 keeps the old
  version and machines already imaged still need it — which is why a KV v1 mount
  is rejected.

The store token can be set as `BAO_TOKEN` (or `VAULT_TOKEN`) in `webui/.env`
instead of through the UI; it then takes precedence and never lands in the app's
own config file. Everything saved through the UI goes to
`output/.secrets-store.json`, mode 0600.

The same thing is available on the command line via `scripts/luks-secret.sh` —
see [BUILDER.md](BUILDER.md#storing-the-passphrase-in-a-secrets-manager).

## Users, roles, and tokens

The UI has named users with three roles, revocable sessions, API tokens for
automation, and an audit log. All of it lives in files under `output/` — no
database.

**Bootstrap.** On the first start with no `output/users.json`, the
`ADMIN_PASSWORD` environment variable becomes the password of a user named
`admin` (role admin), stored as a bcrypt hash. Logging in as
`admin`/`ADMIN_PASSWORD` works exactly as it always has. Once `users.json`
exists, **`ADMIN_PASSWORD` is ignored** — deliberately not resynced on later
boots, so a password changed in the UI is never silently reverted by an
environment variable. To start over, stop the UI and delete
`output/users.json`; the next start bootstraps again. Compose still requires
`ADMIN_PASSWORD` to be set, so a fresh deployment can never come up with a
default credential.

**Roles.** Ordered `viewer` < `operator` < `admin`, enforced per endpoint:

- **viewer** — sees everything (dashboard, images, jobs and their logs, fleet,
  bundles), changes nothing. In the UI, mutating controls are disabled with a
  tooltip; the API answers 403 regardless.
- **operator** — everything a viewer can, plus the work: build images and
  bundles, delete them, manage image files, start/stop the provisioning
  server, edit per-machine assignments, cancel jobs.
- **admin** — everything, plus what changes who can do what: users, API
  tokens, sessions, the audit log, the secrets manager, and the provisioning
  server's configuration file.

The last enabled admin cannot be deleted, demoted or disabled — the refusal
says so — because a system with no admin can never manage users again.

**Sessions** are opaque `fls_…` tokens; the server stores only their SHA-256
in `output/.sessions.json`, so the state file cannot be replayed as a
session. They slide 12 hours on each use, end 7 days after login regardless,
survive a backend restart, and are revocable: **Log out** revokes the one you
hold, and admins can list and revoke anyone's under **Sessions**. Disabling a
user or resetting a password revokes that user's sessions immediately.

**API tokens** are for automation — CI, scripts, cron. Create one under
**API Tokens** (admin): pick a name, a role no higher than needed, and
optionally an expiry. The raw token (`flt_…`) is shown **once**, at creation;
only its hash is stored (`output/.api-tokens.json`). It goes in the same
header the browser uses:

```bash
curl -H "Authorization: Bearer flt_…" http://localhost:8080/api/images
curl -H "Authorization: Bearer flt_…" -X POST \
     -H "Content-Type: application/json" -d '{"image": "debian-trixie-amd64-ab.img"}' \
     http://localhost:8080/api/bundles/build
```

Tokens act under their own name (`token:<name>` in the audit log), with their
own role, and are revocable individually — so a leaked pipeline credential is
one revocation, not a password rotation for everybody.

**Audit log.** Every mutating API call, every login (success and failure —
the attempted username is kept, the password never), every user/token/session
change, and every reveal of a stored LUKS passphrase is appended to
`output/audit.jsonl` with timestamp, actor, role, method, path, outcome and
source IP. Admins can browse and filter it under **Audit Log**, or `grep` the
file directly; it trims itself oldest-first past ~20,000 events.

## Single sign-on (OIDC)

The UI can authenticate against any conforming OpenID Connect provider —
Keycloak, Authentik, Entra ID, Okta — using the authorization-code flow with
PKCE. It is **entirely off** unless both `OIDC_ISSUER` and `OIDC_CLIENT_ID`
are set, and password login keeps working either way: an IdP that is down or
misconfigured only breaks the SSO button, never the login form or the rest of
the app.

Underneath, nothing changes. A successful SSO login produces an ordinary
`users.json` record (with `source: "oidc"` and **no** password hash — such a
record can never log in with a password) and mints the same opaque, revocable
`fls_…` session a password login does. Roles, session expiry, revocation and
the audit log all behave identically.

**Environment reference** (set in `webui/.env`; compose passes them through):

| Variable | Default | Meaning |
| --- | --- | --- |
| `OIDC_ISSUER` | *(unset — SSO off)* | Issuer URL, e.g. `https://auth.example.com/realms/lab`. Discovery is fetched lazily from `<issuer>/.well-known/openid-configuration` and cached for an hour. |
| `OIDC_CLIENT_ID` | *(unset — SSO off)* | The client registered at the IdP. |
| `OIDC_CLIENT_SECRET` | *(empty)* | Optional. Empty means a public client — valid, because the flow always uses PKCE. |
| `OIDC_ROLE_CLAIM` | `groups` | The ID-token claim holding the user's groups. |
| `OIDC_ROLE_MAP` | *(empty)* | Comma list of `idp-group=role`, e.g. `flipside-admins=admin,flipside-ops=operator,flipside-view=viewer`. A user in several mapped groups gets the **highest** role. |
| `OIDC_DEFAULT_ROLE` | `deny` | What a user whose groups map to nothing gets: `deny` refuses them (audited); `viewer`/`operator`/`admin` admits everyone the IdP vouches for, at that role. |
| `OIDC_DISPLAY_NAME` | `Single sign-on` | Label on the login page's SSO button. |
| `OIDC_SCOPES` | `openid profile email` | Scopes requested. |

**Admission is deny-by-default on purpose.** Being known to the IdP is
authentication, not authorization: with `OIDC_DEFAULT_ROLE=deny`, a user none
of whose groups appear in `OIDC_ROLE_MAP` is refused, the login page says so,
and the refusal lands in the audit log. Set a default role only if everyone
the IdP admits really should reach a UI that controls the Docker socket. The
mapped role is re-resolved **on every login**, so a group change at the IdP
propagates at the user's next sign-in — including downward. (A role changed
by hand on the Users page is likewise overwritten at their next login; for
SSO users the IdP's groups are authoritative.)

**Local users cannot be taken over.** If the IdP asserts a username that
already exists here as a local (password) user, the SSO login is **refused
and audited** — never merged. Anything else would let whoever controls that
username at the IdP inherit the local account's rank. Rename one of the two
to resolve it. Disabling an SSO user on the Users page also wins over the
IdP: they stay refused until re-enabled, even with a valid assertion. The
usernames themselves come from `preferred_username` (falling back to the
email's local part), lowercased and held to the same validation as local
usernames.

**Redirect URI.** The callback is `/api/auth/oidc/callback` on whatever
origin the browser used, honoring `X-Forwarded-Proto`/`X-Forwarded-Host` —
so behind the TLS reverse proxy from the [security note](#security-note)
below, the redirect URI to register at the IdP is
`https://webui.example.com/api/auth/oidc/callback`.

**Worked example: Keycloak.** In a realm named `lab`:

1. *Clients → Create client*: type OpenID Connect, client ID `flipside`,
   standard flow on, valid redirect URI
   `https://webui.example.com/api/auth/oidc/callback`. Either leave it public
   (no secret; PKCE covers it) or enable client authentication and copy the
   secret.
2. Keycloak does not put groups in the ID token by default. On the client
   (or a client scope), add a *Group Membership* mapper: token claim name
   `groups`, full group path **off**, *Add to ID token* **on**.
3. Create groups `flipside-admins`, `flipside-ops`, `flipside-view` and put
   people in them.
4. In `webui/.env`:

```bash
OIDC_ISSUER=https://auth.example.com/realms/lab
OIDC_CLIENT_ID=flipside
OIDC_CLIENT_SECRET=            # empty for a public client
OIDC_ROLE_MAP=flipside-admins=admin,flipside-ops=operator,flipside-view=viewer
```

Restart the UI (`docker compose up -d`) and the login page grows the SSO
button.

Other IdPs differ mainly in where the groups claim comes from: **Entra ID**
sends group **object GUIDs** (map the GUIDs, or configure the app to emit
group names), and needs `groupMembershipClaims` enabled on the app
registration; **Okta** needs a Groups claim filter on the authorization
server; **Authentik** includes `groups` with its default scope mappings.

## Metrics, logs, and the audit trail

### Prometheus

`GET /metrics` (also `/api/metrics`) in the standard exposition format. It needs
a **viewer** credential by default — create a viewer API token on the Tokens
page and give it to Prometheus:

```yaml
scrape_configs:
  - job_name: flipside
    authorization:
      credentials: flt_...
    static_configs:
      - targets: ["flipside.example.com:8080"]
```

Set `METRICS_PUBLIC=true` to drop the requirement. That is a real decision, not
a formality: `/metrics` names the versions running in the field, how many
machines there are, and how each live rollout is going — a fair map of the
estate for anyone who can read it.

What is worth alerting on:

| metric | why |
| --- | --- |
| `flipside_rollouts{state="halted"}` | a rollout stopped itself on its failure budget |
| `flipside_fleet_machines{presence="offline"}` | machines that have stopped checking in |
| `flipside_fleet_degraded` | machines whose last check-in reported a failed unit |
| `flipside_never_booted` | imaged and never heard from — the imager cannot see this |
| `flipside_disk_free_bytes` | images and bundles fill a disk quietly |
| `flipside_audit_last_success_seconds` | audit forwarding has stopped arriving |

Label values never come from user data. Request paths are bucketed
(`/api/fleet/hosts/:id`), because one time series per machine id is how a
metrics endpoint takes down the Prometheus scraping it — and a fleet checking in
every five minutes is exactly the shape that does it.

`GET /api/metrics.json` returns the same headline numbers as an object, for
anyone not running Prometheus.

### Structured logs

`LOG_JSON=true` switches stdout to one JSON object per line, applied to
uvicorn's loggers as well as the application's — configuring only one produces
structured lines interleaved with human-formatted access lines, which is neither
parseable nor readable. `LOG_LEVEL` takes the usual names.

Prose stays the default: `make webui-logs` to watch a build is better read as
prose, and turning this on is the deliberate act of someone who has somewhere to
send it.

### Shipping the audit trail off the box

`output/audit.jsonl` is bounded and trimmed oldest-first, so it is a buffer
rather than an archive — and it lives on the machine being audited, which is the
copy that goes when the disk goes, and the copy someone with root deletes.

Point it somewhere else:

```
AUDIT_SYSLOG=udp://siem.example.com:514      # or tcp://host:601
AUDIT_HTTP_URL=https://collector.example.com/flipside
AUDIT_HTTP_TOKEN=...                          # sent as Authorization: Bearer
```

Events go out as RFC 5424 with the event itself as a JSON message (every
collector parses a JSON body; half of them mangle SD-PARAMs), or as one JSON
object POSTed per event. Both can be on at once.

**Delivery never blocks a request.** The forwarder sits behind every mutating
API call, so a collector that is slow or wedged must not make this server slow
or wedged with it. Events go onto a bounded queue and one background thread
sends them; when the queue is full the *oldest* is dropped and a counter goes
up, because a full queue means the collector has been unreachable for a while
and the recent events are the ones worth keeping.

Failures are visible rather than silent — "nothing is arriving" and "nothing
happened" look identical at the collector, so alert on
`flipside_audit_last_success_seconds` rather than on the queue depth, which is
empty both when everything is fine and when nothing is being sent at all.

## Running it

```bash
cp webui/.env.example webui/.env
# Edit it:
#   ADMIN_PASSWORD — password the `admin` user is created with on first start
#   SECRET_KEY     — random string
make webui
```

Open **http://localhost:8080** and log in as **`admin`** with `ADMIN_PASSWORD`.
After the first start the users file is authoritative — see
[Users, roles, and tokens](#users-roles-and-tokens).

No path configuration is needed. Compose mounts the repository root at `/project`
in the UI container, and the UI asks the Docker daemon where that mount came from
on the host — which is what the builder/imager containers need for their own bind
mounts. `HOST_PROJECT_DIR` in `webui/.env` only overrides that detection; set it
if you run `docker compose` from outside the `webui/` directory, and then it must
be the absolute host path of *this* checkout. A stale value mounts an empty
directory and builds fail with `unable to prepare context: path
"/project/builder" not found` — so the Dashboard and Build pages check this on
load and show what to fix.

## How it works

```
browser ─▶ webui (FastAPI + React)
              │  reads ./output, writes server/.env
              └─ docker socket ─▶ builder / imager / server containers
                                   (live logs streamed back via SSE)
```

- The backend launches `docker build` + `docker run` for the builder/imager and
  `docker compose` for the provisioning server, streaming combined output to the
  browser over Server-Sent Events.
- Authentication is named users with roles (viewer/operator/admin), revocable
  sessions and API tokens — see [Users, roles, and tokens](#users-roles-and-tokens).
  Still: run the UI only on a trusted network — it has full control of the
  Docker host.
- FastAPI's interactive API documentation is live at `/docs` (Swagger UI), with
  `/redoc` and the raw schema at `/openapi.json`. The schema is readable without
  logging in; every endpoint it describes requires auth.

## Developing the UI

The backend runs directly with uvicorn — no Docker needed for API work. It
wants the same environment the container gets, plus a `PROJECT_DIR` pointing at
a scratch directory (it defaults to `/project`, the container mount, which does
not exist on your machine):

```bash
cd webui/backend
pip install -r requirements.txt
PROJECT_DIR=$(mktemp -d) ADMIN_PASSWORD=dev SECRET_KEY=dev-secret \
  uvicorn app.main:app --reload --port 8080
```

For the frontend, `npm run dev` in `webui/frontend` starts Vite with a proxy
that forwards `/api` to the backend, so both halves reload live:

```bash
cd webui/frontend
npm ci
npm run dev
```

## Security note

The UI container mounts the Docker socket, which is equivalent to root on the
host. Restrict access to the UI (strong passwords, least-role accounts, trusted
network only, ideally behind a TLS reverse proxy).

The smallest working TLS front is [Caddy](https://caddyserver.com/), which
obtains and renews the certificate itself. A complete `Caddyfile`:

```
webui.example.com {
    reverse_proxy localhost:8080
}
```

If the host has several networks, also bind the compose port to the management
interface rather than every address — in `webui/docker-compose.yml`, publish
`<management IP>:8080:8080` instead of `8080:8080` — so the UI (and the login
form in front of the Docker socket) is not reachable from the imaging segment
or anywhere else it has no business being.
