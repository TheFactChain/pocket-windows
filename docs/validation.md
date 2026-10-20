# Existing acceptance and clone verification

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
