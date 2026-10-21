"""Agent CLI for the two owned QEMU guests. QMP is stdio, never a TCP listener."""
import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
from runtime_network_plan import arguments as network_arguments
from collections import deque
from runtime_logs import start_capped_drain, CappedWriter
from runtime_config import load_settings, load_viewer_settings
import launch_runtime as runtime
import windows_guest_settings as windows_settings
import host_file_sharing as file_sharing

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def work_reset_proof(root, report, events, exit_code):
    if type(exit_code) is not int or exit_code != 0:
        return None
    if not any(event.get('event') == 'SHUTDOWN' and event.get('data', {}).get('guest') is True
               and event.get('data', {}).get('reason') == 'guest-reset'
               and type(event.get('generation')) is int and event['generation'] == report['generation'] for event in events):
        return None
    identity=report['process']
    if runtime.same_identity(identity, runtime.process_identity(identity['pid'])):
        return None
    return {'generation':report['generation'],'process':identity,'exit_code':0,'reason':'guest-reset','gone':True}


def work_stop_requested(directory, nonce, controller):
    path=directory/'stop-session.json'
    if not path.exists():
        return False
    marker=runtime.read_json(path)
    if (set(marker) != {'session_nonce','controller'} or marker['session_nonce'] != nonce
            or not runtime.same_identity(controller,marker['controller'])):
        raise RuntimeError('Windows stop marker does not match this controller session')
    return True


parser=argparse.ArgumentParser()
parser.add_argument('--project',required=True)
parser.add_argument('--guest',choices=['work-install','work-resume','work-boot','gateway-install','gateway-boot'],required=True)
parser.add_argument('--gateway-stage',action='store_true',help='Attach the verified read-only gateway delivery; initially no NIC')
parser.add_argument('--network',choices=['none','isolated-lan','gateway'],default='none')
parser.add_argument('--work-test-media',action='store_true')
args=parser.parse_args()
root=Path(args.project).resolve(strict=True)
settings=load_settings(root)
if args.guest != 'work-boot':
    assert ctypes.windll.shell32.IsUserAnAdmin(), 'Use the authorized administrator CLI; no automatic elevation.'
engine=root/'runtime/qemu-11.1.0/qemu-system-x86_64.exe'
assert sha(engine)=='47d57a6072e0bb3bd98f87926eb129eb1736dfe818c67b3b81ef7ce4edd0b3cd'
work=args.guest.startswith('work-')
install=args.guest.endswith('-install')
assert not args.gateway_stage or args.guest=='gateway-boot'
assert not args.gateway_stage or args.network=='none'
assert args.network=='none' or (not install and args.guest in ['work-boot','gateway-boot'])
assert not args.work_test_media or (args.guest=='work-boot' and args.network=='isolated-lan')
disk=root/('images/workvm-empty.qcow2' if work else 'images/gateway-empty.qcow2')
if install:
    expected='eaa46320059414ffe5fa8941b16a68db96d78d7c4a36c8c5e20f020e40e825e1' if work else 'ded3a0fd54d8a86852317ee1b5d4dbcf4832bd628fcc60bdcef0a3db33c15caa'
    assert sha(disk)==expected,'Refusing to install over a non-empty/changed guest disk.'
stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
output=root/'logs'/('guest-'+args.guest+'-'+stamp)
output.mkdir(parents=True,exist_ok=False)
arguments=['-name','WindowsClaude-'+args.guest,'-machine','q35' if work else 'pc','-accel','whpx','-cpu','Westmere','-smp',str(settings['vcpus']),'-m',str(settings['work_memory_mib'] if work else settings['gateway_memory_mib']),'-L','runtime/qemu-11.1.0/share','-nic','none','-display','sdl,gl=off','-vga','std','-serial','none','-parallel','none','-monitor','none','-qmp','stdio']
if work:
    firmware=root/'state/workvm-vars.fd'
    if not firmware.exists():
        original=root/'runtime/qemu-11.1.0/share/edk2-i386-vars.fd'
        with firmware.open('xb') as file:file.write(original.read_bytes())
    arguments+=['-drive','if=pflash,unit=0,format=raw,readonly=on,file=runtime/qemu-11.1.0/share/edk2-x86_64-code.fd','-drive','if=pflash,unit=1,format=raw,file=state/workvm-vars.fd','-drive','if=ide,index=0,format=qcow2,file=images/workvm-empty.qcow2']
    if install or args.guest=='work-resume':
        iso=root/'build/tiny11-normal-26300.9457-ru-RU-20261019T223332091Z/tiny11-packonly-20261019T231246022Z.iso'
        assert sha(iso)=='cad59a1a6303b8a89ac18f9353e1cff44212f5836c83473a345dfe7e199d7743'
        control=root/'state/install-control/windows-answer.img'
        if install:assert sha(control)=='a79d8fb7bf7a32f05b766b0d7e418ad1579be51805a77567d1f47fc2db19ec8f'
        arguments+=['-cdrom',str(iso.relative_to(root)),'-boot','once=d,order=c,strict=on' if install else 'order=c,strict=on','-device','qemu-xhci,id=xhci','-drive','if=none,id=answers,format=raw,file=state/install-control/windows-answer.img','-device','usb-storage,drive=answers,removable=on']
    else:arguments+=['-boot','order=c,strict=on']
else:
    arguments+=['-drive','if=virtio,format=qcow2,file=images/gateway-empty.qcow2']
    if install:
        assert (root/'state/install-control/debian-initrd.gz').is_file()
        arguments+=['-cdrom','downloads/debian-13.7.0-amd64-netinst.iso','-kernel','state/install-control/debian-vmlinuz','-initrd','state/install-control/debian-initrd.gz','-append','auto=true priority=critical preseed/file=/preseed.cfg netcfg/enable=false locale=en_US.UTF-8 keyboard-configuration/xkb-keymap=us --- quiet','-boot','order=c,strict=on']
    else:arguments+=['-boot','order=c,strict=on']
assert arguments[arguments.index('-nic')+1]=='none'
assert not any(x in arguments for x in ['-netdev','-net','-virtfs','-fsdev','-vnc','-spice'])
if args.network!='none':
    if args.guest == 'work-boot':
        gateway = runtime.discover_guests(root).get('gateway')
        assert gateway and runtime.gateway_ready(root, gateway), 'Gateway connection must be freshly verified before Windows starts'
    else:
        accepted=json.loads((root/'state/gateway-network-accepted.json').read_text(encoding='utf-8'))
        assert accepted['native_tests_passed'] and accepted['wan_bypass_packets']==0
    assert args.network==('isolated-lan' if work else 'gateway')
    arguments+=network_arguments('work' if work else 'gateway',output.relative_to(root).as_posix(),lan_port=settings['lan_port'])
if args.work_test_media:
    media=root/'state/windows-network-test.img'
    prepared=json.loads((root/'logs/windows-network-test-media.json').read_text(encoding='utf-8'))
    assert sha(media)==prepared['sha256']
    arguments+=['-device','qemu-xhci,id=wctestusb','-drive','if=none,id=wctest,format=raw,file=state/windows-network-test.img',
                '-device','usb-storage,drive=wctest,bus=wctestusb.0,removable=on']
if args.gateway_stage:
    manifest=json.loads((root/'logs/gateway-delivery-prepared.json').read_text())
    delivery=root/'private/gohost-user1/gateway-delivery.tar'
    assert manifest['delivery_relative_path']=='private/gohost-user1/gateway-delivery.tar'
    assert sha(delivery)==manifest['archive_sha256']
    arguments[arguments.index('if=virtio,format=qcow2,file=images/gateway-empty.qcow2')]='if=none,id=wcroot,format=qcow2,file=images/gateway-empty.qcow2'
    arguments+=['-device','virtio-blk-pci,drive=wcroot,bootindex=1']
    arguments[arguments.index('-serial')+1]='file:'+str((output/'stage-serial.log').relative_to(root)).replace('\\','/')
    arguments+=['-drive','if=none,id=wcdelivery,format=raw,readonly=on,file=private/gohost-user1/gateway-delivery.tar',
                '-device','virtio-blk-pci,drive=wcdelivery,serial=WCDELIVERY']
elif not work:
    arguments[arguments.index('-serial')+1]='file:'+(output/'runtime-serial.log').relative_to(root).as_posix()
env=os.environ.copy();env['PATH']=str(engine.parent)+';'+os.environ['SystemRoot']+'\\System32;'+os.environ['SystemRoot']
work_reboot_session = args.guest == 'work-boot' and args.network == 'isolated-lan'
normal_runtime = work_reboot_session and not args.work_test_media
session_nonce=uuid.uuid4().hex if work_reboot_session else None
files_session = None
display_settings = None
if normal_runtime:
    display_settings = windows_settings.load_settings(root)
    medium = output / 'display-settings'
    medium.mkdir()
    windows_settings.write_settings(medium, display_settings, session_nonce)
    arguments += windows_settings.media_arguments(medium.relative_to(root).as_posix())
    arguments += ['-device', 'usb-tablet,id=wcabspointer,bus=wcsettingsusb.0']
    arguments += ['-global', 'VGA.xres=' + str(display_settings['width']),
                  '-global', 'VGA.yres=' + str(display_settings['height']),
                  '-chardev', 'ringbuf,id=wcdisplay,size=8192']
    arguments[arguments.index('-serial') + 1] = 'chardev:wcdisplay'
    # SDL's close button otherwise cuts guest power without an ACPI shutdown.
    viewer_settings = load_viewer_settings(root)
    arguments[arguments.index('-display') + 1] = ('sdl,gl=off,window-close=off'
        + (',full-screen=on' if viewer_settings['start_fullscreen'] else ''))
    setup = root / 'state/display-setup'
    if setup.exists():
        from profile_store import _plain
        _plain(setup)
        for name in ('SETUP.JSN', 'GUESTSET.EXE', 'RUNSETUP.CMD'):
            path = setup / name
            _plain(path)
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError('Invalid display setup medium')
        arguments += windows_settings.media_arguments('state/display-setup', 'WCPSETUP')
generation=1
previous_generations=[]
controller_identity=runtime.process_identity(os.getpid())
if work_reboot_session:
    arguments.append('-no-reboot')
stderr_writer = CappedWriter(output/'stderr.txt') if work_reboot_session else None
stderr = subprocess.PIPE if work_reboot_session else (output/'stderr.txt').open('w',encoding='utf-8')
if args.guest == 'work-boot' and args.network == 'isolated-lan':
    assert runtime.gateway_ready(root, gateway), 'Gateway stopped or changed before Windows startup'


def spawn_generation():
    global process, report, stderr_drain, inbox, qmp_events, qmp_reader, sequence
    if work_reboot_session:
        while True:
            with runtime.work_session_lock(output):
                if work_stop_requested(output,session_nonce,controller_identity):
                    return False
                if runtime.gateway_ready(root,gateway):
                    process=subprocess.Popen([str(engine),*arguments],cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
                    break
                if generation == 1:
                    raise RuntimeError('The original gateway is not verified for Windows startup')
            if not runtime.record_live(root,gateway) or not runtime.controller_live(root,gateway):
                raise RuntimeError('The original gateway controller was lost; Windows was not restarted')
            time.sleep(.5)
    else:
        process=subprocess.Popen([str(engine),*arguments],cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
    stderr_drain=start_capped_drain(process.stderr.buffer,stderr_writer) if work_reboot_session else None
    report={'normal_runtime':normal_runtime,'schema':1,'process':runtime.process_identity(process.pid),
        'controller':controller_identity,'started_utc':datetime.now(timezone.utc).isoformat(),
        'guest':args.guest,'pid':process.pid,'arguments':arguments,'network':args.network,
        'host_network_changed':False,'credentials_created':False,'captures':[],'status':'starting'}
    if work_reboot_session:
        report.update(session_nonce=session_nonce,generation=generation,previous_generations=list(previous_generations))
    if display_settings is not None:
        report['windows_display'] = {'status': 'pending', 'requested': display_settings}
    if files_session is not None:
        report['host_files'] = dict(files_session.public, generation=generation)
    report['writers']=[report['process'],report['controller']]
    inbox=queue.Queue();qmp_events=deque(maxlen=64);sequence=0
    save()
    qmp_reader=threading.Thread(target=read_qmp,daemon=True)
    qmp_reader.start()
    return True

def save():
    report['observed_utc']=datetime.now(timezone.utc).isoformat()
    runtime.atomic_json(output/'state.json', report)
    print(json.dumps({'state':str(output/'state.json'),'pid':process.pid,'status':report['status']}),flush=True)
controls=queue.Queue()
def read_qmp():
    for line in process.stdout:
        try:
            message=json.loads(line)
            if 'event' in message:
                allowed={'reason','device','node-name','operation','action','nospace','error','guest'}
                event={'observed_utc':datetime.now(timezone.utc).isoformat(),'event':message['event'],'data':{k:v for k,v in message.get('data',{}).items() if k in allowed}}
                if work_reboot_session:
                    event['generation']=report['generation']
                event['data']={key: value[:512] if isinstance(value,str) else value for key,value in event['data'].items()}
                qmp_events.append(event)
                runtime.atomic_json(output/'qmp-events.json', list(qmp_events))
            else:
                inbox.put(message)
        except ValueError:pass
    inbox.put({'eof':True})
def read_controls():
    for line in sys.stdin:
        try:controls.put(json.loads(line))
        except ValueError:print('{"error":"Invalid control JSON"}',flush=True)
threading.Thread(target=read_controls,daemon=True).start()
def command(name,parameters=None):
    global sequence
    sequence+=1;payload={'execute':name,'id':sequence}
    if parameters:payload['arguments']=parameters
    process.stdin.write(json.dumps(payload)+'\n');process.stdin.flush()
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        msg=inbox.get(timeout=max(.01,deadline-time.monotonic()))
        if msg.get('id')==sequence:
            if 'error' in msg:raise RuntimeError('QMP '+name+': '+json.dumps(msg['error']))
            return msg.get('return')
        if msg.get('eof'):raise RuntimeError('QEMU exited before QMP response')
    raise TimeoutError(name)
def capture(label):
    path=output/(label+'.png')
    # QEMU's monitor file writer needs an ASCII relative path on this Windows build.
    try:command('screendump',{'filename':path.relative_to(root).as_posix(),'format':'png'})
    except RuntimeError as error:
        report.setdefault('capture_errors',[]).append(str(error));save();return
    report['captures'].append({'path':str(path),'elapsed':round(time.monotonic()-begin,2),'sha256':sha(path)})
    save()
    print(json.dumps({'capture':str(path)}),flush=True)


def read_display_receipt():
    global display_buffer
    try:
        data = command('ringbuf-read', {'device': 'wcdisplay', 'size': 4096, 'format': 'utf8'})
        if report.get('status') == 'control-unavailable-guest-running':
            current = command('query-status')
            if not isinstance(current, dict) or current.get('status') not in ('running', 'paused'):
                raise ValueError('Guest status is not confirmed')
            report['status'] = current['status']
            save()
        if not isinstance(data, str):
            raise ValueError('Invalid display channel')
        display_buffer += data
        if len(display_buffer) > 8192:
            raise ValueError('Display response is too large')
        while '\n' in display_buffer:
            line, display_buffer = display_buffer.split('\n', 1)
            receipt = windows_settings.validate_receipt(line.strip(), display_settings, session_nonce)
            report['windows_display'] = {'status': 'applied' if receipt['success'] else 'failed',
                                         'receipt': receipt, 'generation': generation}
            save()
    except (OSError, ValueError, RuntimeError, TimeoutError, queue.Empty) as error:
        if isinstance(error, (OSError, TimeoutError, queue.Empty)):
            report['status'] = 'control-unavailable-guest-running'
        display_buffer = ''
        report['windows_display'] = {'status': 'unconfirmed', 'requested': display_settings}
        save()


def protect_work_window():
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetSystemMenu.argtypes = [wintypes.HWND, wintypes.BOOL]
    user.GetSystemMenu.restype = wintypes.HMENU
    user.EnableMenuItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
    user.DrawMenuBar.argtypes = [wintypes.HWND]
    found = []
    def visit(window, unused):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value == process.pid:
            menu = user.GetSystemMenu(window, False)
            if menu:
                user.EnableMenuItem(menu, 0xF060, 1)  # Gray SC_CLOSE; Stop uses the controller's ACPI action.
                user.DrawMenuBar(window)
                found.append(window)
        return True
    user.EnumWindows(callback_type(visit), 0)
    return bool(found)


def read_file_receipt():
    global files_buffer
    state = report['host_files']
    if not files_session.healthy():
        if state['status'] != 'failed':
            state.update(status='failed', code='FILE_SERVER_STOPPED')
            save()
        return
    if files_session.medium is None:
        return
    try:
        data = command('ringbuf-read', {'device': 'wcfilesreceipt', 'size': 16384, 'format': 'utf8'})
        if not isinstance(data, str) or len(files_buffer) + len(data) > 32768:
            raise ValueError('Invalid file-sharing channel')
        files_buffer += data
        while '\n' in files_buffer:
            line, files_buffer = files_buffer.split('\n', 1)
            receipt = file_sharing.validate_receipt(line.strip(), files_session.config)
            state.update(status=('ready' if state['share_count'] else files_session.public['status'])
                         if receipt['success'] else 'failed', receipt=receipt)
            save()
    except (OSError, ValueError, RuntimeError, TimeoutError, queue.Empty):
        files_buffer = ''
        if state['status'] != 'unconfirmed':
            state['status'] = 'unconfirmed'
            save()


def handle_control(control):
    action=None
    try:
        if not isinstance(control,dict):raise ValueError('Control message must be an object')
        action=control.get('action')
        if action=='capture':capture('manual-'+str(len(report['captures'])))
        elif action=='status':report['qmp_status']=command('query-status');save()
        elif action=='diagnose':
            report['qmp_status']=command('query-status');report['current_blocks']=command('query-block');report['block_statistics']=command('query-blockstats');save()
        elif action=='input-status':
            report['input_devices']=command('query-mice');report['pci_devices']=command('query-pci');save()
        elif action=='gateway-nics':
            assert args.gateway_stage and not work and report['network']=='none'
            assert 'WC_FIREWALL_READY' in (output/'stage-serial.log').read_text(encoding='utf-8',errors='replace')
            try:
                command('netdev_add',{'type':'stream','id':'wclan','server':True,'addr':{'type':'inet','host':'127.0.0.1','port':str(settings['lan_port']),'numeric':True,'ipv4':True,'ipv6':False}})
                command('netdev_add',{'type':'user','id':'wcwan','ipv6':False,'net':'10.0.2.0/24','host':'10.0.2.2'})
                command('device_add',{'driver':'virtio-net-pci','id':'wcnicwan','netdev':'wcwan','mac':'52:54:00:77:00:01'})
                command('device_add',{'driver':'virtio-net-pci','id':'wcniclan','netdev':'wclan','mac':'52:54:00:77:00:02'})
                report['network']='gateway-LAN-loopback-stream-and-user-NAT';report['network_filters']=command('query-rx-filter')
            except RuntimeError as error:
                report['network']='partial-gateway-hotplug';report['network_error']=str(error)
            save()
        elif action in ('gateway-wan-off','gateway-wan-on'):
            assert not work and report['network']!='none'
            command('set_link',{'name':'wcnicwan','up':action=='gateway-wan-on'});save()
        elif action=='powerdown':
            if work_reboot_session:
                with runtime.work_session_lock(output):
                    runtime.atomic_json(output/'stop-session.json',{'session_nonce':session_nonce,'controller':controller_identity})
                report['stop_requested']=True
            if process.poll() is None:
                command('system_powerdown')
            report['acpi_powerdown_requested_utc']=datetime.now(timezone.utc).isoformat();save()
        elif action=='keys':
            keys=control['keys'];assert all(isinstance(k,str) and len(k)<20 for k in keys)
            command('send-key',{'keys':[{'type':'qcode','data':k} for k in keys],'hold-time':100})
        elif action=='sequence':
            for step in control['steps']:
                command('send-key',{'keys':[{'type':'qcode','data':k} for k in step['keys']],'hold-time':80})
                time.sleep(min(2,max(.12,step.get('delay',.15))))
        elif action=='pause':command('stop');report['status']='paused';save()
        elif action=='resume':command('cont');report['status']='running';save()
        else:raise ValueError('Unsupported controller action')
    except Exception as error:
        if isinstance(error, (OSError, TimeoutError, queue.Empty)):
            report['status'] = 'control-unavailable-guest-running'
        report.setdefault('control_errors',[]).append({'action':str(action)[:64],'error':(type(error).__name__+': '+str(error))[:512]})
        report['control_errors']=report['control_errors'][-64:]
        save()

try:
    if normal_runtime:
        files_session = file_sharing.Session(root, session_nonce)
        arguments += files_session.arguments
    while True:
        if not spawn_generation():
            report.update(status='exited',stop_requested=True)
            save()
            break
        begin=time.monotonic()
        try:
            greeting=inbox.get(timeout=15);assert 'QMP' in greeting,'Missing QMP greeting'
            command('qmp_capabilities')
            report['qemu_version']=greeting['QMP']['version']
            report['input_devices']=command('query-mice')
            report['blocks']=command('query-block');report['network_filters']=command('query-rx-filter')
            if args.network=='none':assert report['network_filters']==[],'Unexpected NIC'
            command('cont');report['status']='running';save()
            # Capture only on explicit request, so a user's local password entry is never polled.
            targets=iter([]);target=next(targets,None)
            display_buffer = ''
            files_buffer = ''
            display_poll = time.monotonic() + 5
            protected_window = False
            while process.poll() is None:
                if normal_runtime and not protected_window:
                    protected_window = protect_work_window()
                if display_settings is not None and time.monotonic() >= display_poll:
                    read_display_receipt()
                    if time.monotonic() - begin >= 120 and report['windows_display']['status'] == 'pending':
                        report['windows_display'] = {'status': 'unconfirmed', 'requested': display_settings}
                        save()
                    display_poll = time.monotonic() + 3
                    if files_session is not None:
                        read_file_receipt()
                if target is not None and time.monotonic()-begin>=target:
                    capture('boot-'+str(target))
                    if work and install and target==2:
                        command('send-key',{'keys':[{'type':'qcode','data':'spc'}],'hold-time':100})
                        report['dvd_boot_key_sent']=True;save()
                    target=next(targets,None)
                for control_file in output.glob('control-*.json'):
                    try:controls.put(json.loads(control_file.read_text(encoding='utf-8')));control_file.unlink()
                    except (ValueError,OSError):pass
                try:control=controls.get(timeout=.2)
                except queue.Empty:continue
                handle_control(control)
            process.wait(timeout=15)
        except Exception as error:
            report['error']=type(error).__name__+': '+str(error)
        finally:
            if process.poll() is None:
                report['status']='control-unavailable-guest-running'
                try:save()
                except OSError:pass
                # Keep stdio and the process alive until the guest shuts down itself.
                # Controller errors must never turn into a hard power cut of its disk.
                process.wait()
            qmp_reader.join(timeout=2)
            if qmp_reader.is_alive():
                report['error']='Previous QMP reader has not settled; Windows retained'
            if stderr_drain:
                stderr_drain.close()
            if not qmp_reader.is_alive():
                for pipe in (process.stdin, process.stdout):
                    try:
                        pipe.close()
                    except OSError:
                        pass  # The exited guest's pipe must not prevent its terminal state from being saved.
            reset=work_reset_proof(root,report,list(qmp_events),process.returncode) if work_reboot_session and not report.get('error') else None
            if (work_reboot_session and not reset and not report.get('error') and not any(
                    event.get('event') == 'SHUTDOWN' and event.get('data',{}).get('guest') is True
                    and event.get('data',{}).get('reason') == 'guest-shutdown'
                    and event.get('generation') == generation for event in qmp_events)):
                report['error']='Windows exit reason was not confirmed; gateway retained'
            report['status']='restarting' if reset else 'exited'
            report['exit_code']=process.returncode
            if reset:
                report['reset_proof']=reset
            save()
        if not reset:
            break
        # The old QEMU exited itself; the lifecycle lock serializes Stop with the next spawn.
        time.sleep(.5)
        if work_stop_requested(output,session_nonce,controller_identity):
            report.update(status='exited',stop_requested=True)
            save()
            break
        if generation >= 64:
            report.update(status='exited',error='Windows reboot safety limit reached; gateway retained')
            save()
            break
        previous_generations.append(reset)
        generation+=1
except Exception:
    if (work_reboot_session and 'report' in globals() and 'reset_proof' in report
            and process.pid == report['pid'] and process.poll() is not None):
        report.update(status='exited',error='Windows restart failed; gateway retained')
        save()
    raise
finally:
    if files_session is not None:
        files_session.close(guest_stopped='process' not in globals() or process.poll() is not None)
    if stderr_writer:
        stderr_writer.close()
    elif stderr != subprocess.PIPE:
        stderr.close()
if report.get('error') or process.returncode:raise SystemExit(1)
