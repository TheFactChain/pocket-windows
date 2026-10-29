# SMBLibrary 1.5.8 local patch

`smblibrary-1.5.8.patch` applies to upstream commit
`2edbcf3161084b51dbe11b8a960b0bc7c224b851`, archive SHA-256
`2240c49f69aaffa5225e8316d5a5440ab2f20bc577bd3161091905f05b94d318`,
root directory `TalAloni-SMBLibrary-2edbcf3`.

`scripts/Build-HostFiles.ps1` checks the archive, runs `git apply --check` and
`git apply`, then compiles deterministically. `--ignore-space-change` accommodates
the archive's CRLF files; the archive hash fixes the exact input. The existing
`host/files/dependency-patches/SMBServer.Signatures.cs` is copied separately.
The build manifest records the patch and local source hashes. Build into a new
directory under the task's service temporary directory; runtime files are not
modified. The corresponding modified vendor source accompanies the DLLs.

The patch retains request-signature verification, mandatory signing, related
compound identifiers, LOGOFF signing keys and native open-buffer cleanup. It adds:

- At most 64 pending change notifications per SMB session, original async request
  identities and signing keys, exactly one final completion after the interim
  packet, and CANCEL without its own response.
- Directory restart refresh, partial first-record overflow instead of empty
  success, and unconditional native directory-pattern cleanup.

`ConfinedFileStore.Notify.cs` uses CLR registered waits instead of dedicated
threads and bounds the store's outstanding native requests to 256.

Wire behavior follows Microsoft's specifications:

- [MS-SMB2 3.3.5.16, CANCEL](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-smb2/57bae3d3-5dd7-4a5f-92cb-fc52e2087dad)
- [MS-SMB2 3.3.4.2, interim responses](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-smb2/c1692ece-7c6d-45c8-a0e3-6b53c0483030)
- [MS-SMB2 3.3.5.18, directory requests](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-smb2/29dfcc9b-3aec-406b-abb5-0b4fe96712e2)
- [MS-FSA 2.1.5.6.3, directory information](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/fa8194e0-53ec-413b-8315-e8fa85396fd8)

For a buffer smaller than the fixed record, MS-FSA specifies
`STATUS_INFO_LENGTH_MISMATCH`; when the fixed record fits, a partial first filename
uses `STATUS_BUFFER_OVERFLOW`. The enumeration position is retained for a retry.
