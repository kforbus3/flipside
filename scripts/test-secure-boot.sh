#!/bin/bash
# Boot a built image under firmware that is actually enforcing Secure Boot.
#
# "Secure Boot support" is the easiest claim in this project to make falsely.
# An image with shim on its ESP boots perfectly on firmware with Secure Boot
# switched off, and so does one without — so a test that only proves the image
# boots proves nothing at all about Secure Boot. Three runs are needed:
#
#   1. enforcing firmware, image as built     -> must boot, and the kernel must
#                                                agree that Secure Boot is on
#   2. enforcing firmware, BOOTX64.EFI damaged -> must NOT boot. This is the
#                                                control: without it, run 1
#                                                passing could just mean the
#                                                firmware was not enforcing.
#   3. firmware with Secure Boot off           -> must boot. A Secure Boot image
#                                                that only works with Secure
#                                                Boot on would be a regression
#                                                for every machine that has it
#                                                disabled.
#
#   docker run --rm --privileged --platform=linux/amd64 \
#       -v "$PWD/output":/output -v "$PWD/scripts":/s \
#       --entrypoint bash debian-ab-builder:amd64 /s/test-secure-boot.sh
set -u
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq qemu-system-x86 ovmf gdisk dosfstools mtools util-linux \
    >/dev/null 2>&1

SRC="${SRC:-/output/sb-test.img}"
DISK="${DISK:-/output/sb-target.img}"
SIZE="${SIZE:-16G}"
BOOT_TIMEOUT="${BOOT_TIMEOUT:-420}"
# A refusal is fast: the firmware decides before it ever runs anything. Waiting
# the full boot timeout to conclude "it did not boot" would add ten minutes to
# every run for the one case that is quickest to decide.
DENY_TIMEOUT="${DENY_TIMEOUT:-120}"

fail() { echo "HARNESS-FAIL: $*"; exit 1; }
ok=0; bad=0
pass() { ok=$((ok+1)); echo "  PASS  $1"; }
no()   { bad=$((bad+1)); echo "  FAIL  $1 ${2:-}"; }

[ -f "$SRC" ] || fail "no $SRC — build one with --secure-boot on"

# --- firmware ----------------------------------------------------------------
#
# The .secboot code image is the one that enforces; the .ms vars have
# Microsoft's keys enrolled, which is what a shipped machine has. Both are in
# Debian's ovmf package. Without them there is nothing to test against and
# saying so is the only honest outcome.
FW_DIR=/usr/share/OVMF
CODE_SB=""; VARS_MS=""; CODE_PLAIN=""; VARS_PLAIN=""
for f in "$FW_DIR/OVMF_CODE_4M.secboot.fd" "$FW_DIR/OVMF_CODE.secboot.fd"; do
    [ -f "$f" ] && { CODE_SB="$f"; break; }
done
for f in "$FW_DIR/OVMF_VARS_4M.ms.fd" "$FW_DIR/OVMF_VARS.ms.fd"; do
    [ -f "$f" ] && { VARS_MS="$f"; break; }
done
for f in "$FW_DIR/OVMF_CODE_4M.fd" "$FW_DIR/OVMF_CODE.fd"; do
    [ -f "$f" ] && { CODE_PLAIN="$f"; break; }
done
for f in "$FW_DIR/OVMF_VARS_4M.fd" "$FW_DIR/OVMF_VARS.fd"; do
    [ -f "$f" ] && { VARS_PLAIN="$f"; break; }
done
[ -n "$CODE_SB" ] && [ -n "$VARS_MS" ] \
    || fail "this ovmf has no Secure Boot firmware with Microsoft keys enrolled
    (looked for OVMF_CODE_4M.secboot.fd and OVMF_VARS_4M.ms.fd in $FW_DIR).
    Without them nothing here would be testing Secure Boot."

# --- the disk ----------------------------------------------------------------
rm -f "$DISK"
truncate -s "$SIZE" "$DISK"
dd if="$SRC" of="$DISK" bs=4M conv=notrunc status=none || fail "dd"
sgdisk -e "$DISK" >/dev/null 2>&1
trap 'rm -f "$DISK" /tmp/vars-*.fd' EXIT

esp_offset() {   # byte offset of the ESP, for mtools
    local num start
    num=$(sgdisk -p "$DISK" 2>/dev/null | awk '$NF == "ESP" { print $1; exit }')
    [ -n "$num" ] || return 1
    start=$(sgdisk -i "$num" "$DISK" 2>/dev/null | awk '/First sector/ { print $3; exit }')
    [ -n "$start" ] || return 1
    echo $((start * 512))
}
OFF="$(esp_offset)" || fail "no ESP partition on $DISK"
export MTOOLS_SKIP_CHECK=1
mt() { mtools -c "$@" -i "$DISK@@$OFF"; }

echo "=== what is on the ESP ==="
mt mdir -/ ::/EFI 2>/dev/null | sed 's/^/  /' || echo "  (unreadable)"

for want in ::/EFI/BOOT/BOOTX64.EFI ::/EFI/BOOT/grubx64.efi; do
    if mt mtype "$want" >/dev/null 2>&1; then
        pass "the ESP carries $(basename "$want")"
    else
        no "the ESP carries $(basename "$want")" "(not found)"
    fi
done
# The stub the distribution's signed GRUB will look for. Its prefix is compiled
# into a binary this build does not produce, so if this is missing the machine
# reaches a GRUB rescue prompt rather than a boot menu -- with Secure Boot
# working perfectly, which makes it a confusing way to fail.
if mt mtype ::/EFI/debian/grub.cfg >/dev/null 2>&1 \
   || mt mtype ::/EFI/ubuntu/grub.cfg >/dev/null 2>&1; then
    pass "the prefix stub GRUB looks for is present"
else
    no "the prefix stub GRUB looks for is present" "(no /EFI/*/grub.cfg)"
fi

boot() {   # boot <label> <code.fd> <vars.fd> <timeout> <logfile>
    cp "$3" "/tmp/vars-$1.fd"
    echo ""
    echo "=== $1 (up to ${4}s) ==="
    timeout "$4" qemu-system-x86_64 \
        -machine q35,smm=on -m 2048 -smp 2 \
        -global driver=cfi.pflash01,property=secure,value=on \
        -drive if=pflash,format=raw,unit=0,readonly=on,file="$2" \
        -drive if=pflash,format=raw,unit=1,file="/tmp/vars-$1.fd" \
        -drive file="$DISK",format=raw,if=virtio \
        -nographic -serial mon:stdio -no-reboot > "$5" 2>&1
    return 0
}

# --- 1. enforcing firmware, image as built -----------------------------------
boot enforcing "$CODE_SB" "$VARS_MS" "$BOOT_TIMEOUT" /output/sb-enforcing.log
if grep -qa "login:" /output/sb-enforcing.log; then
    pass "the image boots with Secure Boot enforcing"
else
    no "the image boots with Secure Boot enforcing" "(see /output/sb-enforcing.log)"
    tail -30 /output/sb-enforcing.log | sed 's/^/      /'
fi
# The firmware's word for it, from inside the booted system. Without this the
# run above only proves it booted, which it would also do on firmware that had
# quietly fallen back to no verification.
if grep -qaE "Secure boot enabled|UEFI Secure Boot is enabled|secureboot: Secure boot enabled" \
        /output/sb-enforcing.log; then
    pass "the kernel agrees Secure Boot was on"
else
    no "the kernel agrees Secure Boot was on" \
       "(no 'Secure boot enabled' in the log — the firmware may not have enforced)"
fi

# --- 2. the control: a damaged loader must be refused ------------------------
#
# Everything above passes on firmware that is not really enforcing. This is what
# separates the two: one byte changed inside the signed shim, and the signature
# no longer validates. If the machine still boots, nothing was ever being
# checked and run 1 meant nothing.
mt mcopy ::/EFI/BOOT/BOOTX64.EFI /tmp/BOOTX64.EFI.orig >/dev/null 2>&1 \
    || fail "could not read BOOTX64.EFI off the ESP"
cp /tmp/BOOTX64.EFI.orig /tmp/BOOTX64.EFI.bad
# Well inside the PE image rather than in the header, so it is the signature
# that fails rather than the file being rejected as malformed.
printf '\xde\xad\xbe\xef' | dd of=/tmp/BOOTX64.EFI.bad bs=1 seek=100000 \
    conv=notrunc status=none
mt mcopy -o /tmp/BOOTX64.EFI.bad ::/EFI/BOOT/BOOTX64.EFI >/dev/null 2>&1 \
    || fail "could not write the damaged loader back"

boot tampered "$CODE_SB" "$VARS_MS" "$DENY_TIMEOUT" /output/sb-tampered.log
if grep -qa "login:" /output/sb-tampered.log; then
    no "a damaged loader is refused" \
       "IT BOOTED — the firmware is not enforcing, so nothing above proved anything"
    tail -20 /output/sb-tampered.log | sed 's/^/      /'
else
    pass "a damaged loader is refused, so the firmware really is enforcing"
    grep -qaiE "security violation|access denied|verification failed" /output/sb-tampered.log \
        && pass "and the firmware says why" \
        || echo "  NOTE  the firmware refused it without an explanatory message"
fi

# Put it back.
mt mcopy -o /tmp/BOOTX64.EFI.orig ::/EFI/BOOT/BOOTX64.EFI >/dev/null 2>&1 \
    || fail "could not restore BOOTX64.EFI"

# --- 3. Secure Boot off: the image must still work ---------------------------
#
# The regression that would matter most in the field: most machines have Secure
# Boot disabled today, and an image that only boots with it on would break every
# one of them.
if [ -n "$CODE_PLAIN" ] && [ -n "$VARS_PLAIN" ]; then
    boot plain "$CODE_PLAIN" "$VARS_PLAIN" "$BOOT_TIMEOUT" /output/sb-off.log
    if grep -qa "login:" /output/sb-off.log; then
        pass "the same image still boots with Secure Boot disabled"
    else
        no "the same image still boots with Secure Boot disabled" "(see /output/sb-off.log)"
        tail -30 /output/sb-off.log | sed 's/^/      /'
    fi
else
    echo "  SKIP  no plain OVMF firmware here to test the Secure-Boot-off case"
fi

echo ""
echo "$ok passed, $bad failed"
[ "$bad" -eq 0 ]
