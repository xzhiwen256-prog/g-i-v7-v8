"""V4 opt-in controller: short response and twice V3's far-field rate budget."""
from adaptive_filter import OneEuro2D
from stable_aim import StableAim


class AggressiveAim(StableAim):
    near_response_ms=20.
    bootstrap_response_ms=12.

    def __init__(self,selector):
        super().__init__(selector)
        self.position_filter=OneEuro2D(min_cutoff=4.,beta=.06,derivative_cutoff=6.)
        self.predictor.minimum_observation_interval=.001
