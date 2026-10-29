"""Bounded text codec, fake native clipboard and own loopback; no actual clipboard/VM."""
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import string
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib

SOURCE=Path(__file__).resolve().parents[1]
SERVICE=Path(os.environ['TEMP'])
sys.path.insert(0,str(SOURCE/'scripts'))
import clipboard_bridge as bridge
NONCE='a'*32


@unittest.skipUnless(os.name=='nt','Windows .NET native helper build')
class LargeTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build=Path(tempfile.mkdtemp(prefix='clipboard-large-proof-',dir=SERVICE));cls.metrics=[]
        names=['guest/GuestClipboard.cs','guest/ClipboardTransport.cs','guest/ClipboardFiles.cs','guest/ClipboardStage.cs',
               'guest/ClipboardProtocol.cs','guest/GuestEnvironment.cs','guest/files/GuestFiles.cs','tests/ClipboardLargeTextRegression.cs']
        paths=[]
        for index,name in enumerate(names):
            target=cls.build/(str(index)+'-'+Path(name).name);target.write_bytes((SOURCE/name).read_bytes());paths.append(str(target))
        cls.exe=cls.build/'ClipboardLargeTextRegression.exe'
        compiler=Path(os.environ['WINDIR'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
        result=subprocess.run([str(compiler),'/nologo','/platform:x64','/main:ClipboardLargeTextRegression','/r:System.Web.Extensions.dll',
                               '/r:System.Windows.Forms.dll','/r:System.Drawing.dll','/r:Microsoft.CSharp.dll','/out:'+str(cls.exe),*paths],capture_output=True)
        (cls.build/'compile.stdout').write_bytes(result.stdout);(cls.build/'compile.stderr').write_bytes(result.stderr)
        if result.returncode:raise AssertionError(result.stdout.decode(errors='replace')+result.stderr.decode(errors='replace'))
        rng=random.Random(40)
        cls.samples={'ascii':''.join(rng.choices(string.ascii_letters+string.digits+string.punctuation,k=2*1024*1024)),
                     'unicode':''.join(rng.choices([chr(0x400+i) for i in range(2048)]+['😀','🐱']*100,k=1024*1024))}

    @classmethod
    def tearDownClass(cls):
        (cls.build/'metrics.json').write_text(json.dumps(cls.metrics,indent=2),encoding='utf-8')
        print('Text metrics:',cls.build/'metrics.json')

    def test_codec_cross_language_measured_and_strict(self):
        for kind,text in self.samples.items():
            with self.subTest(kind=kind):
                start=time.perf_counter();line=bridge.encode(NONCE,text);python_encode=time.perf_counter()-start
                self.assertIsNotNone(line);self.assertGreater(len(line),1024*1024)
                message=json.loads(line);raw=base64.b64decode(message['d'])+b'private-trailing';message['d']=base64.b64encode(raw).decode();invalid=json.dumps(message)
                config={'text':base64.b64encode(text.encode()).decode(),'line':line.decode().strip(),'invalid':invalid}
                result=subprocess.run([str(self.exe),'codec'],input=json.dumps(config).encode(),capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
                value=json.loads(result.stdout);self.assertEqual(value['decoded'],hashlib.sha256(text.encode()).hexdigest());self.assertTrue(value['invalid'])
                start=time.perf_counter();self.assertEqual(bridge.decode(value['line'].encode(),NONCE),text);python_decode=time.perf_counter()-start
                self.assertEqual(value['units'],len(text.encode('utf-16-le'))//2)
                self.metrics.append(dict(kind=kind,utf8Bytes=len(text.encode()),utf16Units=value['units'],wireBytes=len(line),
                    pythonEncodeSeconds=python_encode,pythonDecodeSeconds=python_decode,csharpEncodeMs=value['encodeMs'],csharpDecodeMs=value['decodeMs'],workingSet=value['workingSet']))

    def test_caps_oversize_public_code_and_no_payload_leak(self):
        result=subprocess.run([str(self.exe),'limits'],capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'));self.assertTrue(json.loads(result.stdout)['ok'])
        fake=mock.Mock();fake.last_error=None;fake.recovery=[]
        source=bridge.Bridge(0,NONCE,fake)
        for text in ('x'*(bridge.MAX_CHARS+1),'😀'*(bridge.MAX_CHARS//2+1)):
            self.assertIsNone(bridge.encode(NONCE,text));source.local_change(text)
            self.assertEqual(source.public()['code'],'CLIPBOARD_TEXT_TOO_LARGE')
            frame=json.loads(source.pending);self.assertEqual(frame['t'],'skip');self.assertNotIn('d',frame)
            source.receive(bridge.packet(NONCE,'ack',s=frame['s'],o=frame['o'],ok=True,applied=False))
            self.assertEqual(source.public()['code'],'CLIPBOARD_TEXT_TOO_LARGE')
        source.local_change('next');self.assertIsNone(source.transfer_error)
        source.local_change('x'*(bridge.MAX_CHARS+1));source.receive(bridge.encode(NONCE,'remote new copy',source.clock+1,'guest'))
        self.assertIsNone(source.transfer_error)
        maximum='x'*bridge.MAX_CHARS;self.assertEqual(bridge.decode(bridge.encode(NONCE,maximum),NONCE),maximum)
        packer=zlib.compressobj(wbits=-15);bomb=packer.compress(b'x'*(bridge.MAX_CHARS+1))+packer.flush()
        self.assertIsNone(bridge.decode(bridge.packet(NONCE,'text',s=1,o='guest',z=1,d=base64.b64encode(bomb).decode()),NONCE))

    def test_host_native_oversize_code_is_sticky_without_native_read_or_new_copy(self):
        window=bridge.WindowsClipboard.__new__(bridge.WindowsClipboard);window.hwnd=1;window.observed=10;window.exclude=999
        window.recovery=[];window.reset_pending=False;window.close_requested=window.writing=False;window.last_error=None;window.on_change=mock.Mock()
        user,kernel=mock.Mock(),mock.Mock();user.OpenClipboard.return_value=True;user.GetClipboardSequenceNumber.return_value=11
        user.IsClipboardFormatAvailable.side_effect=lambda value:value==bridge.CF_UNICODETEXT;user.GetClipboardData.return_value=1
        kernel.GlobalSize.return_value=(bridge.MAX_CHARS+2)*2
        with mock.patch.multiple(bridge,user32=user,kernel32=kernel):
            window.poll();self.assertEqual(window.last_error,'CLIPBOARD_TEXT_TOO_LARGE');kernel.GlobalLock.assert_not_called()
            window.on_change.assert_called_once_with(bridge.PRIVATE_COPY);window.poll();self.assertEqual(window.last_error,'CLIPBOARD_TEXT_TOO_LARGE')
            user.GetClipboardSequenceNumber.return_value=12;storage=ctypes.create_string_buffer('new\0'.encode('utf-16-le'))
            kernel.GlobalSize.return_value=8;kernel.GlobalLock.return_value=ctypes.addressof(storage);window.poll();self.assertIsNone(window.last_error)

    def test_transport_two_way_actual_large_text_frames(self):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(5)
        child=subprocess.Popen([str(self.exe),'transport',str(listener.getsockname()[1])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        peer=stream=None
        try:
            peer,_=listener.accept();peer.settimeout(8);stream=peer.makefile('rb')
            def read_frame(limit):
                while True:
                    line=stream.readline(limit)
                    self.assertTrue(line.endswith(b'\n'), 'Expected a complete transport frame')
                    if line==b'\n':continue
                    value=json.loads(line)
                    if value==dict(v=2,n=NONCE,t='ping',o='guest'):
                        peer.sendall(bridge.packet(NONCE,'pong',o='host'));continue
                    return line
            self.assertEqual(json.loads(read_frame(4097)),dict(v=2,n=NONCE,t='hello',o='guest',ready=True))
            peer.sendall(bridge.packet(NONCE,'hello',o='host',ready=True))
            for kind,text in self.samples.items():
                line=bridge.encode(NONCE,text);start=time.perf_counter();peer.sendall(line);send=time.perf_counter()-start
                echoed=read_frame(bridge.MAX_LINE+2);elapsed=time.perf_counter()-start
                self.assertEqual(echoed,line);self.assertEqual(bridge.decode(echoed,NONCE),text)
                self.metrics.append(dict(kind=kind,wireBytes=len(line),loopbackSendSeconds=send,loopbackRoundTripSeconds=elapsed))
                self.assertLess(elapsed,5,'Measured loopback transfer exceeded write budget')
            child.stdin.write(b'stop\n');child.stdin.flush();stdout,stderr=child.communicate(timeout=8)
            self.assertEqual(child.returncode,0,stderr.decode(errors='replace'));events=[json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(sum(event.get('kind')=='received' for event in events),2)
            self.assertEqual(next(event['peakReaders'] for event in events if event.get('kind')=='stopped'),1)
        finally:
            if child.poll() is None:child.kill();child.wait()
            for pipe in (child.stdin,child.stdout,child.stderr):
                if pipe:pipe.close()
            if stream:stream.close()
            if peer:peer.close()
            listener.close()


if __name__=='__main__':unittest.main()
