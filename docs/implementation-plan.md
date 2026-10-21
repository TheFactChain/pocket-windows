# Approved portable implementation — seven stages

Current execution status is in the parent portable root's Beads database.
Unfinished criteria were migrated on 2026-10-20: stage 3 `wcp-dxc`, stage 4
`wcp-cxi`, stages 5-7 `wcp-79r`. This document preserves the approved scope;
`wcp-bgr` tracks the bounded fixes and explicitly approved duplicate cleanup.

Execute on separate local copies. Preserve the original working system. Each stage
ends with relevant verification, a report and a separate source commit. The only
GitHub destination is private `nekontritce11123-lab/windows-claude-portable`; no secrets,
disks, logs, downloaded runtimes or authentication data may be committed or pushed.

1. **Baseline and separation.** Fresh host/VM/Git inventory; source and read-back
   SHA256 verified backup; shared code separate from personal endpoint/auth/path data.
   Check immutable source copies, relative-root settings, template validation, existing
   lifecycle tests and staged secret audit before the initial commit.
2. **Small EXE.** Use existing Python supervisor behind Start/Settings/Diagnostics.
   Bundle runtime, validate prerequisites read-only, report concrete blocking errors.
   Prefer the already installed Windows .NET compiler/WinForms to adding build dependencies.
3. **Personal build.** Assemble a separate test copy with existing disks and saved
   profile. Preserve graceful lifecycle, use a distinct loopback LAN port, and run
   relevant fault/boot tests without opening original disks or closing user sessions.
4. **First run.** Accept only bounded Hysteria2 configuration with verified TLS and
   one explicit endpoint. No direct WorkVM internet while configuration is incomplete.
   Prepare application UI/code for changes; any materially new security/network access,
   new credentials or Linux password change requires separate action-time approval.
5. **Clean release.** Build a separate tree with no personal profile, user logs, browser
   state or copied user disks. Do not claim that deleting host-side config sanitizes a
   disk. Windows image distribution remains behind the license gate; do not publish.
6. **Paths and portability.** Test spaces/Unicode/changed location using separate copies,
   bundled dependencies and stale-state handling. Explicitly state that no second PC
   was tested when one is unavailable.
7. **Local ZIPs.** Create personal and release ZIPs with scope/prerequisite instructions,
   manifests, complete member CRC/SHA256 verification, and final ZIP hashes. Personal
   archive retains existing saved secrets locally; neither archive is uploaded.

Shared interfaces: public runtime settings feed both preflight and QEMU; profile
validation feeds guest templates and first-run UI; root resolution feeds both EXE
and Python supervisor; accepted state belongs to the data copy that was actually
tested. An old marker is historical evidence, never proof for another PC or profile.

Scope rulings: the approved seven-stage brief is the controlling plan. Existing
proposal documents describe future ideas but do not authorize changing the host
network/security or creating credentials. Keep incomplete gates explicit rather than
calling an unconfigured clean package a ready Windows release.
