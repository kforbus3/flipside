# Debian A/B Images — build & provisioning orchestration.
.DEFAULT_GOAL := help
OUTPUT ?= $(CURDIR)/output

# Image build options (override on the command line, e.g. `make image HOSTNAME=web01`)
# SUITE picks the release; the distro is auto-detected from it
# (trixie/bookworm -> Debian, resolute/noble/jammy -> Ubuntu).
SUITE ?= trixie
HOSTNAME ?=
USERNAME ?= debian
PASSWORD ?= debian
# "auto" = smallest possible image; it expands to fill the disk on first boot
IMAGE_SIZE ?= auto
# Root slot size in MiB. Empty = the builder picks the default for the distro
# and profile (3072, raised to the floor an Ubuntu or desktop build needs) —
# setting it here would count as an explicit choice and pin every build to it.
ROOT_SIZE ?=
COMPRESS ?= zstd
# Build profile: minimal (default — exactly the base system, nothing added),
# server (a small headless-admin set), or desktop (a full graphical login;
# pick the environment with DESKTOP=gnome|kde|xfce|mate|cinnamon|lxqt —
# availability differs per distro, see docs/BUILDER.md#profiles).
PROFILE ?=
DESKTOP ?=
# Extra packages to install into the image, space-separated
# (e.g. `make image PACKAGES="vim curl qemu-guest-agent"`)
PACKAGES ?=
# Architecture for the netboot imager (amd64 or arm64). The imager is a kernel
# the target machine executes, so build one per architecture you deploy.
ARCH ?= amd64
# Writable state: how much of the root a machine can change, and what the two
# A/B slots share. overlay (default) = the whole root, shared. stateful = root
# read-only, /home and /var persist. appliance = root read-only, only /data
# survives an update. The four path lists are space-separated and work with any
# model — see docs/BUILDER.md#writable-state.
STATE_MODEL ?=
# SLOT_PRIVATE_UPPER=1 gives each slot its own overlay upper layer instead of
# one shared by both, so a config change that stops slot A booting cannot follow
# you into slot B. The slots then share nothing the overlay covers — pair it
# with PERSIST for what should stay shared. It cannot be changed by an update.
SLOT_PRIVATE_UPPER ?=
PERSIST ?=
SLOT_PRIVATE ?=
VOLATILE ?=
RESET_ON_UPDATE ?=
KEEP_PATHS ?=
# LUKS2 encryption: ENCRYPT=1 enables it; UNLOCK picks the method
# (passphrase|keyfile|tpm2|tang); LUKS_PASSPHRASE is required with ENCRYPT=1
ENCRYPT ?=
UNLOCK ?= tpm2
LUKS_PASSPHRASE ?=
TANG_URL ?=

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: image
image: ## Build the A/B disk image into ./output (SUITE=, PROFILE=minimal|server|desktop, DESKTOP=, PACKAGES=, ENCRYPT=1, STATE_MODEL=stateful|appliance)
	./builder/run.sh --suite $(SUITE) $(if $(HOSTNAME),--hostname $(HOSTNAME)) \
	  --username $(USERNAME) --password '$(PASSWORD)' \
	  --image-size $(IMAGE_SIZE) $(if $(ROOT_SIZE),--root-size $(ROOT_SIZE)) --compress $(COMPRESS) \
	  $(if $(PROFILE),--profile $(PROFILE)) $(if $(DESKTOP),--desktop $(DESKTOP)) \
	  $(if $(PACKAGES),--packages "$(PACKAGES)") \
	  $(if $(STATE_MODEL),--state-model $(STATE_MODEL)) \
	  $(if $(SLOT_PRIVATE_UPPER),--slot-private-upper) \
	  $(foreach p,$(PERSIST),--persist $(p)) \
	  $(foreach p,$(SLOT_PRIVATE),--slot-private $(p)) \
	  $(foreach p,$(VOLATILE),--volatile $(p)) \
	  $(foreach p,$(RESET_ON_UPDATE),--reset-on-update $(p)) \
	  $(foreach p,$(KEEP_PATHS),--keep-path $(p)) \
	  $(if $(ENCRYPT),--encrypt --unlock $(UNLOCK) \
	    $(if $(LUKS_PASSPHRASE),--luks-passphrase '$(LUKS_PASSPHRASE)') \
	    $(if $(TANG_URL),--tang-url $(TANG_URL)))

.PHONY: imager
imager: ## Build the netboot imager (kernel + initramfs) into ./output/imager (ARCH=amd64|arm64)
	./imager/run.sh --arch $(ARCH)

.PHONY: bundle
bundle: ## Build a signed RAUC update bundle from an image in ./output (IMAGE=<filename>, VERSION=)
	@test -n "$(IMAGE)" || { \
	  echo "Usage: make bundle IMAGE=<filename in ./output> [VERSION=1.2.3]"; \
	  echo "  e.g. make bundle IMAGE=debian-trixie-ab.img"; exit 1; }
	docker build --platform=linux/amd64 -t debian-ab-builder:amd64 builder
	docker run --rm --privileged -v $(OUTPUT):/output \
	  --entrypoint /build/make-bundle.sh debian-ab-builder:amd64 \
	  --image /output/$(IMAGE) $(if $(VERSION),--version $(VERSION))

.PHONY: webui
webui: ## Start the web management UI on http://localhost:8080 (needs webui/.env)
	@test -f webui/.env || { \
	  echo "webui/.env is missing. Create it with:"; \
	  echo "  cp webui/.env.example webui/.env"; \
	  echo "  # then set ADMIN_PASSWORD and SECRET_KEY"; exit 1; }
	cd webui && docker compose up -d --build
	@echo "Web UI: http://localhost:8080"

.PHONY: webui-down
webui-down: ## Stop the web management UI
	cd webui && docker compose down

.PHONY: webui-logs
webui-logs: ## Follow web UI logs
	cd webui && docker compose logs -f

.PHONY: server-up
server-up: ## Start the PXE/HTTP provisioning server (needs server/.env)
	cd server && docker compose up -d --build

.PHONY: server-down
server-down: ## Stop the provisioning server
	cd server && docker compose down

.PHONY: server-logs
server-logs: ## Follow provisioning server logs
	cd server && docker compose logs -f

.PHONY: all
all: image imager ## Build both the A/B image and the imager

.PHONY: backup
backup: ## Back up everything this server cannot rebuild (FILE=path optional)
	@./scripts/flipside-backup.sh backup $(FILE)

.PHONY: restore
restore: ## Restore from a backup (FILE=path required)
	@test -n "$(FILE)" || { echo "usage: make restore FILE=flipside-backup-....tar.gz"; exit 1; }
	@./scripts/flipside-backup.sh restore $(FILE)

.PHONY: clean
clean: ## Remove build artifacts (keeps output/rauc-keys — the update signing key)
	@if [ -d $(OUTPUT) ]; then \
	  find $(OUTPUT) -mindepth 1 -maxdepth 1 ! -name rauc-keys -exec rm -rf {} +; \
	  echo "Kept $(OUTPUT)/rauc-keys: losing the signing key means no further updates for machines already deployed (see docs/UPDATES.md)."; \
	fi
