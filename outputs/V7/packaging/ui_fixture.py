"""Construct only the customer launcher UI, with toy files and no activation."""
from pathlib import Path
from unittest.mock import patch
import ctypes
import hashlib
import json
import os
import sys
import tempfile

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import launcher
from usb_support import atomic_json


def main():
    attempts=[]
    def deny(*args,**kwargs):
        attempts.append(str(args[0]) if args else 'activation')
        raise RuntimeError('Offline launcher fixture forbids activation')
    def audit(event,args):
        if event in ('ctypes.dlopen','subprocess.Popen','socket.connect','os.system'):
            deny(event)
    sys.addaudithook(audit)
    ctypes.WinDLL=deny
    base=HERE/'test-work';base.mkdir(exist_ok=True)
    checks=[]
    temp=tempfile.TemporaryDirectory(dir=base,prefix='launcher-ui-')
    root=Path(temp.name);usb=root/'USB';usb.mkdir()
    (usb/'toy.txt').write_text('toy','utf-8')
    atomic_json(usb/'MANIFEST.json',dict(schema=1,release_id='V7-USB-20261107-fixture',
        files={'toy.txt':dict(bytes=3,sha256=hashlib.sha256(b'toy').hexdigest())},critical_files=[],driver_component_sha256='1'*64))
    os.environ['TCL_LIBRARY']=str(Path(sys.base_prefix)/'tcl/tcl8.6')
    os.environ['TK_LIBRARY']=str(Path(sys.base_prefix)/'tcl/tk8.6')
    with patch.object(launcher,'USB',usb),patch.dict(os.environ,LOCALAPPDATA=str(root/'customer')),patch.object(launcher,'elevate',deny):
        app=launcher.Launcher()
        app.root.update();app.root.update_idletasks()
        checks.append(dict(name='fresh_computer_consent_unchecked',passed=not app.consent.get() and not app.had_consent))
        for name,widget in (('start',app.start),('vc_repair',app.repair),('mode',app.mode_box)):
            visible=widget.winfo_ismapped() and widget.winfo_rooty()+widget.winfo_height() <= app.root.winfo_rooty()+app.root.winfo_height()+2
            checks.append(dict(name=name+'_within_window',passed=bool(visible),mapped=widget.winfo_ismapped(),top=widget.winfo_rooty(),height=widget.winfo_height(),root_top=app.root.winfo_rooty(),root_height=app.root.winfo_height(),requested=widget.winfo_reqheight()))
        recorded=[]
        with patch.object(app.messagebox,'showwarning') as warning:
            app.begin()
            checks.append(dict(name='no_consent_no_worker',passed=warning.called and not app.busy))
        with patch.object(app,'thread',lambda f:recorded.append(f)):
            app.consent.set(True);app.begin()
            checks.append(dict(name='consent_only_stages_worker_no_activation',passed=app.busy and len(recorded)==1))
        app.cancel.set();app.busy=False;app.close()
    passed=all(x['passed'] for x in checks) and not attempts
    report=dict(passed=passed,checks=checks,native_activation_attempts=attempts,game_or_hid_or_model_started=False,
                actual_windows_uac_tested=False,actual_customer_hardware_tested=False)
    atomic_json(HERE/'launcher-ui-offline.json',report)
    assert root.resolve().is_relative_to(base.resolve())
    temp.cleanup()
    print(json.dumps(report,ensure_ascii=False))
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(main())
