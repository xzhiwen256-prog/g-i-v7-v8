"""Batch-one TensorRT 10 inference with the pinned YOLOv5 preprocessing/NMS.

Only locally validated engine files may serve the live preview. Building and
validation run separately; the preview never downloads or compiles engines.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import time


class UnsupportedShape(RuntimeError):
    pass


class PrecisionUncertain(RuntimeError):
    pass


def tensor_shape(image_shape, input_size, stride=32):
    h, w = image_shape[:2]
    if h <= 0 or w <= 0:
        raise ValueError('Empty image')
    gain = input_size / max(h, w)
    # Match AutoShape's int truncation before stride rounding exactly.
    return (1, 3, math.ceil(int(h * gain) / stride) * stride,
            math.ceil(int(w * gain) / stride) * stride)


def prepare_bgr(bgr, shape, out=None):
    import numpy as np
    from utils.augmentations import letterbox
    if bgr.ndim != 3 or bgr.shape[2] != 3 or bgr.dtype != np.uint8:
        raise ValueError('Expected uint8 BGR image with three channels')
    padded = bgr if tuple(bgr.shape[:2]) == tuple(shape[2:]) else letterbox(bgr, shape[2:], auto=False)[0]
    # V1 passes BGR directly to AutoShape. Do not introduce an RGB reversal.
    chw = padded.transpose(2, 0, 1)[None]
    if out is not None:
        if out.shape != shape or out.dtype != np.uint8:
            raise ValueError('Invalid preallocated input buffer')
        np.copyto(out, chw)
        return out
    return np.ascontiguousarray(chw)


def approved_entries(root, model_hash, gpu_name, capability, trt_version,
                     allow_unvalidated=False):
    directory = Path(root) / 'weights' / 'tensorrt'
    manifest = json.loads((directory / 'engines.json').read_text(encoding='utf-8'))
    expected = (model_hash, gpu_name, list(capability), trt_version)
    actual = tuple(manifest.get(k) for k in ('model_sha256','gpu_name','compute_capability','tensorrt'))
    if actual != expected:
        raise ValueError('TensorRT 模型/GPU/运行时与构建记录不一致，请重建并验证')
    entries = {}
    for item in manifest['engines']:
        shape = tuple(item['shape'])
        if len(shape) != 4 or shape[:2] != (1, 3) or min(shape) <= 0:
            raise ValueError('Invalid engine shape')
        path = (directory / item['file']).resolve()
        if path.parent != directory.resolve():
            raise ValueError('Engine path outside engine directory')
        if not allow_unvalidated and not item.get('validation', {}).get('passed'):
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('TensorRT 引擎校验失败')
        entries[shape] = (path, item)
    if not entries:
        raise ValueError('没有通过检测一致性验证的 TensorRT 引擎')
    return manifest, entries


class TensorRTDetector:
    def __init__(self, root, model_hash, allow_unvalidated=False, reference=None):
        import torch
        import tensorrt as trt
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable')
        self.torch, self.trt = torch, trt
        self.reference = reference
        self.guard_margin = .02
        self.device = torch.device('cuda:0')
        self.manifest, self.entries = approved_entries(
            root, model_hash, torch.cuda.get_device_name(self.device),
            torch.cuda.get_device_capability(self.device), trt.__version__, allow_unvalidated)
        self.names = {int(k): v for k, v in self.manifest['names'].items()}
        self.logger = trt.Logger(trt.Logger.ERROR)
        self.runtime = trt.Runtime(self.logger)
        self.stream = torch.cuda.Stream(device=self.device)
        self.buffers = None
        self.active_shape = None
        self.timings = {}
        self.info = dict(backend='tensorrt', device=str(self.device), torch=torch.__version__,
                         precision='float16', gpu_name=torch.cuda.get_device_name(self.device),
                         tensorrt=trt.__version__, fallback_reason=None,
                         confidence_boundary_guard='pytorch_fp32',
                         validated=not allow_unvalidated)

    def _activate(self, shape):
        if shape not in self.entries:
            raise UnsupportedShape(f'TensorRT 尚未验证输入形状 {shape[2]}×{shape[3]}，使用 PyTorch')
        if shape == self.active_shape:
            return
        torch, trt = self.torch, self.trt
        self.stream.synchronize()
        self.buffers = None
        engine = self.runtime.deserialize_cuda_engine(self.entries[shape][0].read_bytes())
        if engine is None:
            raise RuntimeError('Cannot deserialize TensorRT engine')
        context = engine.create_execution_context()
        inputs = [engine.get_tensor_name(i) for i in range(engine.num_io_tensors)
                  if engine.get_tensor_mode(engine.get_tensor_name(i)) == trt.TensorIOMode.INPUT]
        outputs = [engine.get_tensor_name(i) for i in range(engine.num_io_tensors)
                   if engine.get_tensor_mode(engine.get_tensor_name(i)) == trt.TensorIOMode.OUTPUT]
        if len(inputs) != 1 or len(outputs) != 1 or tuple(engine.get_tensor_shape(inputs[0])) != shape:
            raise RuntimeError('Unexpected engine I/O')
        dtypes = {trt.float32: torch.float32, trt.float16: torch.float16}
        input_tensor = torch.empty(shape, device=self.device, dtype=dtypes[engine.get_tensor_dtype(inputs[0])])
        output = torch.empty(tuple(context.get_tensor_shape(outputs[0])), device=self.device,
                             dtype=dtypes[engine.get_tensor_dtype(outputs[0])])
        if output.ndim != 3 or output.shape[0] != 1 or output.shape[-1] != 7:
            raise RuntimeError('Expected the two-class YOLOv5 raw prediction tensor')
        host = torch.empty(shape, dtype=torch.uint8, pin_memory=True)
        device_u8 = torch.empty(shape, dtype=torch.uint8, device=self.device)
        context.set_tensor_address(inputs[0], input_tensor.data_ptr())
        context.set_tensor_address(outputs[0], output.data_ptr())
        events = [torch.cuda.Event(enable_timing=True) for _ in range(5)]
        self.buffers = (engine, context, host, device_u8, input_tensor, output, events)
        self.active_shape = shape
        self.info['input_shape'] = list(shape)

    def infer(self, bgr, confidence=.3, input_size=640):
        from utils.general import non_max_suppression, scale_boxes
        torch = self.torch
        shape = tensor_shape(bgr.shape, input_size)
        self._activate(shape)
        _, context, host, device_u8, input_tensor, output, events = self.buffers
        start = time.perf_counter()
        prepare_bgr(bgr, shape, out=host.numpy())
        prepared = time.perf_counter()
        with torch.inference_mode(), torch.cuda.stream(self.stream):
            events[0].record(self.stream)
            device_u8.copy_(host, non_blocking=True)
            events[1].record(self.stream)
            input_tensor.copy_(device_u8).div_(255)
            events[2].record(self.stream)
            if not context.execute_async_v3(self.stream.cuda_stream):
                raise RuntimeError('TensorRT execution failed')
            events[3].record(self.stream)
            # Retain distinct lower-score boxes for the guard. Higher-score
            # boxes suppress these exactly as in the original NMS ordering.
            result = non_max_suppression(output, max(.001,confidence-self.guard_margin), .45, max_det=100)[0]
            scale_boxes(shape[2:], result[:, :4], bgr.shape[:2])
            events[4].record(self.stream)
            # Wait only for this inference stream, including empty detections.
            events[4].synchronize()
            postprocessed = time.perf_counter()
            values = result.cpu().tolist()
        rows = [dict(box=v[:4], confidence=v[4], class_id=int(v[5]), label=self.names[int(v[5])])
                for v in values]
        finished = time.perf_counter()
        self.timings = dict(preprocess_cpu_ms=round((prepared-start)*1000,3),
                            upload_gpu_ms=round(events[0].elapsed_time(events[1]),3),
                            normalize_gpu_ms=round(events[1].elapsed_time(events[2]),3),
                            forward_gpu_ms=round(events[2].elapsed_time(events[3]),3),
                            postprocess_gpu_ms=round(events[3].elapsed_time(events[4]),3),
                            download_rows_ms=round((finished-postprocessed)*1000,3),
                            total_ms=round((finished-start)*1000,3), breakdown_available=True)
        uncertain=any(abs(v['confidence']-confidence)<=self.guard_margin for v in rows)
        self.info['last_path']='tensorrt_fp16'
        if uncertain:
            if self.reference is None:
                raise PrecisionUncertain('Confidence is near the threshold; FP32 verification is required')
            rows, reference_ms=self.reference.infer(bgr,confidence,input_size)
            self.timings['fp32_guard_ms']=round(reference_ms,3)
            self.info['last_path']='fp32_boundary_guard'
            finished=time.perf_counter()
            self.timings['total_ms']=round((finished-start)*1000,3)
        else:
            rows=[row for row in rows if row['confidence']>confidence]
        return rows, (finished-start)*1000
