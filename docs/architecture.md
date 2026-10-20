# Current architecture

| Component | Current implementation |
| --- | --- |
| Entry | `Start.cmd` → Windows PowerShell wrapper → bundled Python supervisor |
| Hypervisor | Portable QEMU 11.1.0, WHPX only, pinned engine SHA256 |
| Work guest | Existing Tiny11 normal Windows, IDE qcow2 + persistent UEFI variables |
| Gateway | Existing Debian minimal, sing-box 1.14.3 Hysteria2 with gVisor TUN |
| Work network | One e1000 NIC, loopback QEMU stream to gateway LAN |
| Gateway uplink | QEMU user NAT; no host bridge, TAP, hostfwd or guestfwd |
| Guest policy | nftables default drop; one authorized VPS endpoint; no direct fallback |
| Lifecycle | Gateway first, Windows second; normal Windows shutdown then gateway ACPI |
| Host changes at runtime | None to host network, Defender, VBS or Hyper-V features |

The launcher checks a local accepted-network marker and prevents duplicate owned
disk opens. Errors retain running guests for inspection; controllers never send QMP
`quit` or forcibly terminate a guest. Gateway and Windows both currently have a GUI
window. No guest agent or general profile editor is included.

Normal boot needs only the included source files, runtime directories, two active
qcow2 images, `state/workvm-vars.fd`, and `state/gateway-network-accepted.json`.
The installed gateway reads its VPN configuration inside its disk. The host-side
private profile is retained as requested, but normal boot does not read it.

Install/resume/staging branches in `control_guest_install.py` are historical setup
tools. Their media and delivery archive are excluded from the local runtime ZIP.
Only the normal `Start.cmd` route is supported for this clone. Do not invoke the
installation branches to start the already installed guests.
