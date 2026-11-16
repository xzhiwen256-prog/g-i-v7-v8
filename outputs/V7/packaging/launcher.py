"""Customer USB entry: explicit consent, local copy, runtime check, then UAC."""
from pathlib import Path
import ctypes
from ctypes import wintypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

USB = Path(__file__).resolve().parent
sys.path.insert(0, str(USB))
from usb_support import load_manifest, install_payload, atomic_json, accepted_consent, no_links
os.environ['TCL_LIBRARY'] = str(USB/'payload/runtime/tcl/tcl8.6')
os.environ['TK_LIBRARY'] = str(USB/'payload/runtime/tcl/tk8.6')

DISCLOSURE = '''首次启用输入组件将执行以下配置：
• 将 HIDMaestroTestCert 自签证书加入本机 Root / TrustedPublisher 信任库；
• 安装 HIDMaestro 2.0.3 UMDF 用户态驱动，在 Program Files 写入组件；
• 创建虚拟鼠标及本机输入后台。后台可能持续到手动停止或重启。

不关闭安全软件，不修改 Secure Boot / 内存完整性 / 测试签名设置。
如果系统策略或游戏拒绝该输入设备，程序不能保证可用，也不会绕过拦截。
识别运行、输入后台连接、游戏实际接受输入，是三个不同状态。

运行库随包提供，不需预装 Python / CUDA Toolkit / .NET。首次复制到本机可能需要数分钟及约 9 GB 空间；后续直接使用本机副本。'''


def _elevate_process(executable, arguments, cwd, *, state_file=None, nonce=None, timeout=180):
    """Request normal Windows UAC. Never provide credentials or suppress UAC."""
    class ExecuteInfo(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('fMask', wintypes.ULONG), ('hwnd', wintypes.HWND),
                    ('lpVerb', wintypes.LPCWSTR), ('lpFile', wintypes.LPCWSTR), ('lpParameters', wintypes.LPCWSTR),
                    ('lpDirectory', wintypes.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', wintypes.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', wintypes.LPCWSTR), ('hkeyClass', wintypes.HKEY),
                    ('dwHotKey', wintypes.DWORD), ('hIcon', wintypes.HANDLE), ('hProcess', wintypes.HANDLE)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ExecuteInfo)]
    shell.ShellExecuteExW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    info = ExecuteInfo()
    info.cbSize, info.fMask = ctypes.sizeof(info), 0x40 | 0x100
    info.lpVerb, info.lpFile = 'runas', str(executable)
    info.lpParameters, info.lpDirectory, info.nShow = subprocess.list2cmdline(arguments), str(cwd), 0
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        raise OSError('未批准管理员授权；未继续配置。' if code == 1223 else f'管理员启动失败（{code}）')
    began, ready_at = time.monotonic(), None
    try:
        while time.monotonic()-began < timeout:
            wait = kernel.WaitForSingleObject(info.hProcess, 200)
            state = None
            if state_file is not None and Path(state_file).exists():
                try:
                    state = json.loads(Path(state_file).read_text('utf-8'))
                except (OSError, ValueError):
                    pass
                if state and state.get('nonce') == nonce:
                    if state.get('state') == 'failed':
                        raise RuntimeError(state.get('error', '输入配置失败'))
                    if state.get('state') == 'already_running':
                        return state
                    if state.get('state') == 'launched':
                        ready_at = ready_at or time.monotonic()
                        if time.monotonic()-ready_at >= 2:
                            return state
            if wait == 0:
                code = wintypes.DWORD()
                if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
                    raise OSError('无法读取配置进程结果')
                if state_file is None:
                    return dict(exit_code=code.value)
                if state and state.get('nonce') == nonce and state.get('state') == 'launched':
                    return state
                raise RuntimeError(f'配置进程退出（{code.value}），没有成功启动确认；请查看本机日志')
            if wait != 258:
                raise OSError('等待配置进程失败')
        raise TimeoutError('配置等待超时；未将“识别成功”作为输入可用证明，请查看本机日志')
    finally:
        if info.hProcess:
            kernel.CloseHandle(info.hProcess)


def elevate(executable, arguments, cwd, **options):
    # ShellExecuteEx may use COM. Initialize it on this worker thread rather
    # than depending on whichever apartment the GUI/main thread used.
    ole = ctypes.WinDLL('ole32', use_last_error=True)
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    result = ole.CoInitializeEx(None, 2)
    if result not in (0, 1):
        raise OSError('无法初始化 Windows 配置线程，未启动管理员程序')
    try:
        return _elevate_process(executable, arguments, cwd, **options)
    finally:
        ole.CoUninitialize()


def setup_mutex():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, r'Local\V7USB.Client.Setup.v1')
    if not handle:
        raise OSError('无法建立配置单实例保护')
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return kernel, None
    return kernel, handle


class Launcher:
    def __init__(self):
        import tkinter as tk
        from tkinter import ttk, messagebox
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.manifest, self.manifest_hash = load_manifest(USB)
        local = os.environ.get('LOCALAPPDATA')
        if not local:
            raise RuntimeError('无法定位当前用户的 LocalAppData，未修改系统')
        self.product = Path(local)/'V7PortableClient'
        self.product.mkdir(parents=True, exist_ok=True)
        no_links(self.product,self.product)
        self.root = tk.Tk(); self.root.title('V7 客户版 · 首次配置与启动')
        self.root.geometry('760x650'); self.root.minsize(650, 600)
        self.root.configure(bg='#111318')
        style = ttk.Style(self.root); style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI',10))
        style.configure('TFrame', background='#111318')
        style.configure('TLabel', background='#111318', foreground='#F2F4F8')
        style.configure('Muted.TLabel', foreground='#9AA4B5')
        style.configure('TButton', padding=(12,8))
        style.configure('TCheckbutton', background='#111318',foreground='#F2F4F8')
        outer = ttk.Frame(self.root, padding=22); outer.pack(fill='both',expand=True)
        controls = ttk.Frame(outer); controls.pack(side='bottom',fill='x',pady=(12,0))
        ttk.Label(outer,text='V7 客户启动器',font=('Microsoft YaHei UI',20,'bold')).pack(anchor='w')
        ttk.Label(outer,text='整包复制到 U 盘后保留目录结构 · 不需要访问开发电脑',style='Muted.TLabel').pack(anchor='w',pady=(4,14))
        body = ttk.Frame(outer); body.pack(fill='both',expand=True)
        scroll = ttk.Scrollbar(body,orient='vertical');scroll.pack(side='right',fill='y')
        canvas = tk.Canvas(body,bg='#111318',highlightthickness=0,yscrollcommand=scroll.set)
        canvas.pack(side='left',fill='both',expand=True);scroll.configure(command=canvas.yview)
        description = ttk.Label(canvas,text=DISCLOSURE,wraplength=680,justify='left')
        item=canvas.create_window((0,0),window=description,anchor='nw')
        def reflow(event):
            canvas.itemconfigure(item,width=event.width)
            description.configure(wraplength=max(200,event.width-10))
        canvas.bind('<Configure>',reflow)
        description.bind('<Configure>',lambda event:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<MouseWheel>',lambda event:canvas.yview_scroll(-1 if event.delta>0 else 1,'units'))
        previous = accepted_consent(self.product,self.manifest['driver_component_sha256'])
        self.had_consent = previous
        self.consent = tk.BooleanVar(value=previous)
        ttk.Checkbutton(controls,text='我了解并同意在此电脑配置上述证书和输入组件',variable=self.consent).pack(anchor='w',pady=(16,8))
        ttk.Label(controls,text='此同意只保存在当前电脑，不随 U 盘带到其他电脑。',style='Muted.TLabel').pack(anchor='w')
        modes = ttk.Frame(controls); modes.pack(fill='x',pady=(12,8))
        ttk.Label(modes,text='识别方式').pack(side='left')
        self.mode = tk.StringVar(value='自动选择')
        self.mode_box = ttk.Combobox(modes,textvariable=self.mode,state='readonly',
                                    values=('自动选择','兼容识别（PyTorch）','CPU（性能不保证）'))
        self.mode_box.pack(side='right')
        self.progress = ttk.Progressbar(controls, maximum=1); self.progress.pack(fill='x',pady=8)
        self.status = tk.StringVar(value='尚未配置 · 尚未连接输入后台 · 尚未验证游戏接收')
        ttk.Label(controls,textvariable=self.status,wraplength=700,justify='left').pack(fill='x',pady=8)
        row = ttk.Frame(controls); row.pack(side='bottom',fill='x',pady=(12,0))
        self.start = ttk.Button(row,text='开始配置并启动',command=self.begin); self.start.pack(side='left')
        self.repair = ttk.Button(row,text='安装 / 修复 VC++ 运行库',command=self.repair_runtime,state='disabled'); self.repair.pack(side='right')
        self.events, self.cancel = queue.Queue(), threading.Event()
        self.busy, self.phase, self.install_root = False, '', None
        self.request = None
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.root.after(100,self.poll)

    def thread(self, function):
        def run():
            try:
                function()
            except Exception as exc:
                self.events.put(('error',str(exc)))
                try:
                    atomic_json(self.product/'last-launch-error.json',dict(error=str(exc),recorded_unix=time.time(),
                                release=self.manifest['release_id'],phase=self.phase,game_input_verified=False))
                except OSError:
                    pass
        threading.Thread(target=run,name='V7-CustomerSetup',daemon=True).start()

    def begin(self):
        if self.busy:
            return
        if not self.consent.get():
            self.messagebox.showwarning('尚未同意','配置输入组件会改变此电脑的证书信任库和驱动。不同意则不安装。',parent=self.root)
            return
        self.busy=True; self.cancel.clear(); self.start.configure(state='disabled'); self.repair.configure(state='disabled')
        self.mode_box.configure(state='disabled'); self.phase='copy'
        def prepare():
            last=[0.]
            def progress(message,value):
                now=time.monotonic()
                if now-last[0]>.1 or value>=1:
                    last[0]=now; self.events.put(('progress',message,value))
            target=install_payload(USB,self.product,self.manifest,self.manifest_hash,progress,self.cancel.is_set)
            self.install_root=target; self.phase='runtime_check'
            self.events.put(('progress','正在检查本机 Python / GPU 运行库；没有启动识别或发送输入',1.))
            env=os.environ.copy()
            for key in ('PYTHONHOME','PYTHONPATH'):
                env.pop(key,None)
            result=subprocess.run([str(target/'runtime/python.exe'),'-I','-B',str(target/'runtime_probe.py')],
                                  cwd=target,env=env,capture_output=True,text=True,encoding='utf-8',
                                  timeout=90,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                probe=json.loads(result.stdout.strip().splitlines()[-1])
            except (ValueError,IndexError):
                probe=dict(ok=False,error=(result.stderr or result.stdout or '运行库检查未返回结果')[-3000:])
            atomic_json(self.product/'runtime-check.json',probe)
            self.events.put(('probe',probe))
        self.thread(prepare)

    def start_admin(self):
        target=self.install_root
        mode={'自动选择':'auto','兼容识别（PyTorch）':'torch','CPU（性能不保证）':'cpu'}[self.mode.get()]
        nonce=uuid.uuid4().hex
        self.request=self.product/'requests'/(nonce+'.json')
        atomic_json(self.request,dict(nonce=nonce,install_root=str(target),mode=mode,
                    explicit_install_consent=True,driver_component_sha256=self.manifest['driver_component_sha256']))
        self.phase='input_configuration'
        self.status.set('请自行确认 Windows 管理员授权；正在配置输入组件。不会自动开启识别或跟随。')
        def launch():
            result=elevate(target/'runtime/pythonw.exe', ['-I','-B',str(target/'customer_admin.py'),
                           '--request',str(self.request),'--nonce',nonce],target,
                           state_file=self.request.with_suffix('.state.json'),nonce=nonce,timeout=180)
            self.events.put(('launched',result))
        self.thread(launch)

    def repair_runtime(self):
        if self.busy or self.install_root is None:
            return
        if not self.messagebox.askyesno('安装 Microsoft 运行库',
                '将运行包内 Microsoft 签名的 Visual C++ x64 安装程序，并请求管理员权限。不会自动安装显卡驱动或关闭系统保护。是否继续？',parent=self.root):
            return
        self.busy=True; self.start.configure(state='disabled'); self.repair.configure(state='disabled')
        self.status.set('请自行确认 Microsoft 运行库安装；可能需要重启。')
        def repair():
            result=elevate(self.install_root/'prerequisites/vc_redist.x64.exe',
                           ['/install','/passive','/norestart'],self.install_root)
            self.events.put(('repaired',result['exit_code']))
        self.thread(repair)

    def poll(self):
        for _ in range(20):
            try:
                event=self.events.get_nowait()
            except queue.Empty:
                break
            kind=event[0]
            if kind=='progress':
                self.status.set(event[1]); self.progress['value']=event[2]
            elif kind=='probe':
                probe=event[1]
                if self.cancel.is_set():
                    self.failed('配置已取消，未启动输入组件'); continue
                if not probe.get('ok'):
                    self.failed('运行库检查失败：'+probe.get('error','未知错误')+'\n可安装包内 Microsoft 运行库后重试；若仍失败请提供本机诊断。')
                    self.repair.configure(state='normal'); continue
                if probe.get('game_running') and not self.had_consent:
                    self.failed('首次配置前请先退出游戏，然后点击开始重试；当前尚未安装输入组件。'); continue
                if not probe.get('cuda') and not self.messagebox.askyesno('未检测到可用 NVIDIA CUDA',
                    '只能用 CPU 兼容识别，速度可能不足，不能保证实时使用。可改用支持的 NVIDIA 显卡及官方驱动。是否仍继续？',parent=self.root):
                    self.failed('未启用 CPU 兼容模式；未启动输入组件'); continue
                self.start_admin()
            elif kind=='launched':
                state=event[1]
                if state.get('state')=='already_running':
                    self.failed('此电脑已有客户 V7 在运行，请使用已有窗口，勿同时运行多版'); continue
                atomic_json(self.product/'consents'/(self.manifest['driver_component_sha256']+'.json'),
                            dict(accepted=True,consent_version=1,driver_component_sha256=self.manifest['driver_component_sha256'],recorded_unix=time.time()))
                self.status.set('V7 窗口已打开，输入后台已连接；游戏是否接受输入仍未验证。请在 V7 内手动开始识别 / 开启跟随。')
                self.messagebox.showinfo('已打开 V7','识别和跟随默认关闭。首次请在 V7 填写客户本机 DPI / 游戏基础灵敏度；800 / 0.3 只是示例。\n输入后台连接不等于游戏实际接受输入；若游戏拒绝，请停止使用，不要关闭系统保护。\n文件已复制到本机，可在关闭本启动器后安全弹出 U 盘。',parent=self.root)
                self.root.destroy(); return
            elif kind=='repaired':
                self.failed('运行库安装完成，请点击开始重试。' if event[1] in (0,1638) else
                            '运行库要求重启 Windows 后再试。' if event[1]==3010 else f'运行库安装未成功（{event[1]}），未继续')
            elif kind=='error':
                self.failed(event[1])
        self.root.after(100,self.poll)

    def failed(self,message):
        self.busy=False; self.status.set(message); self.start.configure(state='normal'); self.mode_box.configure(state='readonly')

    def close(self):
        if self.busy:
            self.cancel.set()
            if self.phase == 'input_configuration' and self.request is not None:
                try:
                    data=json.loads(self.request.read_text('utf-8'));data['cancelled']=True;atomic_json(self.request,data)
                except (OSError,ValueError):
                    pass
            if self.phase != 'copy':
                self.messagebox.showinfo('请等待当前配置返回','管理员配置一旦开始，关闭窗口不能撤销已经安装的组件。请先完成或取消 Windows 提示。',parent=self.root)
                return
            self.status.set('正在暂停复制，未启动输入组件；稍后可重试。')
            return
        self.root.destroy()


def main():
    kernel, handle = None, None
    try:
        kernel, handle = setup_mutex()
        if handle is None:
            import tkinter as tk
            from tkinter import messagebox
            root=tk.Tk();root.withdraw()
            messagebox.showinfo('已有配置窗口','V7 客户启动器已在运行，请使用已有窗口，勿重复双击。',parent=root)
            root.destroy();return 0
        app=Launcher(); app.root.mainloop(); return 0
    except Exception as exc:
        import tkinter as tk
        from tkinter import messagebox
        root=tk.Tk(); root.withdraw()
        messagebox.showerror('V7 启动失败',str(exc)+'\n请重新复制完整客户文件夹，不要单独复制开始文件。',parent=root)
        root.destroy(); return 1
    finally:
        if kernel is not None and handle:
            kernel.CloseHandle(handle)


if __name__=='__main__':
    raise SystemExit(main())
