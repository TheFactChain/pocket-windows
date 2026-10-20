# Clone the already installed system locally

Run `scripts/pack_existing_runtime.py` with the bundled Python after both VMs have
been shut down normally. This script never starts or stops a VM, edits credentials,
changes a host setting, contacts GitHub, or uploads a file.

```powershell
& '<project>\runtime\python-3.12.14\python.exe' -I -S -B `
  '<source-repo>\scripts\pack_existing_runtime.py' `
  --project '<project>' --source-repo '<source-repo>' `
  --commit '<verified Git commit>' --output '<local destination>\Windows-Claude.zip'
```

The script includes the tracked source allowlist, both active disks, the complete
existing runtime, the UEFI variables, the accepted-network marker, and the current
saved private VPN profile. It opens source data read-only and refuses existing ZIP
outputs. Large payloads are streamed with ZIP64 and fast standard Deflate compression.

Excluded without deleting originals: `.git`, Git authentication, existing logs and
screenshots, stale session/PID state, cache/history, previous broken/backup images,
installer ISO/WIM/initrd/answer files, downloaded builders, delivery archives, build
directories, and outside-project SSH keys or Downloads profiles. Linux passwords and
installed VPN authentication remain preserved inside the copied gateway disk.

All files required for the same normal boot are included. Source tools for historical
installation branches may reference excluded media; they must not be used for boot.
The host Python/Codex installation is not a runtime dependency.

The generated manifest contains file names, sizes and hashes, not credential values.
The ZIP is deliberately a private local copy with existing secrets and is not encrypted.
The final SHA256 sidecar covers the entire ZIP. Its size is measured after compression;
uncompressed payload size is reported independently.

Offline source tests (no VM start):

```powershell
& '<project>\runtime\python-3.12.14\python.exe' -I -S -B -m unittest discover -s '<source-repo>\tests' -v
```
