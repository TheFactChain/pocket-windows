# Third-party components

The source repository carries only project scripts, tests, configuration templates,
and documentation. No downloaded builder or third-party runtime is committed.

The local ZIP preserves the already present runtime directories and their bundled
license notices: Python 3.12.14 (`LICENSE.txt`), QEMU 11.1.0 (`COPYING`, `COPYING.LIB`),
and bundled firmware notices including `share/edk2-licenses.txt`. Debian and sing-box
remain installed in the existing gateway disk. Windows remains in its existing disk.

This local copy is not a public software release or a grant of redistribution rights.
No new EULA is accepted by the packaging script. Confirm Windows licensing and any
third-party license/source obligations before a separate redistribution decision.
Tiny11 builder source and oscdimg are neither required for normal boot nor included.
The previously reviewed builder NOTICE did not grant a separate license; no license
for that builder is inferred here.
