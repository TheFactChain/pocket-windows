# Stage 2 source review rulings

One fresh-context source reviewer reported no Critical findings and three Important
findings. All three were accepted and fixed before the stage-2 commit.

1. **Save/start race.** Public settings were checked for running guests without
   holding ProjectLock. Saving now acquires the same nonblocking lock as startup,
   checks guest processes and writes atomically while holding it. The new regression
   failed before the fix and passes when the lock is held even without QEMU running.
2. **Lost settings error.** The form ran diagnostics after a failed save, replacing
   the useful error message. The shared form workflow now stops after failure.
   The native C# regression reproduced the lost error before the fix, then verified
   the concrete message remains visible through the real backend/UI workflow.
3. **Hidden startup error.** The hidden wrapper lost concrete UAC/policy/supervisor
   outcomes. A bounded public JSON result channel now exposes whitelisted result
   codes and a safe relative receipt path. It rejects malformed/unowned channels
   and does not forward raw stdout/stderr. PowerShell waits for its direct child,
   avoiding a process-tree wait on the long-running supervisor.

Verification after fixes: 68/68 source tests, final compiled EXE self-test, native
C# to bundled-Python settings/preflight/blocked-start integration, actual form error
preservation, and native Windows PowerShell 5.1 parser/result classification passed.
The tests do not claim real UAC or VM/network integration.

Other review points were recorded with the following scope decisions:

- Guest networking, recovery and binding acceptance to this data copy require the
  separate personal-build tests in stage 3. Old acceptance is historical evidence.
- Real UAC timing, approval/cancellation and operating-system RunAs behavior were
  not exercised. Typed synthetic/native classification tests cover the code path
  without asserting an observed live prompt result.
- Constructing controls and running their workflows does not prove mouse, DPI or
  visual-layout usability. A manual visual pass is still outstanding.
- First-run configuration, clean release, ZIPs and a second PC remain later stages.
- The reviewer did not independently rerun tests, hashes or remote publication.
  The implementer performed the documented checks; commit/push verification is
  recorded in the delivery receipt and final handoff after this source is committed.

There were no Minor findings to defer. No guest profile, host networking, credentials,
password or security setting was changed for this review or its fixes.
