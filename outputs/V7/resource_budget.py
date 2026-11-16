"""Validated resource limits. No GPU, capture or input APIs are imported here."""
from dataclasses import dataclass,replace
import json

RESOURCE_LABELS={30:'30 FPS · 游戏优先',60:'60 FPS · 均衡',120:'120 FPS · 高刷新',240:'240 FPS · 旧上限（高负载）'}


@dataclass(frozen=True)
class ResourceSettings:
    inference_fps:int=60
    preview_enabled:bool=True

    def __post_init__(self):
        if type(self.inference_fps) is not int or self.inference_fps not in RESOURCE_LABELS:
            raise ValueError('Invalid inference/capture frame limit')
        if type(self.preview_enabled) is not bool:raise ValueError('Invalid preview setting')

    def effective_limit(self,backend):return min(self.inference_fps,240 if backend=='tensorrt' else 60)
    def updated(self,**changes):return replace(self,**changes)
    def to_dict(self):return dict(inference_fps=self.inference_fps,preview_enabled=self.preview_enabled)
    @property
    def label(self):return RESOURCE_LABELS[self.inference_fps]


def load_resource_settings(path):
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict):raise ValueError('Invalid resource profile')
        return ResourceSettings(data.get('inference_fps',60),data.get('preview_enabled',True))
    except (OSError,ValueError,TypeError):return ResourceSettings()
