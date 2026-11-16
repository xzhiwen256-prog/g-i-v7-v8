"""Opt-in bounded metadata for existing partitioned callbacks, never input logic.

Caller holds the follower state lock for enable/snapshot/append. Context is
thread-local. No background tasks, screen/model/device or file operations.
"""
from collections import deque
import copy
import math
import threading


class PartitionTiming:
    capacity=64
    def __init__(self):
        self.enabled=False
        self.generation=0
        self._context=threading.local()
        self._records=deque(maxlen=self.capacity)
        self._next_id=0

    @staticmethod
    def timestamp(value):
        return value if isinstance(value,(int,float)) and not isinstance(value,bool) and 0<=value<=1e12 and math.isfinite(value) else None

    def set_enabled(self,value):
        if type(value) is not bool:raise ValueError('Timing diagnostic flag must be boolean')
        if value and not self.enabled:self.generation+=1
        self.enabled=value

    def observe_context(self,value):
        if not self.enabled:return
        data=value if isinstance(value,dict) else {}
        result={'diagnostic_generation':self.generation}
        for name in ('loop_id','source_frame_id'):
            v=data.get(name)
            result[name]=v if type(v) is int and 0<=v<=10**12 else None
        for name in ('observed_at','capture_started_at','capture_completed_at','inference_completed_at','filter_completed_at'):
            result[name]=self.timestamp(data.get(name))
        result['new_frame']=data.get('new_frame') if type(data.get('new_frame')) is bool else None
        self._context.value=result

    def context(self):
        value=getattr(self._context,'value',None)
        return dict(value) if isinstance(value,dict) and value.get('diagnostic_generation')==self.generation else None

    def append(self,*,context,started_at,returned_at,captured_at,command_generation,planned,acknowledged,complete,overrun,reports,diagnostic_generation):
        # Called only for an opted-in command; a switch-off may finish that one.
        self._next_id+=1
        self._records.append(dict(record_id=self._next_id,diagnostic_generation=diagnostic_generation,
            context=copy.deepcopy(context),context_available=context is not None,
            dispatch_started_at=self.timestamp(started_at),dispatch_sampled_end_at=self.timestamp(returned_at),
            observed_at=self.timestamp(captured_at),command_generation=command_generation,
            planned_counts=list(planned),acknowledged_counts=list(acknowledged),complete=bool(complete),
            scheduling_overrun=bool(overrun),report_intervals=copy.deepcopy(reports[:4])))

    def snapshot(self):
        return dict(enabled=self.enabled,generation=self.generation,capacity=self.capacity,
            records=copy.deepcopy(list(self._records)),
            scope='last64 opted-in partitioned commands only; callback return is not game/display acknowledgment',
            cost='extra timing/CPU/memory/serialization/disk work may affect frame rate and staleness; no zero-overhead guarantee',
            privacy='no images or physical mouse samples; existing automatic counts/timestamps only')
