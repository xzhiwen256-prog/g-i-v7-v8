"""No-device UI recovery after a failed HID startup. No native imports."""
import json


class UnavailablePointer:
    backend='virtualhid'
    supports_trigger=False
    def __init__(self,error):self.error=error
    def key_down(self,key):return False
    def close(self):pass
    def cancel_trigger(self):pass
    def trigger_error(self):return None
    def check_backend(self):raise OSError(self.error)
    def move_relative(self,*args):raise OSError('输入未就绪，禁止移动')
    move_to=trigger_click=move_relative


def load_startup_error(root):
    try:
        data=json.loads((root/'.settings/hid-launch.json').read_text('utf-8'))
        message=data.get('error') if isinstance(data,dict) else None
        if isinstance(message,str) and message.strip():return message[:8000]
    except (OSError,ValueError):pass
    return '虚拟HID未就绪。请查看 hid-launch-error.log；退出占用设备的程序后关闭此窗口，重新启动V8。'


def show_recovery(app,error):
    app._startup_input_error=error
    app.stop_event.set();app.disarm()
    app.follower.last_status='输入未就绪：识别、跟随、自动扳机已锁定；修复后请关闭窗口重新启动V8'
    app.status.set('输入启动失败 · 设置查看模式 · 未连接设备')
    app.control_status.set(app.follower.last_status)
    app.backend_status.set('设备启动错误（不是运行就绪）：\n'+error)
    for name in ('start_button','follow_toggle','trigger_toggle'):
        widget=getattr(app,name,None)
        if widget is not None:widget.configure(state='disabled')
