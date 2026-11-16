"""Read per-device relative mouse reports; never suppress or emit input."""
from collections import deque
import ctypes as C
from ctypes import wintypes as W
import math
import threading
import time


class RAWINPUTHEADER(C.Structure):
    _fields_=[('dwType',W.DWORD),('dwSize',W.DWORD),('hDevice',W.HANDLE),('wParam',C.c_size_t)]


class RAWMOUSE(C.Structure):
    _fields_=[('usFlags',W.USHORT),('buttons',W.ULONG),('ulRawButtons',W.ULONG),
              ('lLastX',W.LONG),('lLastY',W.LONG),('ulExtraInformation',W.ULONG)]


class RAWINPUT(C.Structure):
    _fields_=[('header',RAWINPUTHEADER),('mouse',RAWMOUSE)]


class RAWINPUTDEVICE(C.Structure):
    _fields_=[('usUsagePage',W.USHORT),('usUsage',W.USHORT),('dwFlags',W.DWORD),('hwndTarget',W.HWND)]


class RAWINPUTDEVICELIST(C.Structure):
    _fields_=[('hDevice',W.HANDLE),('dwType',W.DWORD)]


def virtual_device(name,vid=None,pid=None):
    normalized=name.upper()
    return (vid==0xF055 and pid==2) or 'VID_F055&PID_0002' in normalized or 'HIDMAESTRO' in normalized


def hid_identity(name):
    class ATTRIBUTES(C.Structure):
        _fields_=[('Size',W.ULONG),('VendorID',W.USHORT),('ProductID',W.USHORT),('VersionNumber',W.USHORT)]
    kernel=C.WinDLL('kernel32',use_last_error=True);hid=C.WinDLL('hid',use_last_error=True)
    kernel.CreateFileW.argtypes=[W.LPCWSTR,W.DWORD,W.DWORD,C.c_void_p,W.DWORD,W.DWORD,W.HANDLE]
    kernel.CreateFileW.restype=W.HANDLE
    kernel.CloseHandle.argtypes=[W.HANDLE]
    hid.HidD_GetAttributes.argtypes=[W.HANDLE,C.POINTER(ATTRIBUTES)];hid.HidD_GetAttributes.restype=C.c_ubyte
    handle=kernel.CreateFileW(name,0,3,None,3,0,None)
    if handle==C.c_void_p(-1).value:return None,None
    try:
        attributes=ATTRIBUTES();attributes.Size=C.sizeof(attributes)
        if hid.HidD_GetAttributes(handle,C.byref(attributes)):return attributes.VendorID,attributes.ProductID
        return None,None
    finally:kernel.CloseHandle(handle)


class RawMouseMonitor:
    def __init__(self,clock=time.perf_counter):
        self.clock=clock
        self.lock=threading.Lock()
        self.events=deque(maxlen=8192)
        self.devices={}
        self.physical_reports=self.virtual_reports=self.unknown_reports=self.overflow=0
        self.left_buttons_down=set()
        self.last_left_press=None
        self.error=None
        self.hwnd=None
        self.ready=threading.Event()
        self.thread=None

    def _device_name(self,handle):
        size=W.UINT()
        if self.api.GetRawInputDeviceInfoW(handle,0x20000007,None,C.byref(size))==0xffffffff:
            return ''
        buf=C.create_unicode_buffer(size.value+1)
        if self.api.GetRawInputDeviceInfoW(handle,0x20000007,buf,C.byref(size))==0xffffffff:
            return ''
        return buf.value

    def _remember(self,handle):
        name=self._device_name(handle)
        vid,pid=hid_identity(name) if name else (None,None)
        # HIDMaestro's actual raw path is HID#HIDCLASS, without VID/PID.
        # Query attributes rather than trusting a substring-only exclusion.
        identified=bool(name) and (vid is not None or 'VID_' in name.upper() or 'ELAN' in name.upper())
        entry=dict(name=name,vid=vid,pid=pid,kind='virtual' if virtual_device(name,vid,pid) else 'physical' if identified else 'unknown')
        with self.lock:self.devices[int(handle or 0)]=entry
        return entry

    def _accept(self,handle,flags,dx,dy,button_flags=0):
        key=int(handle or 0)
        with self.lock:entry=self.devices.get(key)
        if entry is None:entry=self._remember(handle) if key else dict(kind='unknown',name='')
        with self.lock:
            if entry['kind']=='virtual':self.virtual_reports+=1;return
            # Absolute tablet/touch reports and unresolved sources must not be
            # converted into relative camera counts.
            if entry['kind']!='physical' or flags&1:self.unknown_reports+=1;return
            # Button-only reports must survive even when there is no movement.
            # Keep per-device held state; one mouse release must not clear another.
            if button_flags&1:
                self.left_buttons_down.add(key)
                self.last_left_press=self.clock()
            if button_flags&2:
                self.left_buttons_down.discard(key)
            if dx or dy:
                self.physical_reports+=1
                if len(self.events)==self.events.maxlen:self.overflow+=1
                self.events.append((self.clock(),int(dx),int(dy)))

    def drain(self):
        with self.lock:
            events=list(self.events);self.events.clear()
            return events

    def snapshot(self):
        with self.lock:
            return dict(available=bool(self.hwnd) and not self.error,error=self.error,
                        physical_reports=self.physical_reports,virtual_reports_ignored=self.virtual_reports,
                        unknown_reports_ignored=self.unknown_reports,dropped_reports=self.overflow,
                        left_button_down=bool(self.left_buttons_down),last_left_press=self.last_left_press,
                        devices=list(self.devices.values()))

    def start(self):
        if self.thread is not None:return
        self.thread=threading.Thread(target=self._run,name='PhysicalMouse',daemon=True)
        self.thread.start()
        if not self.ready.wait(3):self.error='Raw Input window startup timed out'

    def close(self):
        if self.hwnd:self.api.PostMessageW(self.hwnd,0x0010,0,0)
        if self.thread:self.thread.join(2)

    def _run(self):
        try:
            self.api=C.WinDLL('user32',use_last_error=True)
            kernel=C.WinDLL('kernel32',use_last_error=True)
            PROC=C.WINFUNCTYPE(C.c_ssize_t,W.HWND,W.UINT,C.c_size_t,C.c_ssize_t)
            class WNDCLASS(C.Structure):
                _fields_=[('style',W.UINT),('lpfnWndProc',PROC),('cbClsExtra',C.c_int),('cbWndExtra',C.c_int),
                          ('hInstance',W.HINSTANCE),('hIcon',W.HICON),('hCursor',W.HANDLE),
                          ('hbrBackground',W.HBRUSH),('lpszMenuName',W.LPCWSTR),('lpszClassName',W.LPCWSTR)]
            self.api.DefWindowProcW.argtypes=[W.HWND,W.UINT,C.c_size_t,C.c_ssize_t]
            self.api.DefWindowProcW.restype=C.c_ssize_t
            self.api.CreateWindowExW.argtypes=[W.DWORD,W.LPCWSTR,W.LPCWSTR,W.DWORD,C.c_int,C.c_int,C.c_int,C.c_int,W.HWND,W.HMENU,W.HINSTANCE,C.c_void_p]
            self.api.CreateWindowExW.restype=W.HWND
            self.api.RegisterClassW.argtypes=[C.POINTER(WNDCLASS)]
            self.api.GetRawInputData.argtypes=[W.HANDLE,W.UINT,C.c_void_p,C.POINTER(W.UINT),W.UINT]
            self.api.GetRawInputData.restype=W.UINT
            self.api.GetRawInputDeviceInfoW.argtypes=[W.HANDLE,W.UINT,C.c_void_p,C.POINTER(W.UINT)]
            self.api.GetRawInputDeviceInfoW.restype=W.UINT
            self.api.GetRawInputDeviceList.argtypes=[C.POINTER(RAWINPUTDEVICELIST),C.POINTER(W.UINT),W.UINT]
            self.api.GetRawInputDeviceList.restype=W.UINT
            self.api.RegisterRawInputDevices.argtypes=[C.POINTER(RAWINPUTDEVICE),W.UINT,W.UINT]
            self.api.PostMessageW.argtypes=[W.HWND,W.UINT,C.c_size_t,C.c_ssize_t]
            self.api.DestroyWindow.argtypes=[W.HWND]
            self.api.GetMessageW.argtypes=[C.POINTER(W.MSG),W.HWND,W.UINT,W.UINT]
            self.api.TranslateMessage.argtypes=[C.POINTER(W.MSG)]
            self.api.DispatchMessageW.argtypes=[C.POINTER(W.MSG)]
            self.api.DispatchMessageW.restype=C.c_ssize_t
            kernel.GetModuleHandleW.argtypes=[W.LPCWSTR];kernel.GetModuleHandleW.restype=W.HMODULE

            @PROC
            def procedure(hwnd,msg,wp,lp):
                if msg==0xff:
                    try:
                        size=W.UINT()
                        if self.api.GetRawInputData(W.HANDLE(lp),0x10000003,None,C.byref(size),C.sizeof(RAWINPUTHEADER))!=0xffffffff:
                            storage=C.create_string_buffer(size.value)
                            received=self.api.GetRawInputData(W.HANDLE(lp),0x10000003,storage,C.byref(size),C.sizeof(RAWINPUTHEADER))
                            if received>=C.sizeof(RAWINPUT):
                                raw=C.cast(storage,C.POINTER(RAWINPUT)).contents
                                if raw.header.dwType==0:self._accept(raw.header.hDevice,raw.mouse.usFlags,raw.mouse.lLastX,raw.mouse.lLastY,raw.mouse.buttons&0xffff)
                    except Exception as exc:self.error=f'{type(exc).__name__}: {exc}'
                elif msg==0xfe:
                    try:
                        if wp==1:self._remember(int(lp)) # GIDC_ARRIVAL, including startup notifications
                        else:
                            with self.lock:
                                self.devices.pop(int(lp),None)
                                self.left_buttons_down.discard(int(lp))
                    except Exception as exc:self.error=f'{type(exc).__name__}: {exc}'
                elif msg==0x10:
                    self.api.DestroyWindow(hwnd);return 0
                elif msg==2:
                    self.api.PostQuitMessage(0);return 0
                return self.api.DefWindowProcW(hwnd,msg,wp,lp)

            name=f'FKPhysicalMouseV5_{id(self)}'
            instance=kernel.GetModuleHandleW(None)
            wc=WNDCLASS();wc.lpfnWndProc=procedure;wc.hInstance=instance;wc.lpszClassName=name
            if not self.api.RegisterClassW(C.byref(wc)):raise C.WinError(C.get_last_error())
            try:
                self.hwnd=self.api.CreateWindowExW(0,name,name,0,0,0,0,0,W.HWND(-3),None,instance,None)
                if not self.hwnd:raise C.WinError(C.get_last_error())
                device=RAWINPUTDEVICE(1,2,0x100|0x2000,self.hwnd) # INPUTSINK + DEVNOTIFY; no input capture/suppression
                if not self.api.RegisterRawInputDevices(C.byref(device),1,C.sizeof(device)):raise C.WinError(C.get_last_error())
                count=W.UINT()
                if self.api.GetRawInputDeviceList(None,C.byref(count),C.sizeof(RAWINPUTDEVICELIST))!=0xffffffff:
                    entries=(RAWINPUTDEVICELIST*count.value)()
                    n=self.api.GetRawInputDeviceList(entries,C.byref(count),C.sizeof(RAWINPUTDEVICELIST))
                    if n!=0xffffffff:
                        for entry in entries[:n]:
                            if entry.dwType==0:self._remember(entry.hDevice)
                self.ready.set()
                msg=W.MSG()
                while self.api.GetMessageW(C.byref(msg),None,0,0)>0:
                    self.api.TranslateMessage(C.byref(msg));self.api.DispatchMessageW(C.byref(msg))
            finally:
                remove=RAWINPUTDEVICE(1,2,1,None)
                self.api.RegisterRawInputDevices(C.byref(remove),1,C.sizeof(remove))
                self.api.UnregisterClassW.argtypes=[W.LPCWSTR,W.HINSTANCE]
                self.api.UnregisterClassW(name,instance)
        except Exception as exc:self.error=f'{type(exc).__name__}: {exc}'
        finally:self.hwnd=None;self.ready.set()


class ManualBlend:
    """Strong mode keeps same-direction assistance and softens opposing axes."""
    def __init__(self):
        self.samples=deque()
        self.last_activity=-math.inf
        self.velocity=[0.,0.]
        self.weights=[1.,1.]
        self.soft=False
        self.last_mix=None
        self.fire_priority=False
        self.axis_activity=[-math.inf,-math.inf]
        self.manual_yielded=[False,False]

    def reset_manual_priority(self):
        self.axis_activity=[-math.inf,-math.inf]
        self.manual_yielded=[False,False]
        self.weights=[1.,1.]
        self.last_mix=None

    def observe(self,events,now):
        for stamp,x,y in events:
            self.samples.append((stamp,x,y));self.last_activity=stamp
            for axis,value in enumerate((x,y)):
                if value:self.axis_activity[axis]=stamp
        while self.samples and now-self.samples[0][0]>.024:self.samples.popleft()
        self.velocity=[sum(row[a+1] for row in self.samples)/.024 for a in range(2)]

    def mix(self,delta,now,fire_down=False):
        result=[];previous=self.weights;self.weights=[]
        dt=max(0.,min(.05,now-self.last_mix)) if self.last_mix is not None else 1/60
        self.last_mix=now
        for axis,value in enumerate(delta):
            opposing=value*self.velocity[axis]<0
            speed=abs(self.velocity[axis])
            reduction=max(0.,min(1.,(speed-250.)/2250.)) if opposing else 0.
            weight=1.-(1. if self.soft else .55)*reduction
            if self.soft and weight>previous[axis]:
                # Yield immediately; restore assistance gradually after the hand
                # stops opposing it instead of jumping at the 24 ms window edge.
                weight=previous[axis]+(weight-previous[axis])*(-math.expm1(-dt/.08))
            if not self.fire_priority:
                self.manual_yielded[axis]=False
            else:
                active=now-self.axis_activity[axis]<.100
                if fire_down and active:self.manual_yielded[axis]=True
                if self.manual_yielded[axis]:
                    if active:
                        # Any nonzero physical movement claims this axis, even
                        # slow movement or the same direction as the assist.
                        weight=0.
                    else:
                        target=weight
                        weight=min(target,previous[axis]+(target-previous[axis])*(-math.expm1(-dt/.080)))
                        if abs(weight-target)<.001:
                            weight=target;self.manual_yielded[axis]=False
            self.weights.append(weight);result.append(round(value*weight))
        return tuple(result)

    def snapshot(self):
        return dict(physical_velocity_counts_s=[round(v,1) for v in self.velocity],
                    opposing_axis_weights=list(self.weights),
                    fire_manual_priority=self.fire_priority,manual_yielded_axes=list(self.manual_yielded),
                    policy='fire_manual_axis_priority' if self.fire_priority else
                           'manual_priority_smooth_release' if self.soft else 'strong_directional_blend')
