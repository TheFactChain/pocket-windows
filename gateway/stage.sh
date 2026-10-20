#!/bin/sh
# One-time root-authorized setup. Attached delivery is read-only; no credentials printed.
set -eu
umask 077
exec > /dev/ttyS0 2>&1
echo WC_STAGE_STARTED
trap 'rc=$?; if [ "$rc" != 0 ]; then /usr/sbin/sysctl -w net.ipv4.ip_forward=0; echo WC_STAGE_FAILED_$rc; fi' EXIT
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
/bin/sh "$base/install.sh" > /root/windowsclaude-install.log 2>&1
/usr/sbin/sysctl -p /etc/sysctl.d/90-windowsclaude.conf
/usr/sbin/nft -f /etc/nftables.conf
systemctl start nftables.service
/usr/sbin/nft -j list ruleset > /run/windowsclaude-firewall-before-nics.json
python3 - "$base" <<'PY'
import json,sys
r=json.load(open('/run/windowsclaude-firewall-before-nics.json'))
chains=[x['chain'] for x in r['nftables'] if 'chain' in x and x['chain'].get('hook')]
assert len(chains)==3 and {c['hook'] for c in chains}=={'input','forward','output'}
assert all(c['policy']=='drop' for c in chains)
print('WC_FIREWALL_READY',flush=True)
PY
# No NIC exists until the host verifies WC_FIREWALL_READY and explicitly hotplugs it.
while [ ! -e /sys/class/net/wan0 ] || [ ! -e /sys/class/net/lan0 ]; do sleep 1; done
ifup wan0
ifup lan0
systemctl start windowsclaude-dhcp.service
systemctl disable wc-stage.service
python3 "$base/runtime_checks.py"
echo WC_STAGE_COMPLETED
