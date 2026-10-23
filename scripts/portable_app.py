"""JSON backend for the desktop EXE. Diagnostics never return profile credentials."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_guest_settings as windows_settings
import host_file_sharing as file_sharing
import launch_runtime as runtime
from runtime_config import (DEFAULTS, VIEWER_DEFAULTS, load_settings, validate_settings,
                            load_viewer_settings, validate_viewer_settings)
from connection_profile import import_profile_uri
from profile_store import ProfileStore, StoreBusy, StoreError, SecretsUnavailable, _pinned_directory, _plain
from gateway_package import inspect_current

ENGINE_SHA256 = '8a0feb6045e7493c16579ffefcc2d840074fe1e4af1c99193dee8c6fabf03a86'
RUNTIME_FILES = ('runtime/python-3.12.14/python.exe',
                 'runtime/qemu-11.1.0/qemu-system-x86_64.exe',
                 'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd',
                 'scripts/launch_runtime.py', 'scripts/control_guest_install.py',
                 'scripts/runtime_network_plan.py', 'scripts/runtime_config.py',
                 'scripts/Start-Runtime.ps1', 'scripts/connection_profile.py',
                 'scripts/profile_store.py', 'scripts/gateway_controller.py',
                 'scripts/gateway_package.py', 'scripts/gateway_profile.py',
                 'scripts/session_carrier.py', 'scripts/file_snapshot.py', 'scripts/runtime_logs.py',
                 'scripts/windows_guest_settings.py',
                 'scripts/windows_hardware.py',
                 'scripts/host_file_sharing.py', 'scripts/file_share_carrier.py',
                 'runtime/qemu-11.1.0/qemu-img.exe')
GUEST_FILES = ('images/workvm-empty.qcow2', 'state/workvm-vars.fd')
CHECK_INFO = {
    'windows_x64': ('Версия Windows', 'Нужна 64-разрядная Windows', 'retry', 'Проверить снова'),
    'whpx': ('Аппаратное ускорение', 'Нужен компонент виртуализации Windows', 'windows_features', 'Как включить'),
    'settings': ('Настройки', 'Нужно восстановить настройки', 'settings', 'Открыть настройки'),
    'runtime_state': ('Виртуальные машины', 'Нужно проверить состояние виртуальных машин', 'retry', 'Проверить снова'),
    'memory': ('Оперативная память', 'Не хватает свободной памяти', 'settings', 'Настроить память'),
    'free_space': ('Место на диске', 'Не хватает места на диске', 'retry', 'Проверить снова'),
    'runtime_files': ('Компоненты программы', 'Не все компоненты установлены', 'prepare', 'Восстановить Python и QEMU'),
    'application_files': ('Файлы приложения', 'Нужно распаковать полный комплект', 'retry', 'Проверить после распаковки'),
    'guest_files': ('Диск Windows', 'Не найден диск Windows или файл прошивки', 'retry', 'Проверить снова'),
    'engine_identity': ('QEMU', 'Нужно восстановить QEMU', 'prepare', 'Восстановить Python и QEMU'),
    'gateway_package': ('Шлюз подключения', 'Шлюз нужно восстановить', 'repair_gateway', 'Восстановить шлюз'),
    'powershell': ('Системные инструменты', 'Не найден Windows PowerShell', 'retry', 'Проверить снова'),
    'connection': ('Подключение', 'Добавь своё подключение', 'connection', 'Настроить подключение'),
}


def host_facts():
    facts = {'windows': os.name == 'nt', 'x64': False,
             'whpx': False, 'ram_total_mib': 0, 'ram_available_mib': 0,
             'logical_processors': os.cpu_count() or 1, 'admin': False}
    if os.name != 'nt':
        return facts

    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD),
                    *[(name, ctypes.c_ulonglong) for name in (
                        'total', 'available', 'page_total', 'page_available',
                        'virtual_total', 'virtual_available', 'extended_available')]]

    memory = MemoryStatus()
    memory.length = ctypes.sizeof(memory)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    machine, native = wintypes.WORD(), wintypes.WORD()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.WORD),
                                      ctypes.POINTER(wintypes.WORD)]
    kernel.IsWow64Process2.restype = wintypes.BOOL
    if kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(machine), ctypes.byref(native)):
        facts['x64'] = native.value == 0x8664 and sys.maxsize > 2 ** 32
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        facts.update(ram_total_mib=memory.total // 1048576,
                     ram_available_mib=memory.available // 1048576)
    facts['admin'] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    try:
        whp = ctypes.WinDLL('WinHvPlatform.dll')
        present, written = wintypes.BOOL(), wintypes.UINT()
        whp.WHvGetCapability.argtypes = [ctypes.c_int, wintypes.LPVOID, wintypes.UINT,
                                        ctypes.POINTER(wintypes.UINT)]
        whp.WHvGetCapability.restype = ctypes.c_long
        facts['whpx'] = whp.WHvGetCapability(0, ctypes.byref(present), ctypes.sizeof(present),
                                            ctypes.byref(written)) == 0 and bool(present)
    except OSError:
        pass
    return facts


def preflight(root, host=None, *, full=True):
    root = root.resolve(strict=True)
    host = host_facts() if host is None else host
    checks = {}

    def check(name, passed, detail):
        title, _, action, label = CHECK_INFO[name]
        checks[name] = {'passed': bool(passed), 'detail': detail, 'title': title,
                        'action': action, 'action_label': label}

    check('windows_x64', host['windows'] and host['x64'], 'Требуется Windows x64.')
    check('whpx', host['whpx'], 'Ускорение виртуальных машин доступно.' if host['whpx'] else
          'В компонентах Windows включи «Платформа гипервизора Windows» и перезагрузи ПК. Программа не меняет это автоматически.')
    try:
        settings = load_settings(root)
        check('settings', True, 'Публичные параметры корректны.')
    except (OSError, ValueError):
        settings = dict(DEFAULTS)
        check('settings', False, 'Некорректный config/runtime.json. Исправьте настройки.')
    try:
        display_settings = windows_settings.load_settings(root)
    except (OSError, ValueError):
        display_settings = dict(windows_settings.DEFAULTS)
        check('settings', False, 'Некорректное разрешение Windows. Открой настройки и сохрани их заново.')
    try:
        viewer_settings = load_viewer_settings(root)
    except (OSError, ValueError):
        viewer_settings = dict(VIEWER_DEFAULTS)
        check('settings', False, 'Некорректные настройки окна. Открой настройки и сохрани их заново.')
    try:
        file_settings = file_sharing.load_settings(root)
    except (OSError, ValueError, RuntimeError):
        file_settings = file_sharing.validate_settings({})
        check('settings', False, 'Некорректный доступ к файлам ПК. Открой настройки и сохрани их заново.')
    already_running = False
    guests = {}
    states = {}
    recovery_available = False
    runtime_state = {'work': 'unknown', 'gateway': 'unknown', 'control_available': False}
    try:
        guests = runtime.discover_guests(root) if host['windows'] else {}
        already_running = set(guests) == {'gateway', 'work'}
        states = {role: runtime.read_json(Path(record['state'])) for role, record in guests.items()}
        known_states = {'starting', 'running', 'restarting', 'reconnecting', 'stopping', 'connection-failed',
                        'control-unavailable-guest-running', 'shutdown-unconfirmed', 'failed'}
        runtime_state.update({role: states[role].get('status') if role in states and states[role].get('status') in known_states
                             else 'unknown' if role in states else 'stopped'
                              for role in ('work', 'gateway')})
        usable = (not guests or already_running) and all(
            state.get('test_only') is not True and state.get('status') in
            ('starting', 'running', 'restarting', 'reconnecting', 'stopping') for state in states.values())
        check('runtime_state', usable, 'Сначала заверши текущую проверку или останови сеанс шлюза.' if not usable else
              'Обе VM уже работают.' if already_running else 'Конфликтующих процессов этой копии не найдено.')
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        check('runtime_state', False, 'Состояние VM не подтверждено. Проверьте журналы этой копии.')
    startup = {}
    supervisor_active = False
    try:
        active_path, active = runtime.active_receipt(root)
        supervisor = active.get('supervisor')
        supervisor_active = (runtime.valid_controller(root, supervisor)
            and runtime.same_identity(supervisor, runtime.process_identity(supervisor['pid'])))
        if active.get('phase') in ('starting', 'ready', 'restarting', 'stopping', 'stopped', 'error'):
            startup = {'receipt': active_path.relative_to(root).as_posix(),
                       'phase': active['phase'], 'supervisor_active': supervisor_active}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
        pass
    if not checks['runtime_state']['passed']:
        try:
            recovery_available = runtime.recovery_snapshot(root)['available']
            if supervisor_active:
                recovery_available = False
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            pass
        if recovery_available:
            checks['runtime_state'].update(action='recover_session', action_label='Восстановить сеанс',
                detail='Windows и её контроллер уже завершились. Восстанови прерванный сеанс: при необходимости будет сброшен только одноразовый шлюз. Диск Windows и подключения сохранятся.')
    required_memory = settings['work_memory_mib'] + settings['gateway_memory_mib'] + 1024
    check('memory', already_running or host['ram_available_mib'] >= required_memory,
          'Память для работающей пары уже выделена.' if already_running else
          f"Нужно {required_memory} МиБ свободной памяти; доступно {host['ram_available_mib']} МиБ. "
          'Закрой лишние приложения или уменьши память Windows в настройках.')
    free = shutil.disk_usage(root).free
    check('free_space', free >= 2 * 1024 ** 3,
          f'Свободно {free // 1048576} МиБ; для запуска требуется резерв не менее 2048 МиБ.')
    gateway_version = None
    if full:
        missing = [name for name in RUNTIME_FILES if name.startswith('runtime/') and not (root / name).is_file()]
        check('runtime_files', not missing, 'Не хватает: ' + ', '.join(missing) if missing else 'Python и QEMU найдены.')
        missing = [name for name in RUNTIME_FILES if name.startswith('scripts/') and not (root / name).is_file()]
        check('application_files', not missing,
              'Распакуй полный комплект поверх файлов приложения, сохранив личные диски и настройки. Не хватает: ' + ', '.join(missing)
              if missing else 'Файлы приложения на месте.')
        missing_guests = [name for name in GUEST_FILES if not (root / name).is_file()]
        check('guest_files', not missing_guests,
              'Нужны установленные диски и файл прошивки: ' + ', '.join(missing_guests)
              if missing_guests else 'Диск Windows и данные прошивки найдены.')
        engine = root / RUNTIME_FILES[1]
        valid_engine = False
        if engine.is_file():
            try:
                with engine.open('rb') as reader:
                    valid_engine = hashlib.file_digest(reader, 'sha256').hexdigest() == ENGINE_SHA256
            except OSError:
                pass
        check('engine_identity', valid_engine, 'QEMU 11.1.0 проверен.' if valid_engine else
              'Файл QEMU отсутствует или повреждён. Нажми «Восстановить Python и QEMU».')
        try:
            package = inspect_current(root)
            gateway_version = package['version']
            check('gateway_package', True, 'Установлен шлюз ' + gateway_version + '. Структура корректна; полная проверка SHA256 выполняется перед запуском.')
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            check('gateway_package', False, 'Не найдена исправная версия шлюза. Восстанови её из пакета компонентов.')
        powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        check('powershell', powershell.is_file(), 'Windows PowerShell найден.' if powershell.is_file() else 'Для запуска нужен встроенный Windows PowerShell 5.1.')
    connection = {'selected_profile': None, 'applied_revision_id': None, 'verified': False}
    try:
        connection['selected_profile'] = ProfileStore(root / 'profiles').selected_summary()
        selected = connection['selected_profile']
        check('connection', selected is not None and not selected['needs_credentials'],
              'Пароль выбранного подключения недоступен. Открой раздел «Подключение» и введи его заново.'
              if selected and selected['needs_credentials'] else 'Подключение выбрано для следующего запуска.' if selected else
              'Открой раздел «Подключение», добавь сервер и выбери его для запуска.')
    except (OSError, ValueError, RuntimeError):
        check('connection', False, 'Не удалось прочитать подключение. Открой раздел «Подключение» и проверь сохранённый профиль.')
    network_verified = False
    display_state = {'status': 'not_running'}
    if guests:
        try:
            runtime_state['control_available'] = all(runtime.controller_live(root, item) for item in guests.values())
            if 'work' in guests:
                work_state = runtime.read_json(Path(guests['work']['state']))
                display_state = {'status': 'pending'}
                display = work_state.get('windows_display', {})
                if display.get('status') in ('applied', 'failed') and display.get('generation') == work_state.get('generation'):
                    # Check against this session's selected standard mode, even after the panel setting changes.
                    requested = windows_settings.validate_request(display['requested']) if 'requested' in display else display_settings
                    receipt = windows_settings.validate_receipt(json.dumps(display.get('receipt')), requested, work_state.get('session_nonce'))
                    display_state['requested'] = {key: requested[key] for key in ('width', 'height')}
                    display_state = {'status': 'applied' if receipt['success'] else 'failed', 'receipt': receipt}
                elif display.get('status') == 'unconfirmed':
                    display_state = {'status': 'unconfirmed'}
            if 'gateway' in guests:
                gateway_state = runtime.read_json(Path(guests['gateway']['state']))
                network_verified = runtime.gateway_ready(root, guests['gateway'])
                connection.update(applied_revision_id=gateway_state.get('profile_revision_id'),
                                  applied_profile_id=gateway_state.get('profile_id'))
                selected = connection['selected_profile']
                connection['verified'] = bool(network_verified and selected
                    and selected['profile_id'] == connection['applied_profile_id']
                    and selected['revision_id'] == connection['applied_revision_id'])
                if selected and connection['verified']:
                    selected['state'] = 'verified'
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            runtime_state['control_available'] = False
    ok = all(item['passed'] for item in checks.values())
    blockers = [{'code': name, 'title': CHECK_INFO[name][1], 'detail': item['detail'],
                 'action': item['action'], 'action_label': item['action_label']}
                for name, item in checks.items() if not item['passed']]
    return {'ok': ok, 'report_kind': 'preflight' if full else 'runtime_status',
            'message': (('Windows уже запущена.' if already_running else 'Всё готово к запуску Windows.')
                        if full else 'Состояние сеанса обновлено.') if ok else blockers[0]['title'],
            'root': str(root), 'settings': settings, 'windows_settings': display_settings, 'viewer_settings': viewer_settings,
            'file_settings': file_settings,
            'host_files': states.get('work', {}).get('host_files', {'status': 'off'}) if guests else {'status': 'off'},
            'windows_display': display_state, 'host': host, 'checks': checks,
            'blockers': blockers, 'checked_utc': runtime.utc_now(),
            'already_running': already_running, 'network_verified': network_verified,
            'runtime': runtime_state, 'startup': startup, 'connection': connection,
            'capabilities': {'stop': bool(guests),
                              'recover_session': recovery_available,
                             'test_profile': bool(gateway_version) and not guests and checks['runtime_state']['passed'],
                             'prepare_host': (root / 'scripts/Prepare-Host.ps1').is_file()},
            'gateway_version': gateway_version,
            'network_acceptance_scope': 'current_session' if network_verified else 'unverified',
            'connection_error': 'CONNECTION_LOST' if runtime_state['gateway'] == 'reconnecting' else None,
            'network_warning': 'Связь восстанавливается. Windows продолжает работать.' if runtime_state['gateway'] == 'reconnecting' and 'work' in guests else
                               '' if network_verified else 'Подключение будет проверено шлюзом перед запуском Windows.',
            'secret_values_exposed': False, 'network_requests_made': False}


def runtime_status(root, host=None):
    """Observe the session without repeating installation/integrity checks."""
    return preflight(root, host=host, full=False)


def show_desktop(root):
    work = runtime.discover_guests(root).get('work')
    identity = work.get('process') if work else None
    if not identity or not runtime.same_identity(identity, runtime.process_identity(identity['pid'])):
        return {'ok': False, 'message': 'Работающая Windows не найдена. Обнови состояние панели.'}
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    windows = []

    @callback_type
    def inspect(window, unused):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value == identity['pid'] and user.IsWindowVisible(window):
            windows.append(window)
        return True

    user.EnumWindows(inspect, 0)
    if not windows or not runtime.same_identity(identity, runtime.process_identity(identity['pid'])):
        return {'ok': False, 'message': 'Окно Windows ещё не появилось. Повтори через несколько секунд.'}
    if work.get('state') and runtime.read_json(Path(work['state'])).get('window_parked') is True:
        # Restoring it before the guest enables its tablet risks the QEMU window deadlock.
        return {'ok': False, 'message': 'Windows ещё загружается. Окно откроется само через несколько секунд.'}
    user.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user.ShowWindowAsync(windows[0], 9)
    foreground = bool(user.SetForegroundWindow(windows[0]))
    return {'ok': True, 'message': 'Рабочий стол Windows открыт.' if foreground else
            'Окно Windows восстановлено. Выбери его на панели задач.'}


def save_diagnostic_report(root, result):
    folder = root / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError('Diagnostic directory escapes the project root')
    public = {key: result[key] for key in (
        'ok', 'message', 'checks', 'blockers', 'settings', 'host', 'checked_utc',
        'already_running', 'network_verified', 'network_acceptance_scope', 'network_warning')
        if key in result}
    runtime.atomic_json(folder / 'diagnostics-latest.json', public)
    return 'logs/diagnostics-latest.json'


def save_settings(root, value):
    if not isinstance(value, dict):
        raise ValueError('Нужен объект настроек.')
    value = dict(value)
    display = windows_settings.validate_settings(value.pop('windows')) if 'windows' in value else None
    viewer = validate_viewer_settings(value.pop('viewer')) if 'viewer' in value else None
    files = file_sharing.validate_settings(value.pop('files')) if 'files' in value else None
    settings = validate_settings(value)
    with runtime.ProjectLock(root) as acquired:
        if not acquired:
            raise ValueError('Сейчас выполняется запуск или работа VM. Повторите после штатного завершения.')
        if runtime.engine_processes(root):
            raise ValueError('Сначала завершите VM штатно. Настройки применяются при следующем запуске.')
        folder = root / 'config'
        folder.mkdir(parents=True, exist_ok=True)
        if not folder.resolve().is_relative_to(root.resolve()):
            raise ValueError('Settings directory escapes the project root')
        try:
            load_settings(root)
        except ValueError:
            backup = folder / ('runtime.invalid-' + uuid.uuid4().hex + '.json')
            with (folder / 'runtime.json').open('rb') as source, backup.open('xb') as target:
                shutil.copyfileobj(source, target)
        runtime.atomic_json(folder / 'runtime.json', settings)
        if display is not None:
            try:
                windows_settings.load_settings(root)
            except ValueError:
                backup = folder / ('windows.invalid-' + uuid.uuid4().hex + '.json')
                with (folder / 'windows.json').open('rb') as source, backup.open('xb') as target:
                    shutil.copyfileobj(source, target)
            try:
                runtime.atomic_json(folder / 'windows.json', display)
            except OSError as error:
                raise ValueError('Память и процессоры сохранены, но разрешение сохранить не удалось. Проверь доступ к папке и повтори сохранение.') from error
        if viewer is not None:
            try:
                load_viewer_settings(root)
            except ValueError:
                backup = folder / ('viewer.invalid-' + uuid.uuid4().hex + '.json')
                with (folder / 'viewer.json').open('rb') as source, backup.open('xb') as target:
                    shutil.copyfileobj(source, target)
            try:
                runtime.atomic_json(folder / 'viewer.json', viewer)
            except OSError as error:
                raise ValueError('Ресурсы сохранены, но настройки окна сохранить не удалось. Проверь доступ к папке и повтори сохранение.') from error
        if files is not None:
            with _pinned_directory(folder, create=False):
                path = folder / 'files.json'
                _plain(path)
                try:
                    file_sharing.load_settings(root)
                except ValueError:
                    backup = folder / ('files.invalid-' + uuid.uuid4().hex + '.json')
                    with path.open('rb') as source, backup.open('xb') as target:
                        shutil.copyfileobj(source, target)
                try:
                    runtime.atomic_json(path, files)
                except OSError as error:
                    raise ValueError('Ресурсы сохранены, но доступ к файлам сохранить не удалось. Проверь папку программы и повтори сохранение.') from error
    return settings


def windows_preferences(root):
    result = {'windows_settings': windows_settings.load_settings(root),
              'windows_disk': {'relative_path': 'images/workvm-empty.qcow2', 'resize_available': False}}
    try:
        _plain(root / 'images')
        _plain(root / 'images/workvm-empty.qcow2')
        with (root / 'images/workvm-empty.qcow2').open('rb') as source:
            header = source.read(32)
        if len(header) == 32 and header[:4] == b'QFI\xfb':
            result['windows_disk']['virtual_bytes'] = int.from_bytes(header[24:32], 'big')
    except OSError:
        pass
    return result


START_MESSAGES = {
    'ready': 'Gateway и Windows запущены.',
    'stopped': 'Windows и gateway уже завершились штатно.',
    'uac_cancelled': 'Запрос прав администратора отменён. Повторите запуск и подтвердите UAC.',
    'execution_policy': 'PowerShell запретил запуск скрипта. Политика не изменялась; проверьте её с владельцем ПК.',
    'elevation_failed': 'Повышенный процесс не подтвердил выполнение скрипта запуска.',
    'missing_runtime': 'Обязательный файл runtime отсутствует или исчез до запуска.',
    'runtime_error': 'Supervisor сообщил об ошибке запуска. Откройте указанный receipt.',
    'supervisor_exited': 'Supervisor завершился до подтверждения запуска.',
    'startup_timeout': 'Запуск не подтверждён в отведённое время; работающие VM сохранены.',
    'startup_pending': 'Ожидание запуска превышено. Проверьте UAC и logs; работающие VM сохранены.',
    'result_channel_invalid': 'Результат запуска некорректен. Проверьте целостность комплекта.',
    'wrapper_failed': 'Скрипт запуска не вернул подтверждённый результат.',
}


def start(root):
    from package_integrity import verify_initial
    integrity = verify_initial(root)
    if not integrity['ok']:
        return integrity
    report = preflight(root)
    if not report['ok']:
        return report
    logs = root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    if not logs.resolve().is_relative_to(root.resolve()):
        raise ValueError('Log directory escapes the project root')
    code = 'runtime_error'
    with _pinned_directory(logs):
        directory = logs / ('runtime-launch-' + uuid.uuid4().hex)
        directory.mkdir()
    receipt = runtime.safe_receipt_path(root, directory / 'receipt.json')
    # File handles keep a detached supervisor independent of the desktop backend's pipes.
    with (directory / 'supervisor.stdout.log').open('xb') as output, \
            (directory / 'supervisor.stderr.log').open('xb') as errors:
        process = subprocess.Popen(
            [str(root / 'runtime/python-3.12.14/python.exe'), '-X', 'utf8', '-I', '-S', '-B',
             str(root / 'scripts/launch_runtime.py'), '--project', str(root), '--receipt', str(receipt)],
            cwd=root, stdout=output, stderr=errors, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        expected = runtime.process_identity(process.pid)
        deadline = time.monotonic() + 270
        while True:
            value = None
            try:
                if receipt.is_file():
                    if receipt.stat().st_size > 256 * 1024:
                        raise ValueError('Oversized startup receipt')
                    value = runtime.read_json(receipt)
            except (OSError, ValueError, RuntimeError):
                code = 'result_channel_invalid'
                break
            if value is not None:
                if (not isinstance(value, dict) or type(value.get('schema')) is not int
                        or value['schema'] != 1 or not runtime.same_path(value.get('project', ''), root)
                        or value.get('phase') not in ('starting', 'ready', 'stopped', 'error', 'stopping', 'restarting')):
                    code = 'result_channel_invalid'
                    break
                if value['phase'] in ('ready', 'stopped', 'error'):
                    if (not runtime.valid_controller(root, expected)
                            or not runtime.same_identity(expected, value.get('supervisor'))):
                        code = 'result_channel_invalid'
                    else:
                        code = 'runtime_error' if value['phase'] == 'error' or process.poll() not in (None, 0) else value['phase']
                    break
            if process.poll() is not None:
                code = 'supervisor_exited'
                break
            if time.monotonic() >= deadline:
                code = 'startup_timeout'
                break
            time.sleep(.2)
    name = receipt.relative_to(root).as_posix()
    result = {'ok': code in ('ready', 'stopped'), 'code': code,
              'message': START_MESSAGES[code] + '\n' + name, 'receipt': name}
    if not result['ok'] and process.poll() is None:
        result['outcome_unknown'] = True
    result.update(network_verified=False,
                  network_acceptance_scope=report.get('network_acceptance_scope', 'none'),
                  network_warning=report.get('network_warning', 'Подключение текущего сеанса не подтверждено.'))
    if result.get('ok'):
        current = preflight(root)
        result.update({key: current[key] for key in ('network_verified', 'network_acceptance_scope',
                                                    'network_warning', 'connection', 'runtime') if key in current})
    return result


CONNECTION_ERRORS = {
    'SESSION_MEDIA_INVALID': 'Не удалось передать настройки шлюзу. Повтори запуск после его выключения.',
    'GATEWAY_VERSION_MISMATCH': 'Версия шлюза не совпадает с приложением. Восстанови компоненты шлюза.',
    'PROXY_CONFIG_INVALID': 'Шлюз отклонил конфигурацию подключения. Проверь протокол и параметры.',
    'NETWORK_DEVICES_UNAVAILABLE': 'Сетевые устройства шлюза не появились. Штатно заверши сеанс и повтори запуск.',
    'NETWORK_SETUP_FAILED': 'Не удалось подготовить изолированную сеть шлюза.',
    'NFT_RULES_FAILED': 'Шлюз не смог включить сетевую защиту. Восстанови шлюз.',
    'INTERFACE_SETUP_FAILED': 'Не удалось настроить сетевые устройства шлюза. Восстанови шлюз.',
    'ENDPOINT_ROUTE_FAILED': 'Не удалось настроить маршрут к серверу. Проверь адрес подключения.',
    'FORWARDING_SETUP_FAILED': 'Не удалось включить защищённую передачу данных через шлюз. Восстанови шлюз.',
    'IPV6_ADDRESS_FAILED': 'Шлюз не смог подготовить адреса изолированной сети. Восстанови шлюз в диагностике.',
    'PROXY_SERVICE_FAILED': 'Служба подключения не запустилась. Восстанови шлюз.',
    'CONNECTION_FAILED': 'Сервер не подтвердил подключение. Проверь адрес, порт, пароль и параметры TLS.',
    'CONNECTION_LOST': 'Подключение оборвалось. Прямой выход Windows в интернет остаётся закрыт.',
    'HEALTH_TIMEOUT': 'Шлюз перестал подтверждать соединение. Прямой выход закрыт.',
    'STARTUP_TIMEOUT': 'Шлюз не подтвердил готовность вовремя.',
}


def _connection_test_state(root, expected, view):
    from file_snapshot import locked_read
    if not runtime.valid_controller(root, expected):
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate test state field')
            result[key] = value
        return result
    candidates = []
    for path in (root / 'logs').glob('guest-gateway-boot-*/state.json'):
        try:
            path = runtime.safe_state_path(root, path)
            if not re.fullmatch('guest-gateway-boot-[0-9a-f]{32}', path.parent.name):
                continue
            with _pinned_directory(path.parent, create=False):
                _plain(path)
                with locked_read(path) as reader:
                    _plain(path)
                    raw = reader.read(65537)
                if len(raw) > 65536:
                    continue
                candidate = json.loads(raw, object_pairs_hook=unique)
            if (isinstance(candidate, dict) and type(candidate.get('schema')) is int and candidate['schema'] == 2
                    and candidate.get('test_only') is True and candidate.get('guest') == 'gateway-boot'
                    and candidate.get('nonce') == path.parent.name[len('guest-gateway-boot-'):]
                    and candidate.get('profile_id') == view['profile_id']
                    and candidate.get('profile_revision_id') == view['revision_id']
                    and runtime.same_identity(expected, candidate.get('controller'))):
                candidates.append(candidate)
                if len(candidates) > 1:
                    return None
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError):
            continue
    return candidates[0] if len(candidates) == 1 else None


def test_connection(root, request):
    if not isinstance(request, dict) or set(request) != {'profile_id'}:
        raise ValueError('Supply one profile_id for a connection test')
    view = ProfileStore(root / 'profiles').get_view(request['profile_id'])
    if view['needs_credentials']:
        return {'ok': False, 'code': 'credentials_needed', 'verified': False,
                'message': 'Пароль подключения недоступен. Введи его заново в разделе «Подключение».'}
    host = host_facts()
    settings = load_settings(root)
    if not host['windows'] or not host['x64'] or not host['whpx']:
        return {'ok': False, 'code': 'host_not_ready', 'message': 'Сначала подготовь этот ПК в разделе «Диагностика».'}
    if host['ram_available_mib'] < settings['gateway_memory_mib'] + 512:
        return {'ok': False, 'code': 'low_memory', 'message': 'Не хватает свободной памяти для проверки шлюза.'}
    with runtime.ProjectLock(root) as acquired, _pinned_directory(root / 'logs'):
        if not acquired or runtime.engine_processes(root):
            return {'ok': False, 'code': 'runtime_busy', 'message': 'Перед проверкой подключения штатно выключи Windows и шлюз.'}
        inspect_current(root)
        process = subprocess.Popen(
            [str(root / 'runtime/python-3.12.14/python.exe'), '-I', '-S', '-B',
             str(root / 'scripts/gateway_controller.py'), '--project', str(root),
             '--test-profile', view['profile_id']], cwd=root, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW)
        expected = runtime.process_identity(process.pid)
        try:
            process.wait(timeout=300)
        except subprocess.TimeoutExpired:
            return {'ok': False, 'code': 'test_pending',
                    'message': 'Проверка ещё не завершилась. Открой диагностику, чтобы увидеть состояние шлюза.',
                    'verified': False}
        state = _connection_test_state(root, expected, view)
        verified = bool(state and state.get('status') == 'exited' and process.returncode == 0 and state.get('connection_test_passed') is True
                        and state.get('guest_shutdown') is True and state.get('carrier_removed') is True
                        and state.get('overlay_removed') is True
                        and not runtime.engine_processes(root))
        error = state.get('error') if state and isinstance(state.get('error'), str) else None
        return {'ok': verified, 'code': 'connection_verified' if verified else error if error in CONNECTION_ERRORS else 'connection_failed',
                'message': 'Подключение проверено. Оно применится при следующем запуске Windows.' if verified else
                    CONNECTION_ERRORS.get(error, 'Шлюз не подтвердил подключение. Открой диагностику.'),
                'profile_id': view['profile_id'], 'revision_id': view['revision_id'],
                'applied_revision_id': None, 'verified': verified}


def stop_runtime(root):
    process = subprocess.Popen([str(root / 'runtime/python-3.12.14/python.exe'), '-I', '-S', '-B',
        str(root / 'scripts/launch_runtime.py'), '--project', str(root), '--stop'], cwd=root,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        _stdout, _stderr = process.communicate(timeout=480)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'code': 'shutdown_pending',
                'message': 'Выключение ещё не подтверждено. Работающие VM сохранены; проверь диагностику.'}
    stopped = process.returncode == 0 and not runtime.engine_processes(root)
    return {'ok': stopped, 'code': 'stopped' if stopped else 'shutdown_not_confirmed',
            'message': 'Windows и шлюз выключены.' if stopped else
                'Штатное выключение не подтверждено. VM сохранены. Проверь состояние Windows и диагностику.'}


def repair_gateway(root):
    from gateway_package import repair_package
    from file_snapshot import locked_read
    missing = {'ok': False, 'code': 'package_missing',
               'message': 'Не найден пакет восстановления шлюза. Для восстановления нужен исходный ZIP этого выпуска. Данные Windows и подключений сохранены.'}
    if not (root / 'runtime').exists():
        return missing
    with _pinned_directory(root / 'runtime', create=False):
        catalog_path = root / 'runtime/gateway-recovery.json'
        _plain(catalog_path)
        package_path = root / 'runtime/gateway-recovery.zip'
        _plain(package_path)
        if not catalog_path.is_file() or not package_path.is_file():
            return missing
        with locked_read(catalog_path) as reader:
            catalog = json.loads(reader.read(8193))
        if (not isinstance(catalog, dict) or set(catalog) != {'schema', 'file', 'sha256'}
                or type(catalog['schema']) is not int or catalog['schema'] != 1
                or catalog['file'] != 'gateway-recovery.zip'):
            raise ValueError('Invalid gateway recovery catalog')
        result = repair_package(root, package_path, catalog['sha256'])
    return {'ok': True, 'code': 'gateway_repaired', 'gateway_version': result['version'],
            'message': 'Шлюз восстановлен. Настройки подключений и данные Windows сохранены.'}


PROFILE_ACTIONS = {
    'list-profiles': set(),
    'get-profile-view': {'profile_id'},
    'save-profile': {'profile_id', 'profile'},
    'select-profile': {'profile_id'},
    'import-profile': {'uri', 'profile'},
    'export-profile': {'profile_id', 'destination', 'include_secrets'},
}


def read_request():
    body = sys.stdin.buffer.read(65537)
    if len(body) > 65536:
        raise ValueError('Request exceeds 64 KiB')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate request field')
            result[key] = value
        return result

    try:
        value = json.loads(body, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Invalid JSON request') from None
    if not isinstance(value, dict):
        raise ValueError('Request object required')
    return value


def profile_action(root, action, request):
    if not isinstance(request, dict) or set(request) - PROFILE_ACTIONS[action]:
        raise ValueError('Unsupported request fields')
    store = ProfileStore(root / 'profiles')

    def public(view):
        # Only the session controller may later confirm application and connectivity.
        return view | {'state': 'pending' if view['selected'] else 'saved',
                       'applied_revision_id': None, 'verified': False,
                       'capabilities': {'tcp': True, 'udp': view['udp_supported']}}

    result = {'ok': True, 'schema': 1, 'code': 'saved'}
    if action == 'list-profiles':
        return result | {'profiles': [public(item) for item in store.list_profiles()],
                         'message': 'Подключения загружены.'}
    if action == 'get-profile-view':
        view = store.get_view(request['profile_id'])
        message = 'Настройки подключения загружены. Пароль не отображается.'
    elif action == 'save-profile':
        view = store.save(request['profile'], profile_id=request.get('profile_id'))
        message = 'Подключение сохранено. Работающий сеанс не изменён.'
    elif action == 'select-profile':
        view = store.select(request['profile_id'])
        message = 'Подключение выбрано для следующего запуска.'
    elif action == 'import-profile':
        if ('uri' in request) == ('profile' in request):
            raise ValueError('Supply one connection link or exported profile')
        profile = import_profile_uri(request['uri']) if 'uri' in request else request['profile']
        view = store.save(profile)
        message = 'Подключение импортировано. Выбери его для следующего запуска.'
    elif action == 'export-profile':
        exported = store.export_to(request['profile_id'], request['destination'],
                                   include_secrets=request.get('include_secrets', False))
        return result | {'exported': exported, 'message': 'Подключение экспортировано.'}
    return result | {'profile': public(view), 'message': message}


def profile_error(error):
    if isinstance(error, StoreBusy):
        return 'Сейчас выполняется другое действие с подключениями. Повтори после его завершения.'
    if isinstance(error, SecretsUnavailable):
        return 'Пароль недоступен для этого пользователя Windows. Введи его заново в настройках подключения.'
    if isinstance(error, StoreError):
        if 'already exists' in str(error).lower():
            return 'Файл уже существует. Выбери новое имя для экспорта.'
        return 'Не удалось открыть или сохранить подключения. Проверь доступ к папке. Существующие данные сохранены.'
    messages = {
        'Hysteria2 authentication is required': 'Введи пароль Hysteria2.',
        'Port must be an integer between 1 and 65535': 'Порт должен быть числом от 1 до 65535.',
        'IPv6 endpoints are not supported in this version': 'В этой версии укажи адрес IPv4 или имя сервера.',
        'Invalid server name': 'Проверь имя сервера. Вводи адрес без протокола и пути.',
        'Invalid IPv4 address': 'Проверь адрес IPv4.',
        'Unsupported connection protocol': 'Выбери Hysteria2, SOCKS5, HTTP или HTTPS CONNECT.',
        'Enter credentials again when changing protocol or obfuscation':
            'При смене протокола или маскировки введи пароль заново.',
        'Request exceeds 64 KiB': 'Запрос слишком большой. Максимальный размер 64 КиБ.',
    }
    return messages.get(str(error),
        'Проверь поля подключения или формат импорта. Некорректные данные не сохранены.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--action', choices=['diagnose', 'status', 'show-desktop', 'get-settings', 'save-settings', 'start', 'recover-session',
                                            'stop', 'test-profile', 'repair-gateway',
                                            *PROFILE_ACTIONS], required=True)
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.action == 'test-profile':
            result = test_connection(root, read_request())
        elif args.action == 'stop':
            result = stop_runtime(root)
        elif args.action == 'recover-session':
            result = runtime.recover_session(root)
        elif args.action == 'repair-gateway':
            result = repair_gateway(root)
        elif args.action in PROFILE_ACTIONS:
            request = {} if args.action == 'list-profiles' else read_request()
            result = profile_action(root, args.action, request)
        elif args.action == 'show-desktop':
            result = show_desktop(root)
        elif args.action == 'status':
            result = runtime_status(root)
        elif args.action == 'diagnose':
            from package_integrity import verify_initial
            integrity = verify_initial(root)
            result = preflight(root) if integrity['ok'] else dict(integrity)
            result['package_check'] = integrity
            try:
                result['diagnostic_report'] = save_diagnostic_report(root, result)
            except (OSError, ValueError):
                result['diagnostic_report_error'] = 'Не удалось сохранить отчёт. Проверь доступ к папке программы.'
        elif args.action == 'get-settings':
            try:
                result = {'ok': True, 'settings': load_settings(root)}
            except ValueError:
                result = {'ok': True, 'settings': dict(DEFAULTS), 'recovery_required': True,
                          'message': 'Настройки повреждены. При сохранении исходный файл будет сохранён рядом.'}
            try:
                result.update(windows_preferences(root))
            except ValueError:
                result.update(windows_settings=dict(windows_settings.DEFAULTS), recovery_required=True,
                              message='Настройки разрешения повреждены. При сохранении исходный файл останется рядом.')
            try:
                result['viewer_settings'] = load_viewer_settings(root)
            except ValueError:
                result.update(viewer_settings=dict(VIEWER_DEFAULTS), recovery_required=True,
                              message='Настройки окна повреждены. При сохранении исходный файл останется рядом.')
            result['host'] = host_facts()
            try:
                result['file_settings'] = file_sharing.load_settings(root)
            except ValueError:
                result.update(file_settings=file_sharing.validate_settings({}), recovery_required=True,
                              message='Настройки файлов повреждены. При сохранении исходный файл останется рядом.')
        elif args.action == 'save-settings':
            result = {'ok': True, 'settings': save_settings(root, read_request()),
                      'message': 'Настройки сохранены для следующего запуска.'}
            result.update(windows_preferences(root))
            result['viewer_settings'] = load_viewer_settings(root)
            result['file_settings'] = file_sharing.load_settings(root)
        else:
            result = start(root)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        result = {'ok': False, 'message': profile_error(error) if args.action in PROFILE_ACTIONS or args.action == 'test-profile' else str(error)}
        if args.action == 'recover-session':
            result.update(code='recovery_refused', message='Восстановление не подтверждено: Windows или служебные процессы ещё работают либо состояние файлов не удалось проверить. VM сохранены. Повтори диагностику после завершения текущей операции.')
    result.setdefault('schema', 1)
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
