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
sys.path.insert(0,str(Path(__file__).resolve().parent))
from runtime_network_plan import arguments as network_arguments
from runtime_config import load_settings

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

parser=argparse.ArgumentParser()
parser.add_argument('--project',required=True)
parser.add_argument('--guest',choices=['work-install','work-resume','work-boot','gateway-install','gateway-boot'],required=True)
parser.add_argument('--gateway-stage',action='store_true',help='Attach the verified read-only gateway delivery; initially no NIC')
parser.add_argument('--network',choices=['none','isolated-lan','gateway'],default='none')
parser.add_argument('--work-test-media',action='store_true')
args=parser.parse_args()
root=Path(args.project).resolve(strict=True)
settings=load_settings(root)
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
stderr=(output/'stderr.txt').open('w',encoding='utf-8')
process=subprocess.Popen([str(engine),*arguments],cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
report={'started_utc':datetime.now(timezone.utc).isoformat(),'guest':args.guest,'pid':process.pid,'arguments':arguments,'network':args.network,'host_network_changed':False,'credentials_created':False,'captures':[],'status':'starting'}
def save():
    report['observed_utc']=datetime.now(timezone.utc).isoformat()
    (output/'state.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'state':str(output/'state.json'),'pid':process.pid,'status':report['status']}),flush=True)
inbox=queue.Queue();controls=queue.Queue()
def read_qmp():
    for line in process.stdout:
        try:
            message=json.loads(line)
            if 'event' in message:
                allowed={'reason','device','node-name','operation','action','nospace','error','guest'}
                event={'observed_utc':datetime.now(timezone.utc).isoformat(),'event':message['event'],'data':{k:v for k,v in message.get('data',{}).items() if k in allowed}}
                with (output/'qmp-events.jsonl').open('a',encoding='utf-8') as events:events.write(json.dumps(event)+'\n')
            inbox.put(message)
        except ValueError:pass
    inbox.put({'eof':True})
def read_controls():
    for line in sys.stdin:
        try:controls.put(json.loads(line))
        except ValueError:print('{"error":"Invalid control JSON"}',flush=True)
qmp_reader=threading.Thread(target=read_qmp,daemon=True)
qmp_reader.start();threading.Thread(target=read_controls,daemon=True).start()
sequence=0
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
def handle_control(control):
    try:
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
                command('object-add',{'qom-type':'filter-dump','id':'wc-wan-audit','netdev':'wcwan','file':str((output/'wan.pcap').relative_to(root)).replace('\\','/')})
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
            command('system_powerdown');report['acpi_powerdown_requested_utc']=datetime.now(timezone.utc).isoformat();save()
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
        report.setdefault('control_errors',[]).append({'action':control.get('action'),'error':type(error).__name__+': '+str(error)})
        save()

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
    while process.poll() is None:
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
    report['status']='exited';report['exit_code']=process.returncode;save();stderr.close()
if report.get('error') or process.returncode:raise SystemExit(1)
