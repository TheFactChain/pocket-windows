#!/bin/sh
set -eu
# Executed only in the disposable Debian acceptance delivery, never on the host.
mv /usr/local/lib/wc-gateway/session.py /usr/local/lib/wc-gateway/probe.py
mv /etc/systemd/system/wc-session.service /etc/systemd/system/wc-probe.service
rm -f /etc/systemd/system/multi-user.target.wants/wc-session.service
rm -f /etc/systemd/network/10-wc-wan0.link /etc/systemd/network/10-wc-lan0.link
systemctl mask wc-session.service networking.service systemd-networkd.service \
    systemd-networkd.socket NetworkManager.service systemd-resolved.service sing-box.service
printf '%s\n' 'net.ipv6.conf.all.disable_ipv6=0' 'net.ipv6.conf.default.disable_ipv6=0' \
    'net.ipv6.conf.all.forwarding=0' 'net.ipv6.conf.default.forwarding=0' \
    'net.ipv6.conf.all.accept_ra=1' 'net.ipv6.conf.default.accept_ra=1' \
    'net.ipv6.conf.default.autoconf=1' > /etc/sysctl.d/90-wc-ipv6.conf
systemctl enable wc-probe.service
sha256sum /usr/local/bin/sing-box /usr/local/lib/wc-gateway/*.py \
    /etc/systemd/system/wc-probe.service > /etc/windowsclaude/installed-sha256.txt
