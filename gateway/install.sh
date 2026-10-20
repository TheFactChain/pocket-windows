#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run inside the new Debian gateway as root.'; exit 1; }
grep -q '^ID=debian$' /etc/os-release || { echo 'Debian guest required.'; exit 1; }
[ -b /dev/vda ] || { echo 'Expected new guest virtio disk.'; exit 1; }
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Every dependency is included from the signed, pinned Debian ISO. No network is used.
DEBIAN_FRONTEND=noninteractive apt-get -y --no-download --no-install-recommends -o Dpkg::Options::=--force-confold install "$base"/packages/*.deb
install -m 0755 "$base/sing-box" /usr/local/bin/sing-box
install -d -m 0700 /etc/sing-box
install -m 0600 "$base/sing-box.json" /etc/sing-box/config.json
/usr/local/bin/sing-box check -c /etc/sing-box/config.json
/usr/sbin/nft -c -f "$base/nftables.vpn.nft"
install -m 0600 "$base/nftables.vpn.nft" /etc/nftables.conf
install -d -m 0755 /etc/windowsclaude
install -m 0644 "$base/profile-public.json" /etc/windowsclaude/profile-public.json
install -d /etc/systemd/network /etc/systemd/system/networking.service.d /etc/systemd/system/nftables.service.d
install -m 0644 "$base/10-windowsclaude-wan.link" "$base/10-windowsclaude-lan.link" /etc/systemd/network/
install -m 0644 "$base/interfaces" /etc/network/interfaces
install -m 0644 "$base/90-windowsclaude.conf" /etc/sysctl.d/90-windowsclaude.conf
install -m 0644 "$base/networking-firewall.conf" /etc/systemd/system/networking.service.d/windowsclaude.conf
install -m 0644 "$base/nftables-retain.conf" /etc/systemd/system/nftables.service.d/windowsclaude.conf
install -m 0644 "$base/udhcpd.conf" /etc/udhcpd-windowsclaude.conf
install -m 0644 "$base/udhcpd.service" /etc/systemd/system/windowsclaude-dhcp.service
install -m 0644 "$base/sing-box.service" /etc/systemd/system/sing-box.service
systemctl daemon-reload
systemctl enable nftables.service networking.service sing-box.service windowsclaude-dhcp.service
echo 'Guest files installed. NIC activation, VPN connectivity and failure tests remain pending.'
