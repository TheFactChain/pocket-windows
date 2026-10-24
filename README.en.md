[Русский](README.md) | [English](README.en.md)

![Pocket Windows: portable Windows with a separate network connection](docs/assets/readme/cover.svg)

# Pocket Windows

Portable Windows for accessing blocked services through an existing proxy. One folder contains Windows 11, a launcher and a separate network environment. Extract the bundle, add your connection and open a desktop with Claude Desktop and Firefox already prepared.

<p><a href="#download"><strong>Bundle download</strong></a></p>
<p><a href="#first-start">Get started</a></p>
<p><a href="https://t.me/TheFactChain">News on Telegram</a></p>

The screenshots show version **2026.10.24.13**. The full `Pocket-Windows-2026.10.24.13.zip` archive is about **16.9 GB** (15.8 GiB). A public download link for this bundle has not been published yet. GitHub **Source code** archives contain source files and cannot be used as the application.

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

A link to the full ZIP and its `.sha256` file will appear here when the bundle is published. Follow the [project Telegram channel](https://t.me/TheFactChain) for news. The old `Setup.exe` does not match the version shown here; this version requires the full Pocket Windows archive.

## First start

1. **Download the bundle once it is published.** Use the link in the [Download section](#download) to get the full ZIP and matching `.sha256` file. You can compare the archive's SHA256 with that file to check the download.
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

The update mechanism supports application files without downloading the Windows image again. The panel will offer an update when a compatible package is published for your installed version. Release news is posted in the [Telegram channel](https://t.me/TheFactChain).

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
