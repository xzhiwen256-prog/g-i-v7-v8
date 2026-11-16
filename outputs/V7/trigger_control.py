"""Opt-in single clicks from verified fresh observations. No native API here."""
import math
from outline_settings import VERIFIED_OUTLINE_REASONS
import threading
import time
from aim_region import chest_candidates


def hit_box(rows, area, confidence, region):
    """Strict evidence required even when the following filter is non-strict."""
    try:
        width,height=float(area['width']),float(area['height'])
        threshold=max(.5,float(confidence))
        if not all(math.isfinite(v) for v in (width,height,threshold)) or min(width,height)<=0:
            return None
    except (KeyError,TypeError,ValueError,OverflowError):return None
    candidates=chest_candidates(rows) if region=='chest' else rows if region=='original' else []
    for row in candidates:
        if (row.get('target_eligible') is not True or row.get('visible') is False or row.get('occluded') is True
                or row.get('team') in ('ally','friendly','teammate')):continue
        if region=='original':
            if (row.get('label')!='enemy_head' or row.get('filter_reason') not in VERIFIED_OUTLINE_REASONS
                    or row.get('outline_evidence',{}).get('passed') is not True):continue
        elif row.get('chest_validation')!='same_frame_head_body_joint':continue
        try:
            x1,y1,x2,y2=map(float,row['box']);score=float(row['confidence'])
            if not all(math.isfinite(v) for v in (x1,y1,x2,y2,score)) or score<threshold or x2<=x1 or y2<=y1:continue
        except (KeyError,TypeError,ValueError,OverflowError):continue
        if region=='chest':
            height_box=y2-y1;y1,y2=y1+.15*height_box,y1+.40*height_box
        inset_x,inset_y=(x2-x1)*.1,(y2-y1)*.1
        if x1+inset_x < width/2 < x2-inset_x and y1+inset_y < height/2 < y2-inset_y:
            return (x1,y1,x2,y2)
    return None


def overlap(a,b):
    intersection=max(0.,min(a[2],b[2])-max(a[0],b[0]))*max(0.,min(a[3],b[3])-max(a[1],b[1]))
    union=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-intersection
    return intersection/union if union>0 else 0.


class AutoTrigger:
    def __init__(self,clock=time.perf_counter):
        self.clock=clock;self.lock=threading.RLock()
        self.enabled=False;self.epoch=0;self.shots=0;self.error=None
        self.last_shot=-math.inf;self.last_observed=-math.inf;self.previous=None;self.confirmed=0
        self.status='关闭';self.inflight=False

    def set_enabled(self,enabled):
        if type(enabled) is not bool:raise ValueError('Trigger switch must be boolean')
        with self.lock:
            self.enabled=enabled;self.epoch+=1;self.previous=None;self.confirmed=0
            self.last_observed=-math.inf
            self.status='已开启 · 等待跟随/新帧确认' if enabled else '关闭'
            if enabled:self.error=None

    def snapshot(self):
        with self.lock:return dict(enabled=self.enabled,status=self.status,shots=self.shots,error=self.error,
            inflight=self.inflight,confirm_frames=2,minimum_confidence=.5,interval_ms=150,
            hold_target_ms=40,minimum_press_interval_ms=190,
            implementation='parallel_button_reports',maximum_frame_age_ms=45,default_enabled=False)

    def update(self,rows,area,observed_at,confidence,region,new_frame,gate,physical_down,click):
        with self.lock:
            if not self.enabled or self.inflight:return
            now=self.clock()
            try:
                fresh=(type(observed_at) in (float,int) and math.isfinite(observed_at)
                       and 0<=now-observed_at<=.045)
                allowed=gate() is True
                held=physical_down()
                if type(held) is not bool:raise ValueError('Physical button state unavailable')
            except Exception as exc:
                self.enabled=False;self.error=str(exc);self.status='已关闭：无法确认保护/手动按键';return
            if not allowed or not fresh or held:
                self.previous=None;self.confirmed=0;self.status='暂停：跟随/前台/帧龄/手动按键保护';return
            if not new_frame or observed_at<=self.last_observed:
                self.status='等待新鲜帧';return
            consecutive=observed_at-self.last_observed<=.1
            self.last_observed=observed_at
            box=hit_box(rows,area,confidence,region)
            if box is None:
                self.previous=None;self.confirmed=0;self.status='等待准心命中已验证区域';return
            self.confirmed=(min(2,self.confirmed+1) if consecutive and self.previous and overlap(box,self.previous)>=.5 else 1)
            self.previous=box
            if self.confirmed<2:self.status='确认 1/2 帧';return
            if now-self.last_shot<.150:self.status='单发冷却';return
            epoch=self.epoch;self.inflight=True
        def guard():
            with self.lock:
                return (self.enabled and self.epoch==epoch and 0<=self.clock()-observed_at<=.045
                        and gate() is True and physical_down() is False)
        try:
            # Backend must recheck guard after waiting for its own lock/connection.
            submitted=click(guard,observed_at+.045)
            with self.lock:
                if submitted is True:
                    self.shots+=1;self.last_shot=self.clock()+.040;self.previous=None;self.confirmed=0
                    self.status='已提交按下 · 后台松键/等待重新确认' if self.enabled else '关闭 · 已提交按下，正在请求松开'
                else:self.status='已取消：提交前保护变化' if self.enabled else '关闭'
        except Exception as exc:
            with self.lock:
                self.enabled=False;self.epoch+=1;self.error=f'{type(exc).__name__}: {exc}'
                self.status='输入失败 · 已关闭，不重试'
        finally:
            with self.lock:self.inflight=False
