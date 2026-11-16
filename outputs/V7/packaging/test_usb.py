"""Hardware-free installer tests: toy files, mocked UAC/helper, no model import."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
import ctypes
import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import usb_support as support
import launcher
import customer_admin


class PackageTests(unittest.TestCase):
    def setUp(self):
        base=HERE/'test-work';base.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base,prefix='fixture-')
        self.root=Path(self.tmp.name);self.source=self.root/'USB 中文 空格';self.source.mkdir()
        self.product=self.root/'customer-cache'
        self.manifest=dict(schema=1,release_id='V7-USB-20261107-tests',files={},critical_files=[],driver_component_sha256='1'*64)
        for name,data in [('payload/runtime/tiny.txt',b'runtime'),('payload/app/tiny.txt',b'app')]:
            p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
            self.manifest['files'][name]=dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
            self.manifest['critical_files'].append(name)
        support.atomic_json(self.source/'MANIFEST.json',self.manifest)
    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to((HERE/'test-work').resolve()))
        self.tmp.cleanup()
    def install(self,**kw):
        m,h=support.load_manifest(self.source)
        return support.install_payload(self.source,self.product,m,h,**kw)
    def test_whole_payload_copies_and_renames(self):
        final=self.install()
        self.assertEqual((final/'app/tiny.txt').read_bytes(),b'app')
        self.assertTrue((final/'INSTALL-COMPLETE.json').is_file())
        self.assertFalse(list((self.product/'versions').glob('.pending-*')))
    def test_repeated_start_reuses_verified_critical_files(self):
        first=self.install()
        self.assertEqual(self.install(),first)
    def test_corrupt_usb_refuses_complete_marker(self):
        (self.source/'payload/app/tiny.txt').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'U盘'):
            self.install()
        self.assertFalse((self.product/'versions'/self.manifest['release_id']).exists())
    def test_corrupt_local_critical_file_refuses_launch(self):
        final=self.install();(final/'app/tiny.txt').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError,'critical'):
            self.install()
    def test_changed_manifest_does_not_overwrite_installed_release(self):
        self.install();self.manifest['new_metadata']=True
        support.atomic_json(self.source/'MANIFEST.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'does not match'):
            self.install()
    def test_cancellation_can_resume(self):
        calls=[0]
        def cancel():
            calls[0]+=1;return calls[0]>=2
        with self.assertRaises(InterruptedError):self.install(cancelled=cancel)
        final=self.install()
        self.assertEqual((final/'runtime/tiny.txt').read_bytes(),b'runtime')
    def test_not_enough_space_is_clear(self):
        with patch.object(support.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(OSError,'空间不足'):self.install()
    def test_no_install_inside_usb(self):
        self.product=self.source/'local'
        with self.assertRaises(ValueError):self.install()
    def test_reject_junction_product_without_native_access(self):
        original=Path.is_junction
        with patch.object(Path,'is_junction',lambda p: p==self.product or original(p)):
            with self.assertRaisesRegex(ValueError,'Linked'):self.install()
    def test_traversal_and_absolute_paths_rejected(self):
        for text in ('../evil','C:/evil','/evil','payload/../evil','payload\\evil','payload//evil','payload/./evil'):
            with self.subTest(text=text),self.assertRaises(ValueError):support.safe_relative(text)
    def test_case_colliding_manifest_rejected(self):
        self.manifest['files']['PAYLOAD/app/tiny.txt']=self.manifest['files']['payload/app/tiny.txt']
        support.atomic_json(self.source/'MANIFEST.json',self.manifest)
        with self.assertRaisesRegex(ValueError,'Case'):support.load_manifest(self.source)
    def test_duplicate_json_keys_rejected(self):
        (self.source/'MANIFEST.json').write_text('{"schema":1,"schema":2}','utf-8')
        with self.assertRaisesRegex(ValueError,'Duplicate'):support.load_manifest(self.source)
    def test_consent_not_inherited_by_other_customer(self):
        h='1'*64
        support.atomic_json(self.product/'consents'/(h+'.json'),dict(accepted=True,consent_version=1,driver_component_sha256=h))
        self.assertTrue(support.accepted_consent(self.product,h))
        self.assertFalse(support.accepted_consent(self.root/'other-computer',h))
        self.assertFalse(support.accepted_consent(self.product,'2'*64))


class Function:
    def __init__(self,fn):self.fn=fn
    def __call__(self,*args):return self.fn(*args)


class AdminTests(unittest.TestCase):
    def setUp(self):
        base=HERE/'test-work';base.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=base,prefix='admin-');self.root=Path(self.tmp.name)
        c=self.root/'app/hid/app/HIDMaestro.Core.dll';c.parent.mkdir(parents=True);c.write_bytes(b'fake component')
        s=self.root/'app/.settings';s.mkdir(parents=True);support.atomic_json(s/'hid-launch.json',dict(connected=True))
        self.request=self.root/'request.json';self.nonce='a'*32
        self.data=dict(nonce=self.nonce,install_root=str(self.root),mode='auto',explicit_install_consent=True,
                       driver_component_sha256=support.digest(c))
        support.atomic_json(self.request,self.data)
        self.kernel=SimpleNamespace(CreateMutexW=Function(lambda *a:7),CloseHandle=Function(lambda *a:1))
        self.child=Mock(returncode=0);self.child.pid=123;self.child.poll.return_value=None;self.child.wait.return_value=0
    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to((HERE/'test-work').resolve()));self.tmp.cleanup()
    def run_admin(self,admin=True,already=False,check=0):
        ctypes.set_last_error(183 if already else 0)
        self.driver_check=Mock(return_value=check)
        with patch.object(customer_admin,'ROOT',self.root),patch.object(sys,'argv',['customer_admin','--request',str(self.request),'--nonce',self.nonce]), \
             patch.object(customer_admin.ctypes,'windll',SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda:admin))), \
             patch.object(customer_admin.ctypes,'WinDLL',lambda *a,**kw:self.kernel), \
             patch.dict(sys.modules,{'launch_windows_admin':SimpleNamespace(main=self.driver_check)}), \
             patch.object(customer_admin.subprocess,'Popen',return_value=self.child) as spawn,patch.object(customer_admin.time,'sleep'):
            code=customer_admin.main();self.spawn_calls=spawn.call_args_list
        self.state=json.loads(self.request.with_suffix('.state.json').read_text('utf-8'));return code
    def test_no_admin_no_driver_install(self):
        self.assertEqual(self.run_admin(admin=False),1);self.driver_check.assert_not_called();self.assertFalse(self.spawn_calls)
    def test_explicit_consent_required(self):
        self.data['explicit_install_consent']=False;support.atomic_json(self.request,self.data)
        self.assertEqual(self.run_admin(),1);self.driver_check.assert_not_called()
    def test_component_fingerprint_required(self):
        self.data['driver_component_sha256']='0'*64;support.atomic_json(self.request,self.data)
        self.assertEqual(self.run_admin(),1);self.driver_check.assert_not_called()
    def test_singleton_prevents_duplicate_controller(self):
        self.assertEqual(self.run_admin(already=True),0);self.driver_check.assert_not_called()
        self.assertEqual(self.state['state'],'already_running')
    def test_input_failure_never_launches_recognition(self):
        self.assertEqual(self.run_admin(check=1),1);self.assertFalse(self.spawn_calls)
        self.assertFalse(self.state['game_input_verified'])
    def test_success_uses_isolated_app_entry_and_no_auto_recognition(self):
        self.assertEqual(self.run_admin(),0);self.assertEqual(len(self.spawn_calls),1)
        command=self.spawn_calls[0].args[0]
        self.assertIn(str(self.root/'app_entry.py'),command)
        self.assertNotIn('--smoke',command)
        self.assertFalse(self.state['automatic_recognition']);self.assertFalse(self.state['automatic_follow'])
        self.assertFalse(self.state['game_input_verified']);self.child.wait.assert_called_once()
    def test_cancel_before_window_launch(self):
        self.data['cancelled']=True;support.atomic_json(self.request,self.data)
        self.assertEqual(self.run_admin(),1);self.assertFalse(self.spawn_calls)


class ElevationTests(unittest.TestCase):
    def test_com_cleanup_after_cancel(self):
        ole=SimpleNamespace(CoInitializeEx=Function(lambda *a:0),CoUninitialize=Mock())
        with patch.object(launcher.ctypes,'WinDLL',return_value=ole),patch.object(launcher,'_elevate_process',side_effect=OSError('cancel')):
            with self.assertRaises(OSError):launcher.elevate(Path('fixture.exe'),[],Path('.'))
        ole.CoUninitialize.assert_called_once()
    def test_com_failure_does_not_request_uac(self):
        ole=SimpleNamespace(CoInitializeEx=Function(lambda *a:-1),CoUninitialize=Mock())
        with patch.object(launcher.ctypes,'WinDLL',return_value=ole),patch.object(launcher,'_elevate_process') as execute:
            with self.assertRaises(OSError):launcher.elevate(Path('fixture.exe'),[],Path('.'))
            execute.assert_not_called()
        ole.CoUninitialize.assert_not_called()
    def test_duplicate_setup_mutex_is_not_owned(self):
        def create(*args):
            ctypes.set_last_error(183);return 7
        close=Mock();kernel=SimpleNamespace(CreateMutexW=Function(create),CloseHandle=Function(close))
        with patch.object(launcher.ctypes,'WinDLL',return_value=kernel):
            _,handle=launcher.setup_mutex()
        self.assertIsNone(handle);close.assert_called_once_with(7)
    def test_uac_cancel_is_an_error_not_success(self):
        def cancelled(*args):
            ctypes.set_last_error(1223);return 0
        shell=SimpleNamespace(ShellExecuteExW=Function(cancelled))
        kernel=SimpleNamespace(WaitForSingleObject=Function(lambda *a:0),GetExitCodeProcess=Function(lambda *a:0),CloseHandle=Function(lambda *a:1))
        with patch.object(launcher.ctypes,'WinDLL',lambda name,**kw:shell if name=='shell32' else kernel):
            with self.assertRaisesRegex(OSError,'未批准'):
                launcher._elevate_process(Path('fixture.exe'),[],Path('.'))


if __name__=='__main__':
    native_attempts=[]
    def audit(event,args):
        if event in ('ctypes.dlopen','subprocess.Popen','os.system','socket.connect'):
            native_attempts.append(event);raise RuntimeError('Packaging tests forbid native/process/network execution')
    sys.addaudithook(audit)
    result=unittest.main(exit=False,verbosity=2).result
    print(json.dumps(dict(passed=result.wasSuccessful() and not native_attempts,tests=result.testsRun,native_attempts=native_attempts)))
    raise SystemExit(0 if result.wasSuccessful() and not native_attempts else 1)
