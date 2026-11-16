"""Customer-only runtime check. Building/testing the package never runs this."""
import json
import platform
import struct
import subprocess
import sys
import traceback


def main():
    result = dict(python=sys.version.split()[0], bits=struct.calcsize('P')*8,
                  system=platform.system(), machine=platform.machine(), ok=False)
    try:
        if result['system'] != 'Windows' or result['bits'] != 64 or result['machine'].casefold() not in ('amd64','x86_64'):
            raise RuntimeError('仅支持 Windows x64；不支持 32 位或 Windows ARM64 仿真')
        windows=sys.getwindowsversion()
        result['windows_build']=windows.build
        if windows.major<10 or windows.build<19041:
            raise RuntimeError('需要 Windows 10/11 x64（Build 19041 或更新），不自动修改系统版本')
        process = subprocess.run(['tasklist.exe','/FI','IMAGENAME eq VALORANT-Win64-Shipping.exe','/FO','CSV','/NH'],capture_output=True,text=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        result['game_running'] = 'valorant-win64-shipping.exe' in process.stdout.casefold()
        import numpy
        import cv2
        import torch
        import torchvision
        result.update(torch=torch.__version__, numpy=numpy.__version__, opencv=cv2.__version__,
                      torchvision=torchvision.__version__, cuda=bool(torch.cuda.is_available()))
        if result['cuda']:
            result['gpu'] = torch.cuda.get_device_name(0)
            result['compute_capability'] = list(torch.cuda.get_device_capability(0))
        result['ok'] = True
    except Exception as exc:
        result.update(error=str(exc), error_type=type(exc).__name__, traceback=traceback.format_exc())
    print(json.dumps(result, ensure_ascii=True), flush=True)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
