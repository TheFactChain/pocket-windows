# Execution ledger — docs/implementation-plan.md

Current status moved to Beads in the parent portable root on 2026-10-20.
The entries below are historical. Stage 3 has a prepared personal copy, confirmed
by `STAGE3-PREPARATION.json`; complete fresh network acceptance is not confirmed.
Current fixes/cleanup: `wcp-bgr`; remaining stage 3: `wcp-dxc`; stage 4: `wcp-cxi`;
stages 5-7: `wcp-79r`. Do not interpret the old "not started" section as current.

## Stage 1 — complete

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

- Staged audit: 43 files / 136,306 bytes, seven encodings of saved-credential
  references checked; no findings and no values printed. No binary/data payloads.
- Source commit `78f372646dab9dfb0bd6c6249f435d45ecc7f783` pushed to the approved
  private repository; owner/visibility and remote SHA confirmed at 10:54 UTC.
- E: free 150.15 GiB after the verified baseline. No original VM was started/stopped.

## Stage 2 — in progress

Use the installed .NET Framework compiler for a small WinForms EXE. Python remains
the runtime/lifecycle backend. Add read-only preflight and public resource settings;
no profile is applied and no guest is started by a diagnostic/settings test.

- Backend + native EXE suite: 62/62 passed. New tests were watched RED before code.
- Real C#↔Python JSON/stdin and public-settings persistence passed in a separate
  verified-runtime preview with no disks or profiles; unconfigured Start was blocked.
- Small EXE built: 22,528 bytes. Stage-2 artifact and detailed limits are recorded in
  `docs/stage2-report.md`. No actual VM/network test is claimed for the EXE.
- Pending: fresh-context source review, final staged audit, separate commit and push.
## Stage 2 - implemented and verified

- Native WinForms EXE built with the installed .NET compiler; no new dependencies
  or license acceptance. Final artifact: 23,040 bytes; SHA256 and paths are in
  `stage2-evidence.json` and `stage2-report.md`.
- Full source suite: 68/68 passed after fixes. The compiled EXE self-test and native
  C# to Python integration passed in a separate verified-runtime preview with no
  disks or profiles. Unconfigured Start was blocked; QEMU process set unchanged.
- Public settings persistence and native form failure-message preservation passed.
  PowerShell 5.1 parsing and bounded public startup-result classification passed.
- Fresh-context review: no Critical findings; all three Important findings fixed
  with RED-to-GREEN regressions (save lock, preserved UI error, typed startup result).
  Detailed review rulings and limitations are recorded in `stage2-review.md`.
- No original project change, VM start/stop, VPN application or host network/security
  change. No live UAC, manual visual, new guest/network or second-PC test is claimed.
- The stage's source-only commit and private remote SHA are delivered in the final
  handoff/local publication receipt, which is produced after this source snapshot.

## Next boundary - stage 3, not started

Assemble and test a separate personal copy. Preserve original disks and sessions;
use a distinct loopback LAN port. Report VM/network acceptance only after tests.
