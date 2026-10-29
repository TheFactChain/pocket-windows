#!/bin/sh
set -eu
delivery=/tmp/wc-build
cd "$delivery"
sha256sum -c SHA256SUMS
install -d -m 0755 /usr/local/lib/wc-gateway /etc/windowsclaude /etc/systemd/network
install -m 0755 sing-box /usr/local/bin/sing-box
install -m 0755 session.py /usr/local/lib/wc-gateway/session.py
install -m 0644 gateway_profile.py connection_profile.py /usr/local/lib/wc-gateway/
install -m 0644 wc-session.service /etc/systemd/system/
printf '%s\n' '2026.10.27.1' > /etc/windowsclaude/gateway-version
printf '%s\n' 'auto lo' 'iface lo inet loopback' > /etc/network/interfaces
for entry in 'wan0 52:54:00:77:00:01' 'lan0 52:54:00:77:00:02'; do
    set -- $entry
    printf '[Match]\nMACAddress=%s\n\n[Link]\nName=%s\n' "$2" "$1" \
        > "/etc/systemd/network/10-wc-$1.link"
done
systemctl mask networking.service systemd-networkd.service systemd-networkd.socket \
    NetworkManager.service systemd-resolved.service ssh.service ssh.socket \
    apt-daily.timer apt-daily-upgrade.timer
systemctl enable wc-session.service acpid.service
install -d -m 0755 /etc/acpi/events
printf '%s\n' 'event=button/power.*' 'action=/usr/bin/systemctl --no-block poweroff' \
    > /etc/acpi/events/windowsclaude-power
passwd -l root
printf '%s\n' 'net.ipv6.conf.all.disable_ipv6=0' 'net.ipv6.conf.default.disable_ipv6=0' \
    'net.ipv6.conf.wan0.disable_ipv6=1' \
    'net.ipv6.conf.all.accept_ra=0' 'net.ipv6.conf.default.accept_ra=0' \
    'net.ipv6.conf.all.accept_redirects=0' 'net.ipv6.conf.default.accept_redirects=0' \
    > /etc/sysctl.d/90-wc-ipv6.conf
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=8M\nForwardToConsole=no\n' \
    > /etc/systemd/journald.conf.d/wc.conf
printf '%s\n' 'GRUB_TIMEOUT=0' 'GRUB_CMDLINE_LINUX_DEFAULT="quiet"' \
    'GRUB_CMDLINE_LINUX="console=tty0"' > /etc/default/grub
update-grub
python3 -m py_compile /usr/local/lib/wc-gateway/*.py
/usr/local/bin/sing-box version > /etc/windowsclaude/sing-box-version.txt
dpkg-query -W -f='${Package}\t${Version}\n' > /etc/windowsclaude/packages.tsv
sha256sum /usr/local/bin/sing-box /usr/local/lib/wc-gateway/*.py \
    /etc/systemd/system/wc-session.service > /etc/windowsclaude/installed-sha256.txt
apt-get clean
rm -rf /var/lib/apt/lists/*
find /var/log -type f -exec truncate -s 0 {} \;
rm -f /etc/machine-id /var/lib/dbus/machine-id
touch /etc/machine-id
printf 'WC_BUILD_COMPLETE 2026.10.27.1\n' > /dev/ttyS0
