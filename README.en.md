[Русский](README.md) | [English](README.en.md)

![Pocket Windows: portable Windows with a separate network connection](docs/assets/readme/cover.svg)

# Pocket Windows

Portable Windows for accessing blocked services through an existing proxy. One folder contains Windows 11, a launcher and a separate network environment. Extract the bundle, add your connection and open a desktop with Claude Desktop and Firefox already prepared.

<p><a href="https://github.com/nekontritce11123-lab/pocket-windows/releases/tag/v2026.10.25.7"><strong>Download the 2026.10.25.7 update</strong></a></p>
<p><a href="https://drive.google.com/drive/folders/1OnAncsF3BK5Mo6Vc245Z0D7sW1iBxKHD"><strong>Download the full 2026.10.24.12 bundle</strong></a></p>
<p><a href="#first-start">Get started</a></p>
<p><a href="https://t.me/TheFactChain">News on Telegram</a></p>

Application update **2026.10.25.7** is available on GitHub: `Pocket-Windows-2026.10.25.7-app.zip`, **957,576,617 bytes** (about 957.6 MB). It updates an existing installation and does not include the Windows disk; see the [tested update paths](#do-application-updates-require-downloading-the-whole-windows-image-again).

The full download on Google Drive remains version **2026.10.24.12**: `Pocket-Windows-2026.10.24.12.zip`, about **16.9 GB** (15.8 GiB). The screenshots were taken in version **2026.10.24.13**. GitHub **Source code** archives contain source files and cannot be used as the application.

![Windows desktop inside Pocket Windows with the prepared applications](docs/assets/readme/desktop.png)

*A separate Windows desktop for a browser and applications that need your selected connection.*

## A separate Windows for the services you need

Browse in Firefox and run applications inside Pocket Windows. Their traffic uses the selected connection. There is no need to reconfigure the VPN on your main Windows system: the separate environment uses its own gateway.

The system and its installed applications stay in the bundle folder between sessions. After shutting Windows down normally, you can move the folder to another compatible PC. Saved connection passwords will need to be entered again on the new PC.

Claude Desktop and Firefox are already prepared. Accounts, subscriptions and service availability depend on the services themselves and your connection; they are not provided by the bundle.

## Start from one panel

The home screen shows the Windows status and selected connection. Start the desktop, return to a running Windows session or shut it down from here. The ready-to-run bundle does not require separate installations of QEMU, Python or Linux.

![Launcher home screen showing Windows status and the start control](docs/assets/readme/launcher.png)

*The panel for starting and managing a session.*

The panel minimizes to the system tray after startup. Closing it does not shut down a running Windows session. Use “Выключить Windows” (Shut down Windows) in the panel or tray menu, then wait for shutdown before moving the folder.

Version **2026.10.25.7** also provides “Выключить Windows и выйти” (Shut down Windows and exit) in the tray. It waits for confirmed Windows and gateway shutdown before closing the panel. An unconfirmed shutdown may expose a separate “Принудительно выключить” (Force shut down) button. It requires confirmation: unsaved work may be lost and the guest Windows disk may be damaged.

## Connect through an existing server or proxy

Add a connection URI or enter its parameters in the panel. You need a working server or proxy and valid credentials. Pocket Windows does not set up a server on an empty VPS.

Supported connections are **Hysteria2 for TCP and UDP**, and **SOCKS5, HTTP and HTTPS CONNECT for TCP**. Access to a particular service depends on your server, its location and the service's policies.

![Form for adding a connection from a URI or entering its settings manually](docs/assets/readme/connection.png)

*Add a connection from an existing URI or enter the server settings.*

If the connection drops, Windows does not fall back to direct internet access. Network access stays blocked until the connection recovers. Saving or testing another profile does not switch the running session: changes take effect the next time you start Windows.

## Memory, display and clipboard

The settings let you choose Windows memory, processor count and display resolution. Resource and window settings take effect on the next start.

![Windows settings for memory, processors, display resolution and window behavior](docs/assets/readme/settings.png)

*Resource allocation and desktop window settings.*

Work in a window or toggle full screen with `Ctrl+Alt+F`. Use `Ctrl+Alt+G` to release the mouse. Full screen preserves the selected resolution's aspect ratio, so borders may appear when it differs from the monitor's aspect ratio.

The shared clipboard transfers **text only** in both directions between your main and guest Windows systems. It does not copy images or files. You can turn text sharing off in settings before the next start.

## Choose which PC files to share

File sharing is off by default. You can share selected folders or all accessible PC drives. The choice takes effect the next time Windows starts. When sharing is first enabled, the guest helper may request administrator permission inside the guest to configure the file connection.

![The PC demonstration folder opened as drive Z in guest Windows File Explorer](docs/assets/readme/files.png)

*A selected PC folder with demonstration files opened inside Pocket Windows.*

These are the actual files on your PC. Editing or deleting a shared file inside Pocket Windows changes or deletes the original. For your first sessions, sharing a dedicated folder with the files you need is a useful starting point.

## Download

### Application update 2026.10.25.7

Download `Pocket-Windows-2026.10.25.7-app.zip` from the [GitHub release](https://github.com/nekontritce11123-lab/pocket-windows/releases/tag/v2026.10.25.7). The package is **957,576,617 bytes** and does not include the Windows disk. Read the [update instructions](#do-application-updates-require-downloading-the-whole-windows-image-again) before installing it. Do not copy the contents of `app` over your installation manually: that bypasses validation and rollback.

<details>
<summary>SHA256 of the 2026.10.25.7 application package</summary>

```text
70379972667e923383c35519c73812072b009cb2448bf9710e8ef89e13798c72
```

</details>

### Full bundle 2026.10.24.12

Google Drive has not been updated to 25.7. Open the [Pocket Windows folder on Google Drive](https://drive.google.com/drive/folders/1OnAncsF3BK5Mo6Vc245Z0D7sW1iBxKHD) and download `Pocket-Windows-2026.10.24.12.zip`. No account sign-in is required. Google Drive may ask you to confirm the download because the archive is too large for its virus scan.

<details>
<summary>SHA256 checksum</summary>

For the accepted `Pocket-Windows-2026.10.24.12.zip` archive:

```text
0140a811fde1f7656ad68f72bdaef8c969b07ab9d46e16eadd79f4ed5200cfac
```

</details>

## First start

1. **Download the bundle.** Open the [download folder](https://drive.google.com/drive/folders/1OnAncsF3BK5Mo6Vc245Z0D7sW1iBxKHD) and download the full ZIP. To check the download, compare the file's SHA256 with the checksum in the [Download section](#download).
2. **Extract the entire archive.** Use Windows File Explorer and a new writable folder. Do not run `Launcher.exe` from inside the archive or separate it from the other files.
3. **Check your PC.** Open `Launcher.exe` and “Проверка” (Checks). If Windows Hypervisor Platform is disabled, use “Подготовить ПК” (Prepare PC), approve the administrator prompt and restart the PC if required.
4. **Add a connection.** In “Подключения” (Connections), add an existing Hysteria2, SOCKS5, HTTP or HTTPS proxy and test it. Obtain the server credentials from your connection provider.
5. **Start Windows.** Return to the home screen and press the start button. The network is prepared first, then the Windows desktop opens. Sign in to your applications and services.

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

For **2026.10.25.3 and later**, use the launcher's normal update flow: shut guest Windows down normally, open “Обновления” (Updates) and install **2026.10.25.7**. No Windows image download is needed: the Windows disk, applications inside it, connections and personal settings are preserved.

The **2026.10.24.13 to 2026.10.25.7** transition was tested only with the new external updater from a separate extraction of `Pocket-Windows-2026.10.25.7-app.zip`. Shut guest Windows down normally, close every launcher panel for that copy, then follow the [step-by-step release instructions](https://github.com/nekontritce11123-lab/pocket-windows/releases/tag/v2026.10.25.7) (currently in Russian). Do not use the old 24.13 built-in updater for this transition: it does not fully replace the active gateway.

The exact upgrade from **2026.10.24.12** has not been tested separately; no verified instructions for updating that version are provided here. The full bundle on Google Drive is still version 24.12. Release news is posted in the [Telegram channel](https://t.me/TheFactChain).

### Where can I report a problem?

Open an issue in [GitHub Issues](https://github.com/nekontritce11123-lab/pocket-windows/issues). Include your version, the step where the problem occurred and the error text. Remove passwords, connection URIs and personal information from screenshots and logs before posting. [Telegram](https://t.me/TheFactChain) is the project's news channel.

## Documentation and source code

The [Pocket Windows repository](https://github.com/nekontritce11123-lab/pocket-windows) contains launcher code, supporting scripts, tests and documentation. Windows and gateway images, the ready-to-run runtime and user data are not stored in Git. A `git clone` or GitHub Source code ZIP does not replace the [ready-to-run bundle](#download).

| Document | Contents |
| --- | --- |
| [Architecture](docs/architecture.md) | How the application works and its component boundaries |
| [Building and releases](docs/packaging.md) | Preparing, packaging and releasing the bundle |
| [QEMU](docs/qemu-build.md) | Building the virtualization engine |
| [Validation](docs/validation.md) | Check results and their limits |
| [Third-party components](docs/third-party.md) | Licenses and distribution obligations |

Most technical documentation is currently in Russian; the third-party component notes are in English.

## Licenses

A license for Pocket Windows source code has not yet been selected. Windows and third-party components have their own licenses; the bundle does not provide a Windows license or application subscriptions. Component distribution terms and obligations are described in [third-party.md](docs/third-party.md). Check the applicable rights and terms before redistributing the bundle.
