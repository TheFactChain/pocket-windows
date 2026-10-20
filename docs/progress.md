# Execution ledger — docs/implementation-plan.md

## Stage 1 — in progress

- 2026-10-20 10:42 UTC: no QEMU/VirtualBox/Hyper-V/VMware guest processes found.
- Host: Windows 11 Pro 10.0.26200, i5-11600 6/12, 15.88 GiB RAM; WHPX enabled,
  hypervisor present, VBS running. E: free 191.01 GiB; C: free 2.20 GiB.
- Original and prepared source directories are not inside any Git repository.
- GitHub identity matches the approved owner; destination returned HTTP 404.
- Both original qcow2 images have no backing file and dirty flag false. Virtual
  sizes are 80 GiB Windows / 12 GiB gateway; internal snapshots retained.
- Baseline copy started separately on E:, with read handles denying writes/deletes.
  Whole baseline is read back and checked against SHA256 before it is accepted.
- Initial suite 50/50 passed. Initial sandbox-temp permission errors were resolved
  by choosing the allowed temporary directory for the test process; product unchanged.
- New settings/LAN-port/profile-template tests were observed failing before implementation.
- Ruling: keep the existing source directory as the Git work area and keep large
  baseline/test/output data on E:. No existing repository or original source is replaced.
  Cost if wrong: work-area paths must be supplied explicitly to build scripts.
- Ruling: use the approved seven-stage plan directly; do not ask for repeated general
  implementation approval. Separate action-time security/licensing gates remain.
  Cost if wrong: work pauses at an explicit gate instead of silently activating access.

- Baseline completed: 4,294 files, 43,867,806,466 bytes; every copied file was read
  back and matched its source-stream SHA256. Existing account hashes and VPN profile
  remain only in the local private baseline, never in the source repository.
- Final stage-1 source suite: 54/54 passed, including backup immutability, writer
  exclusion, settings validation, configurable loopback LAN and closed guest templates.
- Ruling: Git reports Administrators ownership for this newly created work area.
  Supply `safe.directory` for this exact directory per Git command, without persistent
  global trust changes. Cost if wrong: a later command without the option fails closed.

Stage 1 still needs the staged audit, private repository visibility, commit and push.
