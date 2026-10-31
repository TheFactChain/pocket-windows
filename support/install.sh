#!/bin/sh
# Install or update the support receiver on a Debian/Ubuntu host. Run as root from the folder with receiver.py.
# The token is not handled here: put it into /etc/pocket-support/github-token (0600) yourself, then restart.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
install -d -m 0755 /opt/pocket-support
install -m 0644 "$here/receiver.py" /opt/pocket-support/receiver.py
install -d -m 0700 /etc/pocket-support
install -m 0644 "$here/pocket-support.service" /etc/systemd/system/pocket-support.service
systemctl daemon-reload
if [ -s /etc/pocket-support/github-token ]; then
    systemctl enable --now pocket-support
    systemctl restart pocket-support
    systemctl --no-pager --lines=5 status pocket-support
else
    echo "Receiver installed. Put the GitHub token into /etc/pocket-support/github-token, then: systemctl enable --now pocket-support"
fi
