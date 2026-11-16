"""Run with Python 3 while Start.cmd's control window is running.
Replace samples with your own relative mouse movement producer.
Uses only the Python standard library and the bundled .NET runtime.
"""
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
env = os.environ.copy()
env["DOTNET_ROOT"] = str(root / "runtime")
process = subprocess.Popen(
    [str(root / "runtime" / "dotnet.exe"),
     str(root / "app" / "VirtualKM.dll"), "stream", "-", "--audit"],
    stdin=subprocess.PIPE, text=True, encoding="utf-8", env=env,
)
try:
    samples = [(7, -3, 20), (2, 1, 20)]
    for dx, dy, dt_ms in samples:
        process.stdin.write(json.dumps({"dx": dx, "dy": dy, "dt_ms": dt_ms}) + "\n")
        process.stdin.flush()
finally:
    process.stdin.close()
return_code = process.wait()
if return_code:
    raise SystemExit(return_code)
