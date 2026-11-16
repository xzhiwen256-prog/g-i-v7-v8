# V7 / V8 — private source and model snapshot

This repository archives the current V7 and unfinished V8 versions on 2026-11-16.

## Contents
- `Start-V7.cmd`, `Start-V8.cmd`: version launchers.
- `outputs/V7`, `outputs/V8`: application sources, documentation, dependency specifications, models and existing TensorRT artifacts.
- Input backend support files, third-party code and their existing notices/licenses.
- Selected parameter profiles only; identity, API and diagnostic settings are excluded.
- `FILE_MANIFEST.json`: SHA-256 hashes of the archived source files.

## Not a self-contained release
Python virtual environments and installed GPU libraries are not bundled. Restore a compatible runtime and configure interpreter paths before use. Existing machine-specific Python path files are intentionally excluded.

TensorRT artifacts are preserved as-is; their presence does not guarantee compatibility with another GPU/runtime. V8 remains unfinished. In particular, the full-screen TensorRT comparison has not passed its gate, and the recoil-jitter issue is not claimed resolved. See each version's existing documentation for its limitations.

## Intentionally not uploaded
- `work/`: local Python environments and dependencies.
- `licensing/`: legacy licensing databases and secrets.
- All `verification/` trees: training datasets, checkpoints, recorded frames, test history and historical diagnostic artifacts.
- Logs, caches, backups, runtime sessions and identity/API settings.
- `python-path.txt` and `python-cpu-path.txt`: local absolute interpreter paths.

These exclusions apply only to this upload snapshot. No original project files were deleted or modified. This repository is an archive, not a new packaging/compatibility validation.

## Third-party material
Existing upstream licenses and notices are retained. Private storage does not grant new redistribution rights. Check those terms before sharing the repository or distributing binaries.