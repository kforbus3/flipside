# Security

## Imaging is destructive — control the network

The provisioning server will re-image **any** machine that PXE-boots from it. Run
it only on a network (or isolated switch) where every PXE-booting machine is meant
to be wiped and re-imaged. Prefer `MODE=proxy` on shared LANs (it only answers PXE
requests) and restrict it to one interface with `INTERFACE=`.

## Image credentials

- The image is built with a username/password you pass in. **Use a strong
  password** and change it after first boot, or use `--ssh-pubkey` for key-only
  access and a throwaway password.
- `root` is locked; administration is via the sudo user.
- Don't commit images — they contain the password hash. `.gitignore` excludes
  `output/` and `*.img*`.

## RAUC signing keys

- Update bundles are GPG/x509-signed. **Keep the CA and signing private keys
  off the device and out of git.** Only the CA *certificate* (`keyring.pem`) ships
  in the image.
- `.gitignore` excludes `*.pem`, `*.key`, `*.crt`, and `certs/`.

## Network transport

- PXE/TFTP and the image are served over plain HTTP on the local segment — fine
  for a trusted provisioning LAN. Do not expose the provisioning server to
  untrusted networks.
- The builder publishes a `<image>.sha256` next to each image and the imager
  verifies the download against it before rebooting, so a truncated or corrupted
  transfer fails loudly instead of producing a broken machine. (This is an
  integrity check against accidents, not an authenticity check — HTTP is
  unauthenticated; control the network.)
- The web UI passes build secrets (login password, LUKS passphrase) to the
  builder via the process environment, not command-line arguments, so they don't
  appear in `ps` or persisted job records. They remain visible to anyone with
  Docker access (`docker inspect` on a running build) — which is root-equivalent
  anyway.
- The web UI has named users with roles (`viewer` < `operator` < `admin`).
  `ADMIN_PASSWORD` only seeds the initial `admin` user on the first start;
  after that, `output/users.json` is authoritative and the variable is
  ignored. Passwords are stored as bcrypt hashes — the file leaking costs an
  attacker a cracking run, not a login — but treat `output/` as sensitive
  anyway: it also holds the secrets-manager configuration.
- The web UI can optionally authenticate against an OIDC provider
  (authorization-code flow with PKCE; ID tokens verified against the IdP's
  JWKS for signature, issuer, audience, expiry and nonce). Sessions and roles
  are unchanged underneath: an SSO login mints the same revocable session,
  and the user record simply carries `source: "oidc"` and no password hash.
  Admission is deny-by-default (unmapped groups are refused and audited), an
  IdP username colliding with a local user is refused rather than merged,
  and a locally-disabled user stays refused even with a valid IdP assertion.
  A down IdP affects only the SSO endpoints — never password login.
- Web UI sessions are opaque, server-side and revocable (12 h sliding expiry,
  7-day cap); API tokens for automation are named, role-limited and
  individually revocable. Both are stored **as SHA-256 hashes** in
  `output/.sessions.json` and `output/.api-tokens.json` (mode 0600), so a
  stolen state file yields no working credential. Live log streams use
  short-lived (60 s) per-job tokens in the query string instead of the
  session token. Failed logins are rate-limited per username+IP.
- Every mutating API call, every login attempt (username kept, password
  never) and every reveal of a stored LUKS passphrase is appended to
  `output/audit.jsonl`, bounded at ~20k events. Admins can read it in the UI.
- The OpenAPI schema at `/docs` is intentionally readable without login; every
  endpoint it describes requires auth (403 for an authenticated principal
  whose role is insufficient).
- **Imaging** requires Secure Boot to be off on the target: the netboot imager
  is a custom initramfs and nothing signs it. The **installed image** supports
  Secure Boot — it carries the distribution's signed shim and GRUB — so the
  sequence on a machine where policy requires it is disable, image, re-enable.
  Signing the imager chain is not done and is an open item.

## Disk encryption

Optional LUKS2 encryption (`--encrypt`) covers both root slots and the overlay
(`/boot` stays plaintext for GRUB). Choose an unlock method by threat model
(`--unlock`):

- **`tpm2`** — key sealed to the machine's TPM; never on disk. Best where a TPM
  exists.
- **`tang`** — key fetched from a Tang server on a trusted LAN (NBDE); never on
  disk. Best no-TPM auto-unlock.
- **`keyfile`** — key stored on the unencrypted BOOT partition of the same disk
  (`ab-keys/luks.key`, mode 0400) and copied into the initramfs at boot.
  Convenient and universal, but provides **weak at-rest protection** (pulling the
  disk yields the key). Prefer `tpm2`/`tang` for real protection.

  It lives there rather than inside the image because an update replaces the root
  slot *and* the initramfs built from it: a key baked into an image is the
  builder's key, not the machine's, and delivering one by bundle left encrypted
  machines unbootable. Exposure is unchanged — the key was already on that same
  plaintext partition, inside the initramfs — but it is no longer duplicated into
  every image, and **no longer shipped inside update bundles**, which are
  published over plain HTTP for any machine to fetch.
- **`passphrase`** — prompt at boot; most secure, not unattended.

For `tpm2`/`tang`, a bootstrap keyfile makes the first boot unattended. Enrollment
then binds the volumes with clevis and stages a keyless initramfs, and only after
the **next** boot has come up through it — proving the machine no longer needs the
keyfile — is the bootstrap keyslot destroyed. A failure at any stage leaves the
keyfile in place and retries, so no machine is left unable to unlock itself.

A `tpm2` binding is sealed to **PCR 7** (Secure Boot policy state) by default, not
to the PCRs that measure the boot chain. PCR 7 does not move when the kernel or
initramfs changes, so the binding survives an A/B update and works from the
recovery GRUB entries as well as the normal ones. Override with `--tpm2-pcrs` if
your threat model calls for it, knowing that sealing to 8/9 means re-enrolling on
every update.

The `--luks-passphrase` you supply is always kept as a recovery key — store it
safely. See [BUILDER.md](BUILDER.md#disk-encryption-luks2).

> **Images built before this**, with `--unlock tpm2`, enrolled via
> `systemd-cryptenroll` and wrote `tpm2-device=auto` into `/etc/crypttab`. Debian's
> initramfs-tools does not implement that option, so those machines cannot unlock
> at all once the bootstrap keyfile is destroyed — on either slot, and from every
> GRUB entry, because LUKS is unlocked long before the kernel command line is
> read. Unlock with the recovery passphrase (the prompt goes to `ttyS0`, not the
> monitor) and re-image, or re-add a keyfile keyslot by hand.

### Where the recovery passphrase lives

"Store it safely" is the part that fails in practice. For every unlock method
except `passphrase`, the passphrase is never typed again during normal
operation — machines unlock from the TPM, from Tang, or from a keyfile — so
nothing exercises it until the day a TPM is cleared by a firmware update and a
machine stops at the initramfs prompt.

The web UI can therefore generate it and put it in a secrets manager instead
(OpenBao or HashiCorp Vault, KV v2). Configure a store under **Secrets Manager**,
then tick *Generate a random passphrase and store it* on an encrypted build:

- The passphrase is 256 bits of randomness, generated in the backend — never
  typed, never displayed during the build, never on a command line.
- It is written to the store **before the build starts**. If the store will not
  take it, no image is built. The reverse order can produce an encrypted image
  whose recovery key was never persisted, which is not recoverable; a leftover
  secret from a failed build is merely untidy.
- It is filed under the image's name (`<mount>/<prefix>/<image>.img`), with the
  distro, suite, arch and unlock method alongside it.
- The **builder container never talks to the store.** It receives the passphrase
  in `LUKS_PASS` exactly as it does when one is typed, so nothing privileged
  gains network access or a store credential.
- Packaging an update bundle from an encrypted image reads the passphrase back
  automatically rather than prompting for it.

Two things to be deliberate about:

- **The store credential needs write access**, not just read. A read-only policy
  passes the connection test and fails the first build.
- **Rebuilding the same image name files a new passphrase.** KV v2 keeps the
  previous version, and machines already imaged still need it — so keep version
  history on that mount (a KV v1 mount is rejected for this reason).

Revealing a stored passphrase from the UI is deliberately one click: the moment
you need it is a machine stopped at an initramfs prompt. It is no wider than the
session already is — this UI drives container builds, and the image builder runs privileged, so starting a build reaches host root regardless of the Docker API proxy in front of it (see WEBUI.md, "The Docker socket").

On the command line, `scripts/luks-secret.sh` does the same job against the
`bao`/`vault` CLI and writes the same payload, so an image built either way is
recoverable from the other. See
[BUILDER.md](BUILDER.md#storing-the-passphrase-in-a-secrets-manager).

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Report a vulnerability** under
the repository's Security tab
(<https://github.com/kforbus3/flipside/security/advisories/new>), with
reproduction steps and the affected commit. Expect an acknowledgment within a
few days; confirmed issues are fixed before public disclosure.
