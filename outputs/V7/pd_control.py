"""Bounded PD correction rates. Pure calculations; no GUI or input APIs.

P scales the existing time-based correction rate. D uses the low-pass filtered
screen-error rate minus estimated target-world velocity (when available), not
the predicted aim point. D is capped at 80% of P so it cannot reverse a command
by itself. This is an experimental constrained PD, not a full PID; I is zero.
"""
import math


def validate_pd(algorithm=None,kp=None,kd=None):
    if algorithm is not None and algorithm not in ('legacy','pd','lock'):
        raise ValueError('Unknown control algorithm')
    for name,value,lower,upper in (('PD P multiplier',kp,.25,2.),('PD D damping',kd,0.,1.)):
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float))
                or not math.isfinite(value) or not lower<=value<=upper):
            raise ValueError(name+' is outside the allowed range')


class PDController:
    cutoff_hz=8.
    def __init__(self):self.reset()

    def reset(self):
        self.previous=None;self.stamp=None
        self.derivative=[0.,0.];self.p=[0.,0.];self.d=[0.,0.]

    def observe(self,error,stamp,target_velocity=(0.,0.)):
        if (len(error)!=2 or len(target_velocity)!=2 or isinstance(stamp,bool)
                or not math.isfinite(stamp) or any(not math.isfinite(v) for v in (*error,*target_velocity))):
            raise ValueError('Invalid PD observation')
        dt=stamp-self.stamp if self.stamp is not None else None
        if dt is None or dt<.001 or dt>.1:
            # Seed instead of differentiating an old frame, dropout or new target.
            self.derivative=[0.,0.]
        else:
            alpha=-math.expm1(-2*math.pi*self.cutoff_hz*dt)
            for a in range(2):
                rate=(error[a]-self.previous[a])/dt-target_velocity[a]
                rate=max(-3000.,min(3000.,rate))
                self.derivative[a]+=alpha*(rate-self.derivative[a])
        self.previous=list(error);self.stamp=stamp

    def correction(self,axis,base_rate,gain,kp,kd):
        p=kp*base_rate
        cap=.8*abs(p)
        d=max(-cap,min(cap,gain*kd*self.derivative[axis]))
        self.p[axis]=p;self.d[axis]=d
        return p+d

    def snapshot(self):
        return dict(derivative_px_s=list(self.derivative),p_counts_s=list(self.p),
                    d_counts_s=list(self.d),integral=0.,cutoff_hz=self.cutoff_hz)
