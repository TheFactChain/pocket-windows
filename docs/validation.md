# Existing acceptance and clone verification

## Portable maintenance verification — 2026-10-20

Current evidence: `maintenance-verification.json`; cleanup evidence is the parent
portable root's `CLEANUP-REPORT.json`. The original working project was excluded
from changes. Baseline deletion was explicitly approved by the user.

- Full portable suite: 79 tests passed, zero failures (4.125 seconds). Regressions
  were observed failing before the fixes for package EXE/settings inventory,
  owned-pair memory checks, invalid settings recovery, and historical warning retention.
- Native C# to Python integration passed on a separate QA copy without disks or
  profiles: invalid settings were editable as defaults and preserved before save,
  warnings remained visible, failed saves stayed visible, and unconfigured Start
  was refused. The QEMU process set did not change.
- Rebuilt 24,064-byte Launcher.exe passed its native self-test. Its SHA256 is
  recorded in the JSON evidence. No original guest was opened or controlled.
- Approved baseline, preview and old build copies were removed after checking
  standalone qcow2 disks and absence of link dependencies. 8,519 files were deleted;
  filesystem free space increased by about 42.08 GiB. Protected working data retained
  its sizes and last-write times.
- After cleanup, required files, QEMU SHA256, public settings, process state and
  WHPX passed read-only checks outside the sandbox. Actual startup was blocked only
  by available RAM: 4,153 MiB observed versus 4,864 MiB required by current settings.
  The sandbox itself cannot observe WHPX capability; it was checked outside it.

No VM/VPN test, second-PC test or complete personal ZIP build was performed during
this maintenance. Saved acceptance remains historical; diagnostics do not certify
the network of the current copy. Ongoing project status is in Beads, not this report.

## Historical acceptance of the original system

The original system was accepted on its original Windows 11 Pro host on 2026-10-20.
The historical observations below are not second-PC tests.

- Native gateway suite: 10/10 checks, including VPN stop/recovery, crash restart,
  VPS outage/recovery, retained nftables policy on service stop, and blocked IPv6.
- Actual Windows long test: 375 samples, 339 successful HTTPS requests and 36
  expected failures during controlled fault injection. No unexpected successful IP.
- Actual Windows gateway absence: five failed HTTPS probes; recovery after gateway
  cold boot. All successful requests used the configured VPS egress.
- Common cold boot followed by a fresh actual Windows test: 49/49 samples with DNS,
  HTTPS and expected VPS egress successful.
- Captured gateway WAN audits reported zero bypass packets.
- Existing launcher/controller offline tests: 35 launcher tests and four controller
  safety tests passed during original implementation; rerun before this source push.
- Normal shared start and repeat click were tested without duplicate guests.

This packaging operation does not boot or stop guests. It checks that guest processes
are absent, obtains read-only qcow2 metadata, and holds source disks/UEFI state with
Windows read handles that deny concurrent writers while copying. It keeps the active
disks and their internal snapshots unchanged; no repair, conversion or compaction.

ZIP verification streams every payload member, validates CRC during decompression,
checks SHA256/size against the manifest, rejects duplicate or unsafe member paths,
and checks the expected member set. It then computes SHA256 of the final ZIP.

Portability checks are limited to the relative-path normal-boot plan and complete
dependency inventory. Actual start, input, performance, policy, and fail-closed tests
on another PC remain unperformed. The acceptance marker records historical acceptance
and does not silently certify a different host.
