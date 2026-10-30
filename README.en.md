[Русский](README.md) | [English](README.en.md)

# Pocket Windows

**Windows 11 in one folder, with your own connection and prepared apps.**

<p align="center">
  <a href="https://t.me/TheFactChain"><img src="docs/assets/readme/badges/telegram.svg" width="180" alt="TheFactChain Telegram channel" /></a>
  <a href="https://drive.google.com/drive/folders/1OnAncsF3BK5Mo6Vc245Z0D7sW1iBxKHD"><img src="docs/assets/readme/badges/download-en.svg" width="122" alt="Download the full bundle from Google Drive" /></a>
</p>
<p align="center">
  <a href="#first-start"><img src="docs/assets/readme/badges/first-en.svg" width="106" alt="First start guide" /></a>
  <a href="docs/updating.en.md"><img src="docs/assets/readme/badges/update-en.svg" width="136" alt="Update guide" /></a>
  <a href="https://www.youtube.com/watch?v=KAFEjYQKF1E"><img src="docs/assets/readme/badges/video-en.svg" width="138" alt="Watch the video overview on YouTube, in Russian" /></a>
  <a href="#if-something-does-not-work"><img src="docs/assets/readme/badges/help-en.svg" width="92" alt="Help if something does not work" /></a>
</p>

Portable Windows for accessing blocked services through an existing proxy. One folder contains Windows 11, a launcher and a separate network environment. Extract the bundle, add your connection and open a desktop with Claude Desktop and Firefox already prepared.

<p align="center"><img src="docs/assets/readme/social-preview.png" width="720" alt="Pocket Windows and the launcher in version 2026.10.24.13" /><br />
<sub>The Russian-language interface and prices shown are from version 2026.10.24.13. Accounts and subscriptions are not included.</sub></p>

<p align="center"><a href="https://www.youtube.com/watch?v=KAFEjYQKF1E"><img src="https://i.ytimg.com/vi/KAFEjYQKF1E/maxresdefault.jpg" width="640" alt="Watch the Pocket Windows video overview on YouTube, in Russian" /></a><br />
<a href="https://www.youtube.com/watch?v=KAFEjYQKF1E"><strong>Watch the video overview on YouTube (in Russian)</strong></a></p>

## Get started

**First time here:** [download the full 2026.10.30.1 bundle](https://drive.google.com/drive/folders/1OnAncsF3BK5Mo6Vc245Z0D7sW1iBxKHD).

**Already have Pocket Windows:** [latest update for an existing installation](https://github.com/TheFactChain/pocket-windows/releases/latest).

> **The full bundle and application updates are different downloads.** Google Drive contains the full **2026.10.30.1** bundle, about **17.0 GB** (15.8 GiB). It already includes the updater: after extraction, open «Обновления» (Updates) and install the current version. GitHub hosts updates **without the Windows disk**; they cannot run on their own.

| Your situation | What to do |
| --- | --- |
| You do not have Pocket Windows | Download the **full bundle** from Google Drive and follow [First start](#first-start). |
| You have version 2026.10.25.3 or later | Shut down guest Windows normally, open **«Обновления» (Updates)** in the launcher, click **«Проверить» (Check)**, then **«Обновить» (Update)**. |
| You have the older version 2026.10.24.13 | Follow the [separate transition instructions](docs/updating.en.md#version-2026102413), then update from the launcher. |

**In this guide:** [download and SHA256](#download) | [PC requirements](#pc-requirements) | [known limitations](#known-limitations) | [documentation](#documentation-and-source-code)

The application interface is currently **in Russian**. This guide includes the Russian button labels and their English meanings. The screenshots below show version 2026.10.24.13; newer features are described separately.

## Features

![Windows desktop inside Pocket Windows with the prepared applications](docs/assets/readme/desktop.png)

*A separate Windows desktop for a browser and applications that need your selected connection.*

### A separate Windows for the services you need

Browse in Firefox and run applications inside Pocket Windows. Their traffic uses the selected connection. There is no need to reconfigure the VPN on your main Windows system: the separate environment uses its own gateway.

The system and its installed applications stay in the bundle folder between sessions. After shutting Windows down normally, you can move the folder to another compatible PC. Saved connection passwords will need to be entered again on the new PC.

Claude Desktop and Firefox are already prepared. Accounts, subscriptions and service availability depend on the services themselves and your connection; they are not provided by the bundle.

### Start from one panel

The home screen shows the Windows status and selected connection. Start the desktop, return to a running Windows session or shut it down from here. The ready-to-run bundle does not require separate installations of QEMU, Python or Linux.

![Launcher home screen showing Windows status and the start control](docs/assets/readme/launcher.png)

*The panel for starting and managing a session.*

The panel minimizes to the system tray after startup. Closing it does not shut down a running Windows session. Use “Выключить Windows” (Shut down Windows) in the panel or tray menu, then wait for shutdown before moving the folder.

Version **2026.10.25.7** also provides “Выключить Windows и выйти” (Shut down Windows and exit) in the tray. It waits for confirmed Windows and gateway shutdown before closing the panel. An unconfirmed shutdown may expose a separate “Принудительно выключить” (Force shut down) button. It requires confirmation: unsaved work may be lost and the guest Windows disk may be damaged.

### Connect through an existing server or proxy

Add a connection URI or enter its parameters in the panel. You need a working server or proxy and valid credentials. Pocket Windows does not set up a server on an empty VPS.

Supported connections are **Hysteria2 for TCP and UDP**, and **SOCKS5, HTTP and HTTPS CONNECT for TCP**. External connections use **IPv4**. Access to a particular service depends on your server, its location and the service's policies.

![Form for adding a connection from a URI or entering its settings manually](docs/assets/readme/connection.png)

*Add a connection from an existing URI or enter the server settings.*

If the connection drops, Windows does not fall back to direct internet access. Network access stays blocked until the connection recovers. Saving or testing another profile does not switch the running session: changes take effect the next time you start Windows.

### Memory, display and clipboard

The settings let you choose Windows memory, processor count and display resolution. Resource and window settings take effect on the next start.

![Windows settings for memory, processors, display resolution and window behavior](docs/assets/readme/settings.png)

*Resource allocation and desktop window settings.*

Work in a window or toggle full screen with `Ctrl+Alt+F`. Use `Ctrl+Alt+G` to release the mouse. Full screen preserves the selected resolution's aspect ratio, so borders may appear when it differs from the monitor's aspect ratio.

The shared clipboard transfers **text** in both directions between your main and guest Windows systems. You can turn it off in settings before the next start. Copying images is not supported.

The current update implements file transfer with `Ctrl+C` / `Ctrl+V`, but copying in both directions through File Explorer has not completed live acceptance testing. Use [a shared PC folder](#choose-which-pc-files-to-share) to exchange files.

### Choose which PC files to share

File sharing is off by default. You can share selected folders or all accessible PC drives. The choice takes effect the next time Windows starts. When sharing is first enabled, the guest helper may request administrator permission inside the guest to configure the file connection.

![The PC demonstration folder opened as drive Z in guest Windows File Explorer](docs/assets/readme/files.png)

*A selected PC folder with demonstration files opened inside Pocket Windows.*

These are the actual files on your PC. Editing or deleting a shared file inside Pocket Windows changes or deletes the original. For your first sessions, sharing a dedicated folder with the files you need is a useful starting point.

## Download

### Full bundle for the first start

Open the [Pocket Windows folder on Google Drive](https://drive.google.com/drive/folders/1OnAncsF3BK5Mo6Vc245Z0D7sW1iBxKHD) and download **`Pocket-Windows-2026.10.30.1.zip`**. Its size is **16,965,561,181 bytes**. The archive includes Windows, the launcher, the updater and its runtime; no separate installer is needed.

No account sign-in is required. Google Drive may ask you to confirm the download because the archive is too large for its virus scan. After extraction, update the application from the launcher; you do not need to download the Windows disk again.

SHA256:

```text
059f00949dd693bf0b1e329c86d9164ae51a37235f286f30e1cfcc6140ed8851
```

### Updating an existing installation

The current published release is **[2026.10.29.3](https://github.com/TheFactChain/pocket-windows/releases/tag/v2026.10.29.3)**: microphone, audio, power defaults and accumulated fixes. The complete application update, `Pocket-Windows-2026.10.29.3-app.zip`, is **909,595,516 bytes** (about 910 MB). It does not include the Windows disk. You normally do not need to download it manually: use **«Обновления» (Updates)** in the launcher and follow the [instructions for your version](docs/updating.en.md).

The new updater downloads changed, verified chunks and resumes interrupted downloads. The amount depends on your installed version and saved cache; the first download can be large.

| GitHub Assets file | What it is for |
| --- | --- |
| `Pocket-Windows-<version>-app.zip` | Complete **application update**, without Windows. Do not extract `app` over your installation manually. |
| `-chunks.zip`, `-update.json`, `-update-v2.json`, `-updater.zip` | Files used by the updater. You do not need to select or run them manually. `-updater.zip` is not a first-install package. |
| `*.sha256` | Checksum of the corresponding file. |
| **Source code (zip / tar.gz)** | Source files for developers, not the ready-to-run application. |

<details>
<summary>SHA256 of the 2026.10.29.3 application package</summary>

```text
21d08f305d94807ce4df0062da61f29c6633339a53cd3721372ecd6a0504c874
```

</details>

### Verify a downloaded ZIP

In PowerShell, run this command with the path to your downloaded file:

```powershell
Get-FileHash -LiteralPath 'C:\Users\YOUR_NAME\Downloads\Pocket-Windows-2026.10.30.1.zip' -Algorithm SHA256
```

Compare `Hash` with the value for **the same version and filename** above or in the corresponding `.sha256` file. If the values differ, do not use the archive; download it again. SHA256 checks integrity against the published value; it is not a publisher's digital signature.

## First start

1. **Download the full bundle and verify SHA256.** Follow the [download instructions](#download). An application update or Source code archive is not suitable for the first start.
2. **Extract the entire archive.** Use Windows File Explorer and a new writable folder. Do not run `Launcher.exe` from inside the archive or separate it from the other files.
3. **Update the application.** Open `Launcher.exe`, then «Обновления» (Updates). Click «Проверить» (Check), then «Обновить» (Update) and wait for completion. The full 30.1 bundle already includes this feature; guest Windows does not need to be running to update.
4. **Check your PC.** Open «Проверка» (Checks). If Windows Hypervisor Platform is disabled, use «Подготовить ПК» (Prepare PC), approve the administrator prompt and restart the PC if required.
5. **Add a connection.** In «Подключения» (Connections), add an existing Hysteria2, SOCKS5, HTTP or HTTPS proxy and test it. Obtain the server credentials from your connection provider.
6. **Start Windows.** Return to the home screen and press the start button. The network is prepared first, then the Windows desktop opens. Sign in to your applications and services.

Save your work inside Windows and end the session with **«Выключить Windows» (Shut down Windows)**. Wait until it stops before moving or backing up the folder. Click the Pocket Windows icon in the system tray near the clock to reopen the panel. Do not unplug a USB drive or external disk while the application is running.

## PC requirements

![Checks for the Windows version, virtualization, memory, components and connection](docs/assets/readme/diagnostics.png)

*The panel checks whether your PC is ready and shows what needs attention before startup.*

| Resource | Requirement |
| --- | --- |
| System | Windows 11 x64 and .NET Framework 4.8 |
| Virtualization | Enabled in BIOS/UEFI, with Windows Hypervisor Platform available |
| Available RAM | **6 GiB free** at the default settings: 4 GiB for Windows, 1 GiB for the gateway and 1 GiB of headroom |
| Space after extraction | About **18.1 GB** plus **at least another 2 GiB free** to start |
| Download and extraction | Around **40 GB free** is recommended if you keep the downloaded ZIP |
| Network | Internet access and a working server or proxy of a supported type |

The memory requirement is recalculated when you change the allocated memory. Disk usage grows as you add data to Windows.

## Frequently asked questions

### Do I need to install Windows or Linux?

The ready-to-run bundle only needs extraction. It already includes prepared Windows and a hidden network gateway. Your PC must have the virtualization components listed above available.

### Can I move the folder to another PC?

Yes, after shutting Windows down normally. The archive and Windows disk are not encrypted. Profile passwords are protected with Windows DPAPI for the current user of the main system, so you need to enter them again on another PC or under another user account.

### Why did the connection stay the same after I changed it?

The profile, resource settings and sharing settings apply to the next session. Shut Windows down from the panel and start it again. Testing a profile confirms a server response; it does not change the connection used by a running Windows session.

### What does recovery do?

The Checks section can restore Python and QEMU from the local archive, replace the gateway or recover an interrupted session. These operations are separate from the Windows disk and saved connections. They do not repair a damaged Windows disk: recovering that requires a healthy backup.

### Do application updates require downloading the whole Windows image again?

No: the full 30.1 bundle already supports updates from the launcher. The application is updated while the Windows disk and your data are preserved. See the [update guide](docs/updating.en.md), including the separate transition from the older 24.13 release. Do not replace your Windows disk with one from another full bundle.

### Where can I report a problem?

Open an issue in [GitHub Issues](https://github.com/TheFactChain/pocket-windows/issues/new). Russian and English reports are welcome. Include your Pocket Windows version, host Windows version, the action, expected result and error text. For an update failure, include both the starting and target versions. Remove passwords, connection URIs and personal information from screenshots and logs before posting. [Telegram](https://t.me/TheFactChain) is the project's news channel.

## If something does not work

| Situation | What to check |
| --- | --- |
| `Launcher.exe` or components are missing | Make sure you downloaded the full bundle and extracted the entire ZIP into a writable folder. |
| Virtualization is not ready | Open «Проверка» (Checks). Enable virtualization in BIOS/UEFI and use the suggested PC preparation; a restart may be needed. |
| Not enough memory or disk space | Close other applications or lower guest memory in «Характеристики» (Specifications). Check the requirements above; allow for disk growth and update space. |
| No internet inside Windows | Test your existing connection in the panel. Direct internet stays blocked when it fails. After changing the profile, shut down guest Windows and start it again. |
| A PC folder is missing | Select a dedicated test folder in the sharing settings and restart guest Windows. Changes in the shared folder affect the originals. |
| An update was interrupted | Keep the error text and recovery files and follow the [update guide](docs/updating.en.md). Do not delete `state` or copy `app` manually. |

## Known limitations

- The full Google Drive bundle is version 30.1; install current fixes through «Обновления» (Updates) in the launcher.
- Clipboard file transfer, clipboard failure warnings, preservation of NTFS alternate data streams and folder status recovery after a polling failure still need completion of live testing. The service drives `WCPSETTINGS` / `WCPFILES` may be visible in File Explorer.
- External internet uses IPv4; UDP requires Hysteria2. Access to files with multiple hard links and to reparse points is restricted to protect PC folders.
- Release 29.3 adds audio and microphone support, but real recording, playback and display behavior after long idle periods still await owner verification. Select default audio devices before starting; recording requires microphone permissions in both host and guest Windows.
- Full acceptance on another PC, with two monitors and in full screen is incomplete. An intermittent local file-server `AccessDenied` failure is also tracked.

See each release's [GitHub notes](https://github.com/TheFactChain/pocket-windows/releases/latest) for its checks and limitations.

## Documentation and source code

The [Pocket Windows repository](https://github.com/TheFactChain/pocket-windows) contains launcher code, supporting scripts, tests and documentation. Windows and gateway images, the ready-to-run runtime and user data are not stored in Git. A `git clone` or GitHub Source code ZIP does not replace the [ready-to-run bundle](#download).

| Document | Contents |
| --- | --- |
| [Updating](docs/updating.en.md) | Steps for current and older versions, and recovery after a failure |
| [Architecture](docs/architecture.md) | How the application works and its component boundaries |
| [Building and releases](docs/packaging.md) | Preparing, packaging and releasing the bundle |
| [QEMU](docs/qemu-build.md) | Building the virtualization engine |
| [Validation](docs/validation.md) | Check results and their limits |
| [Third-party components](docs/third-party.md) | Licenses and distribution obligations |

Most technical documentation is currently in Russian; the third-party component notes are in English.

## Licenses

A license for Pocket Windows source code has not yet been selected. Windows and third-party components have their own licenses; the bundle does not provide a Windows license or application subscriptions. Component distribution terms and obligations are described in [third-party.md](docs/third-party.md). Check the applicable rights and terms before redistributing the bundle.

![Pocket Windows: portable Windows with a separate network connection](docs/assets/readme/cover.svg)
