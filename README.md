# Windows Claude — existing local system

This private repository contains the launcher, gateway setup sources, offline tests,
and packaging instructions. It deliberately contains no VM disks, saved VPN profile,
runtime binaries, personal logs, GitHub credentials, or installation media.

The separate **local ZIP** is a clone of the current working system, including both
installed guests, bundled runtime, UEFI state, and existing saved credentials. It is
not a generic installer. It has not been tested on another PC.

## Run the local ZIP

1. Fully extract the ZIP to a writable local NTFS folder with sufficient free space.
   Keep its `Windows-Claude` folder structure together. Do not run inside the ZIP.
2. Use Windows x64 with hardware virtualization and **Windows Hypervisor Platform**
   already available. The tested host runs Windows 11 Pro with 16 GB RAM. Guests use
   4096 MB + 1024 MB, two virtual CPUs each. Allow RAM for the host as well.
3. Run `Start.cmd` and approve its administrator request. The existing launcher uses
   Windows PowerShell 5.1 under the host's script execution policy. It does not enable
   Windows features, install drivers, bypass policy, or reconfigure the host network.
   Missing prerequisites require a separate decision by the computer's owner.
4. Two existing QEMU windows appear: Debian gateway and Windows. The gateway starts
   its services automatically; routine operation does not require console login.
   Programs in Windows are installed by the user.
5. Shut down guest Windows normally. The supervisor then requests a graceful gateway
   shutdown. For explicit graceful stop, run `scripts/Stop-Runtime.ps1` in PowerShell.
   Do not force-close guest processes.

Start paths are relative to the extracted root; the entry scripts derive that root
from their own location. Any desktop shortcut must target the newly extracted
`Start.cmd`; an old absolute shortcut is not portable. There is no `Launcher.exe`,
headless gateway, or generic VPN importer in this version.

Use only one instance on a host: the fixed guest LAN listens on loopback port 31861.
The copied VPN must remain valid and its server reachable. The configured system is
designed to remain offline if the gateway/VPN fails. Prior acceptance is documented
in [validation](docs/validation.md); copying does not prove acceptance on another PC.

## Saved credentials and integrity

The local ZIP intentionally retains existing credentials. In particular, the gateway
disk contains the installed VPN authentication and Linux account password hashes.
`private/gohost-user1/sing-box.json` is a byte-for-byte local copy of the saved VPN
profile. Windows disk contents, browser state, and internal snapshots are retained.
No password is reset, recovered, added to this README, or requested in chat.

The ZIP is **not encrypted**. Keep it as private as the running system; it is never
uploaded by the packaging procedure. This source repository is not a place for it.
`MANIFEST.json` and `MANIFEST.sha256` describe packaged bytes; the adjacent ZIP SHA256
can be checked before extraction. The full archive is verified without booting a VM.

See [architecture](docs/architecture.md), [local packaging](docs/packaging.md), and
[third-party components](docs/third-party.md). Windows use/activation rights and
third-party redistribution rights are separate from this local copy operation.
