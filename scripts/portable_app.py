"""JSON backend for the small desktop EXE. Diagnostics never read private profiles."""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import launch_runtime as runtime
from runtime_config import DEFAULTS, load_settings, validate_settings

ENGINE_SHA256 = '47d57a6072e0bb3bd98f87926eb129eb1736dfe818c67b3b81ef7ce4edd0b3cd'
RUNTIME_FILES = ('runtime/python-3.12.14/python.exe',
                 'runtime/qemu-11.1.0/qemu-system-x86_64.exe',
                 'runtime/qemu-11.1.0/share/edk2-x86_64-code.fd',
                 'scripts/launch_runtime.py', 'scripts/control_guest_install.py',
                 'scripts/runtime_network_plan.py', 'scripts/runtime_config.py',
                 'scripts/Start-Runtime.ps1')
GUEST_FILES = ('images/workvm-empty.qcow2', 'images/gateway-empty.qcow2', 'state/workvm-vars.fd')


def host_facts():
    facts = {'windows': os.name == 'nt', 'x64': False,
             'whpx': False, 'ram_total_mib': 0, 'ram_available_mib': 0, 'admin': False}
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


def preflight(root, host=None):
    root = root.resolve(strict=True)
    host = host_facts() if host is None else host
    checks = {}

    def check(name, passed, detail):
        checks[name] = {'passed': bool(passed), 'detail': detail}

    check('windows_x64', host['windows'] and host['x64'], 'Требуется Windows x64.')
    check('whpx', host['whpx'], 'Windows Hypervisor Platform должен быть уже доступен; программа его не включает.')
    try:
        settings = load_settings(root)
        check('settings', True, 'Публичные параметры корректны.')
    except (OSError, ValueError):
        settings = dict(DEFAULTS)
        check('settings', False, 'Некорректный config/runtime.json. Исправьте настройки.')
    required_memory = settings['work_memory_mib'] + settings['gateway_memory_mib'] + 1024
    check('memory', host['ram_available_mib'] >= required_memory,
          f"Нужно свободно не менее {required_memory} МиБ; доступно {host['ram_available_mib']} МиБ.")
    free = shutil.disk_usage(root).free
    check('free_space', free >= 2 * 1024 ** 3,
          f'Свободно {free // 1048576} МиБ; для запуска требуется резерв не менее 2048 МиБ.')
    missing = [name for name in RUNTIME_FILES if not (root / name).is_file()]
    check('runtime_files', not missing, 'Не хватает: ' + ', '.join(missing) if missing else 'Bundled runtime на месте.')
    missing_guests = [name for name in GUEST_FILES if not (root / name).is_file()]
    check('guest_files', not missing_guests,
          'Нужны отдельные установленные диски и UEFI state: ' + ', '.join(missing_guests)
          if missing_guests else 'Диски и UEFI state найдены.')
    engine = root / RUNTIME_FILES[1]
    valid_engine = False
    if engine.is_file():
        try:
            with engine.open('rb') as reader:
                valid_engine = hashlib.file_digest(reader, 'sha256').hexdigest() == ENGINE_SHA256
        except OSError:
            pass
    check('engine_identity', valid_engine, 'Нужен проверенный QEMU 11.1.0 с ожидаемым SHA256.')
    try:
        runtime.check_acceptance(root)
        accepted = True
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        accepted = False
    check('network_acceptance', accepted,
          'Для сетевого запуска нужен подтверждённый gateway; без него запуск заблокирован.')
    powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    check('powershell', powershell.is_file(), 'Требуется встроенный Windows PowerShell 5.1; политика скриптов не обходится.')
    ok = all(item['passed'] for item in checks.values())
    return {'ok': ok, 'message': 'Готово к запуску; будет запрошено UAC.' if ok else 'Запуск заблокирован: откройте диагностику.',
            'root': str(root), 'settings': settings, 'host': host, 'checks': checks,
            'private_profile_read': False, 'network_requests_made': False}


def save_settings(root, value):
    settings = validate_settings(value)
    with runtime.ProjectLock(root) as acquired:
        if not acquired:
            raise ValueError('Сейчас выполняется запуск или работа VM. Повторите после штатного завершения.')
        if runtime.engine_processes(root):
            raise ValueError('Сначала завершите VM штатно. Настройки применяются при следующем запуске.')
        folder = root / 'config'
        folder.mkdir(parents=True, exist_ok=True)
        runtime.atomic_json(folder / 'runtime.json', settings)
    return settings


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


def startup_result(root, channel, returncode, stderr):
    value = None
    if channel.is_file():
        try:
            if channel.stat().st_size > 8192:
                raise ValueError('Oversized result')
            value = json.loads(channel.read_text(encoding='utf-8-sig'))
            if (not isinstance(value, dict) or type(value.get('schema')) is not int
                    or value['schema'] != 1 or not isinstance(value.get('code'), str)
                    or value['code'] not in START_MESSAGES):
                raise ValueError('Invalid result schema')
        except (OSError, ValueError):
            value = {'code': 'result_channel_invalid'}
    if value is None:
        policy = any(marker in stderr for marker in ('PSSecurityException', 'about_Execution_Policies',
                                                     'running scripts is disabled'))
        value = {'code': 'execution_policy' if policy else 'wrapper_failed'}
    code = value['code']
    result = {'ok': code in ('ready', 'stopped') and returncode == 0, 'code': code,
              'message': START_MESSAGES[code]}
    if value.get('receipt'):
        try:
            name = value['receipt']
            if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
                raise ValueError('Unsafe receipt')
            receipt = runtime.safe_receipt_path(root, root / name).relative_to(root).as_posix()
            result.update(receipt=receipt, message=result['message'] + '\n' + receipt)
        except (OSError, ValueError, RuntimeError):
            return {'ok': False, 'code': 'result_channel_invalid',
                    'message': START_MESSAGES['result_channel_invalid']}
    native_error = value.get('native_error')
    if type(native_error) is int and 0 <= native_error <= 65535:
        result.update(native_error=native_error, message=result['message'] + f' Windows error: {native_error}.')
    return result


def start(root):
    report = preflight(root)
    if not report['ok']:
        return report
    logs = root / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    if not logs.resolve().is_relative_to(root.resolve()):
        raise ValueError('Log directory escapes the project root')
    channel = logs / ('gui-start-' + uuid.uuid4().hex + '.json')
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    process = subprocess.Popen(
        [str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden',
         '-File', str(root / 'scripts/Start-Runtime.ps1'), '-Project', str(root), '-Gui',
         '-GuiResultPath', str(channel)],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        _stdout, stderr = process.communicate(timeout=190)
    except subprocess.TimeoutExpired:
        # Do not kill a helper or guest on a timeout; existing runtime receipts retain ownership.
        return {'ok': False, 'code': 'startup_pending', 'message': START_MESSAGES['startup_pending']}
    return startup_result(root, channel, process.returncode, stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--action', choices=['diagnose', 'get-settings', 'save-settings', 'start'], required=True)
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.action == 'diagnose':
            result = preflight(root)
        elif args.action == 'get-settings':
            result = {'ok': True, 'settings': load_settings(root)}
        elif args.action == 'save-settings':
            body = sys.stdin.buffer.read(65537)
            if len(body) > 65536:
                raise ValueError('Settings request is too large')
            result = {'ok': True, 'settings': save_settings(root, json.loads(body)),
                      'message': 'Настройки сохранены для следующего запуска.'}
        else:
            result = start(root)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        result = {'ok': False, 'message': str(error)}
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
