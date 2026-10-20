# Stage 2: small desktop EXE and read-only preflight

The native x64 WinForms front end is compiled with the installed .NET Framework
compiler. It calls the existing bundled Python backend through JSON stdin/stdout;
no credentials are placed in command-line arguments. No build dependency or driver
was installed, and no new license agreement was accepted.

Implemented:

- Start, Settings, Diagnostics buttons. The panel runs without elevation; UAC is
  requested for VM startup by the existing wrapper after preflight succeeds.
- Public RAM/vCPU/loopback-port settings, validation, atomic save and reload.
  Saving holds the runtime ProjectLock and refuses this copy's running QEMU guests.
- Read-only checks for Windows AMD64, WHPX, available RAM/disk space, bundled files,
  pinned QEMU SHA256, guest disks/UEFI variables and the existing acceptance marker.
- Missing guest disks/acceptance keep startup blocked. Diagnostics performs no
  internet probe, reads no private profile, and changes no host configuration.
- Paths come from the executable location rather than current working directory.
  Closing the panel does not shut down a guest. Timeouts never force-kill guests.
- Failed settings saves remain visible. The startup wrapper returns a bounded,
  typed public result for UAC cancellation, execution-policy blocks, runtime errors
  and the local receipt path; raw logs or private error text are not shown.

Verification performed on DANK-PC:

1. Full Python/source suite: **68/68 passed** in 3.174 seconds. New backend/native
   regressions were observed RED before their fixes, then GREEN.
2. Real compiled EXE self-test: three controls, different current directory, Unicode/
   spaces/quotes/backslashes through native Windows argv, and safe missing-runtime refusal.
3. Separate preview runtime: **4,169 files / 1,296,313,105 bytes** copied from the
   verified baseline and checked against its SHA256 manifest. No guest disks, private
   profiles, Git data, history or installation media were copied. Public test settings
   and a runtime lock file were created only in this QA copy.
4. Real C# Bridge to bundled Python integration: settings serialized over stdin,
   saved and re-read (LAN port 32861), runtime/pinned-engine preflight passed,
   unconfigured startup refused, and the QEMU process set remained unchanged.
   A real MainForm workflow preserved the concrete failed-save message.
5. Native Windows PowerShell 5.1 parsed the wrappers, serialized only whitelisted
   public result fields, and classified a wrapped Win32 cancellation code 1223.
6. Native preflight observed Windows AMD64, WHPX available, 16,265 MiB total RAM,
   8,482 MiB available at that measurement, and 152,504 MiB free on E:.

Final local artifact (outside Git):

```text
E:\Проекты\windows Claude portable\build\stage2-final-20261020T112112Z\Launcher.exe
E:\Проекты\windows Claude portable\preview-stage2-20261020T110420Z\Launcher.exe
```

Final artifact: **23,040 bytes**, SHA256
`3EDA5F20BA31EEA548D5C75990B9E38B54388F43A62376A735CAFA19348BF458`.
The preview is only for panel/runtime checks; Start stays blocked without images.
Historical QA builds are retained separately and are not release distributions.

A fresh-context source review found three Important issues; all three were fixed
and regression-tested. See `stage2-review.md` for the decisions and remaining scope.

Limits: these are shell/backend tests, not a new guest/network acceptance run. No
VM was booted/stopped and no VPN profile was applied in stage 2. Mouse-click/visual
review and actual UAC approval/cancellation were not manually exercised. Personal
build, first-run profile application, clean release, second-PC verification and ZIPs
remain later stages. Historical network acceptance of the original system is not
attributed to this new EXE. The original project and VM disks remain untouched.

Rebuild source with `scripts/Build-Launcher.ps1 -OutputDirectory <new folder>`.
The native integration fixture is `tests/DesktopBackendIntegration.cs`, compiled
with `ui/Launcher.cs` and `/main:DesktopBackendIntegration`; run it only against a
separate source/runtime-only QA copy with no images or private profiles.
