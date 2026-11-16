"""DXcam producer + one-frame mailbox, with QPC presentation timestamps."""
import math
import threading
import time
from screen_source import ScreenSource


class LatestScreenSource(ScreenSource):
    def __init__(self,*args,clock=time.perf_counter,**kwargs):
        super().__init__(*args,**kwargs)
        self.clock=clock
        self.condition=threading.Condition()
        self.closed=threading.Event()
        self.reader=None
        self.packet=None
        self.sequence=0
        self.consumed=0
        self.observed_at=0.
        self.frame_id=None
        self.error=None
        if self.camera is not None:
            try:
                self.camera.start(region=self.region,target_fps=240,video_mode=False)
                self.info.update(mode='latest_only',capture_fps_limit=240,timestamp_source='DXGI_LastPresentTime_QPC',mailbox_capacity=1)
                self.reader=threading.Thread(target=self._read,name='LatestFrame',daemon=True)
                self.reader.start()
            except Exception as exc:
                self.camera.release();self.camera=None
                self.info=dict(self.info,backend='mss',fallback_reason=f'{type(exc).__name__}: {exc}',mode='synchronous')

    def _publish(self,image,stamp):
        now=self.clock()
        if image.shape!=(self.area['height'],self.area['width'],3):raise RuntimeError('DXGI frame dimensions changed')
        # DXcam converts LastPresentTime to the same QPC seconds as Python's
        # perf_counter. Reject invalid/future/foreign clock domains, never
        # replace an old presentation timestamp with a fresh request time.
        if not math.isfinite(stamp) or stamp<=0 or stamp>now+.005 or now-stamp>60:
            raise RuntimeError('DXGI presentation timestamp is outside the QPC clock domain')
        with self.condition:
            if self.packet is not None and stamp<=self.packet[1]:return
            self.sequence+=1
            self.packet=(image,stamp,self.sequence)
            self.condition.notify_all()

    def _read(self):
        try:
            while not self.closed.is_set():
                packet=self.camera.get_latest_frame(copy=True,with_timestamp=True)
                if packet is None:
                    if not self.closed.is_set():
                        raise RuntimeError('DXGI capture reader ended unexpectedly')
                    break
                self._publish(*packet)
        except Exception as exc:
            self.error=f'{type(exc).__name__}: {exc}'
            with self.condition:self.condition.notify_all()

    def grab(self):
        if self.reader is None:
            image,area=super().grab()
            self.observed_at=self.clock();self.frame_id=None
            self.info['timestamp_source']='MSS_capture_completion_estimate'
            return image,area
        with self.condition:
            self.condition.wait_for(lambda:self.sequence>self.consumed or self.error or self.closed.is_set(),timeout=.005)
            if self.error:raise RuntimeError(self.error)
            if self.sequence<=self.consumed:return None,dict(self.area)
            image,self.observed_at,self.frame_id=self.packet
            self.consumed=self.sequence
            return image,dict(self.area)

    def close(self):
        self.closed.set()
        with self.condition:self.condition.notify_all()
        if self.camera is not None and self.reader is not None:self.camera.stop()
        if self.reader is not None:self.reader.join(2)
        super().close()


class LatestRenderer:
    """A slow preview replaces queued work instead of delaying control."""
    def __init__(self,render):
        self.render=render
        self.condition=threading.Condition()
        self.pending=None
        self.stopped=False
        self.error=None
        self.thread=threading.Thread(target=self._run,name='PreviewRenderer',daemon=True)
        self.thread.start()

    def submit(self,payload):
        with self.condition:self.pending=payload;self.condition.notify()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.pending is not None or self.stopped)
                if self.stopped:return
                payload=self.pending;self.pending=None
            try:self.render(payload)
            except Exception as exc:self.error=f'{type(exc).__name__}: {exc}'

    def close(self):
        with self.condition:self.stopped=True;self.pending=None;self.condition.notify()
        self.thread.join(2)
