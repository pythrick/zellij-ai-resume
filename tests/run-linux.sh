#!/usr/bin/env bash
# tests/run-linux.sh: run the whole test suite, the real-Zellij one included,
# on Arch Linux ARM in an Apple `container` VM (native on Apple silicon), once
# with bash and once with dash as /bin/sh.
#
#   brew install container && container system start
#   tests/run-linux.sh
#
# The host downloads the packages and the VM installs them offline, so this
# also works where the VM has no network of its own (a corporate proxy or VPN
# on the host). pacman inside verifies every package signature. The image is
# community-built (the official archlinux image is x86_64 only, and under
# Rosetta every process shows /run/rosetta/rosetta in its argv), so the
# checkout is mounted read-only and nothing else is shared with it.
set -euo pipefail

image=${ZJR_LINUX_IMAGE:-docker.io/menci/archlinuxarm:latest}
mirror=${ZJR_ALARM_MIRROR:-http://mirror.archlinuxarm.org/aarch64}
packages="python procps-ng zsh dash zellij"
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/sync" "$work/pkg"

ro() { printf 'type=bind,source=%s,target=%s,readonly' "$1" "$2"; }

echo "Fetching package databases from $mirror"
for db in core extra alarm aur; do
    curl -fsSL -o "$work/sync/$db.db" "$mirror/$db/$db.db"
done
container run --rm --mount "$(ro "$work/sync" /var/lib/pacman/sync)" "$image" \
    pacman -Su --print --needed --noconfirm --disable-sandbox $packages | grep '^http' >"$work/urls" || true
[ -s "$work/urls" ] || { echo "run-linux.sh: could not work out the packages to install" >&2; exit 1; }
echo "Fetching $(wc -l <"$work/urls" | tr -d ' ') packages"
(cd "$work/pkg" && while read -r url; do curl -fsSL -O "$url" && curl -fsSL -O "$url.sig"; done <"$work/urls")

cat >"$work/inside.sh" <<EOF
#!/bin/bash
set -u
pacman -Su --needed --noconfirm --disable-sandbox --cachedir /pkg $packages >/tmp/pacman.log 2>&1 ||
    { tail -20 /tmp/pacman.log; exit 1; }
echo "\$(uname -srm) | \$(python3 --version) | \$(zellij --version) | \$(ps --version | head -1)"
useradd -m tester
cp -a /src /home/tester/zjr && rm -rf /home/tester/zjr/*/__pycache__ && chown -R tester: /home/tester/zjr
status=0
for sh in bash dash; do
    ln -sf \$sh /usr/bin/sh
    echo "== /bin/sh is \$sh"
    for file in install.sh uninstall.sh src/launcher.sh.in src/zjr.sh.in; do
        /bin/sh -n /home/tester/zjr/\$file || status=1
    done
    su tester -c 'cd ~/zjr/tests && ZJR_E2E=1 python3 -W error::DeprecationWarning -m unittest test_unit test_launcher test_zellij' || status=1
done
exit \$status
EOF
chmod +x "$work/inside.sh"

container run --rm -c 4 -m 4G \
    --mount "$(ro "$root" /src)" --mount "$(ro "$work" /work)" \
    --mount "$(ro "$work/sync" /var/lib/pacman/sync)" --mount "$(ro "$work/pkg" /pkg)" \
    "$image" /work/inside.sh
