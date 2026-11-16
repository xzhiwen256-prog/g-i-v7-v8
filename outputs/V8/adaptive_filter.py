"""Speed-adaptive One Euro position filtering in compensated image coordinates."""
import math


class OneEuro2D:
    def __init__(self,min_cutoff=4.,beta=.06,derivative_cutoff=6.):
        self.min_cutoff=min_cutoff
        self.beta=beta
        self.derivative_cutoff=derivative_cutoff
        self.reset()

    def reset(self):
        self.time=None
        self.raw=self.value=None
        self.derivative=[0.,0.]

    @staticmethod
    def alpha(cutoff,dt):
        rate=2*math.pi*cutoff*dt
        return rate/(1+rate)

    def update(self,point,stamp):
        point=list(point)
        if self.time is None or not 0<stamp-self.time<=.12:
            self.time=stamp;self.raw=point;self.value=point.copy();self.derivative=[0.,0.]
            return tuple(point)
        dt=stamp-self.time
        alpha=self.alpha(self.derivative_cutoff,dt)
        derivative=[(point[axis]-self.raw[axis])/dt for axis in range(2)]
        self.derivative=[self.derivative[axis]+alpha*(derivative[axis]-self.derivative[axis]) for axis in range(2)]
        cutoff=self.min_cutoff+self.beta*math.hypot(*self.derivative)
        alpha=self.alpha(cutoff,dt)
        self.value=[self.value[axis]+alpha*(point[axis]-self.value[axis]) for axis in range(2)]
        self.time=stamp;self.raw=point
        return tuple(self.value)
