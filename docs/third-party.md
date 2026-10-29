# Third-party components

The source repository carries only project scripts, tests, configuration templates,
and documentation. No downloaded builder or third-party runtime is committed.

The local ZIP preserves the already present runtime directories and their bundled
license notices: Python 3.12.14 (`LICENSE.txt`), QEMU 11.1.0 (`COPYING`, `COPYING.LIB`),
and bundled firmware notices including `share/edk2-licenses.txt`. `qemu-system-x86_64.exe`
is built from QEMU 11.1.0 with `patches/qemu-11.1.0-sdl-window.patch`: a fixed SDL
window, corrected 2D mouse coordinates and edge release, a fixed window title, and
no caption updates on mouse grab. The source archive, build procedure and native SDL
regression are documented in `qemu-build.md`; `scripts/patch_qemu_engine.py` installs
the approved executable and updates component recovery without binary patching.
QEMU retains its GPLv2 license. The existing DLLs and firmware are preserved. Debian and sing-box
remain installed in the existing gateway disk. Windows remains in its existing disk.

`Launcher.exe` is built from `launcher-wpf` (WPF, .NET Framework 4.8) and embeds
WPF-UI 3.0.5 (lepoco/wpfui, MIT license) restored from NuGet at build time.

The public source repository and release downloads do not grant additional rights to
Windows or third-party components beyond their respective licenses.
No new EULA is accepted by the packaging script. Confirm Windows licensing and any
third-party license/source obligations before a separate redistribution decision.
Tiny11 builder source and oscdimg are neither required for normal boot nor included.
The previously reviewed builder NOTICE did not grant a separate license; no license
for that builder is inferred here.
