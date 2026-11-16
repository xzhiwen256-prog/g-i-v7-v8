"""Build local static FP16 engines. Live use requires a separate parity check."""
import argparse
import hashlib
import json
from pathlib import Path
import time
from v5_runtime import ROOT, MODEL_HASH, TorchDetector


def build(shapes):
    import torch
    import onnx
    import tensorrt as trt
    detector = TorchDetector(device='cuda')
    core = detector.model.model
    directory = ROOT / 'weights' / 'tensorrt'
    directory.mkdir(exist_ok=True)
    manifest_path = directory / 'engines.json'
    manifest = dict(model_sha256=MODEL_HASH, gpu_name=torch.cuda.get_device_name(0),
                    compute_capability=list(torch.cuda.get_device_capability(0)),
                    tensorrt=trt.__version__, names=detector.names, engines=[])
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding='utf-8'))
        if all(previous.get(k) == manifest[k] for k in ('model_sha256','gpu_name','compute_capability','tensorrt')):
            manifest['engines'] = previous['engines']
    logger = trt.Logger(trt.Logger.WARNING)
    for height, width in shapes:
        shape = [1,3,height,width]
        stem = f'best-fp16-{height}x{width}'
        onnx_path, engine_path = directory / (stem+'.onnx'), directory / (stem+'.engine')
        print(f'EXPORT {height}x{width}', flush=True)
        dummy = torch.zeros(shape, device='cuda')
        with torch.inference_mode():
            core(dummy)
            torch.onnx.export(core, dummy, str(onnx_path), input_names=['images'],
                              output_names=['predictions'], opset_version=12,
                              do_constant_folding=True, dynamo=False)
        onnx.checker.check_model(onnx.load(str(onnx_path)))
        builder = trt.Builder(logger)
        network = builder.create_network(1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH))
        parser = trt.OnnxParser(network, logger)
        if not parser.parse_from_file(str(onnx_path)):
            raise RuntimeError('\n'.join(str(parser.get_error(i)) for i in range(parser.num_errors)))
        config = builder.create_builder_config()
        config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 30)
        config.set_flag(trt.BuilderFlag.FP16)
        config.clear_flag(trt.BuilderFlag.TF32)
        config.builder_optimization_level = 3
        start = time.perf_counter()
        print(f'BUILD {height}x{width}', flush=True)
        serialized = builder.build_serialized_network(network, config)
        if serialized is None:
            raise RuntimeError('TensorRT build failed')
        engine_path.write_bytes(bytes(serialized))
        item = dict(shape=shape, file=engine_path.name,
                    sha256=hashlib.sha256(engine_path.read_bytes()).hexdigest(),
                    build_seconds=round(time.perf_counter()-start,2), validation=dict(passed=False))
        manifest['engines'] = [v for v in manifest['engines'] if v['shape'] != shape] + [item]
        temporary = manifest_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(manifest_path)
        print(json.dumps(item),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--shapes', nargs='+', default=['640x640','736x1280'])
    args = parser.parse_args()
    build([tuple(map(int, item.split('x'))) for item in args.shapes])
