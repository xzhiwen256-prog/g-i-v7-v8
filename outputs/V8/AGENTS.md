# V8 model-only comparison with V7
- Latest user request: same V7 runtime and parameters, only newly trained model differs.
- Preserve V7/V8, their engines, settings, launchers and shared environment. User explicitly deleted older versions on 2026-11-13; do not recreate them.
- This request authorizes offline ONNX export, TensorRT construction and saved-frame parity checks required for that comparison. No live capture, game, input, HID or app startup for verification.
- Match V7 backend policy, engine shapes, precision, inference FPS policy, filtering, gain, control and parameter profiles. No hidden tuning.
- Version labels, expected model hash and necessary offline-build tools/metadata may differ; disclose numerical TensorRT variation and finite validation limits.
- Keep caches/logs/engine artifacts inside V8. Do not upgrade shared packages or modify HID protocol.
- Back up before overwriting; write atomically. Start recognition, follow and trigger OFF as in V7.

# Cleanup retention (2026-11-13)
- Keep both work/v2-gpu-env and work/fk-gpu-env: the former imports the latter via a .pth file.
- User parameter profiles, runtime code, models, active engines and training source data are protected.

# Latest user override (2026-11-13)
- User explicitly chose fewer ally mislocks over model-only comparison, accepting possible misses with UI disclosure.
- V8 now has a user-selected mixed-friendly-outline veto (separate friend-guard.json). V7 stays unchanged. Turning this guard off restores legacy filtering; do not claim current settings otherwise equal V7.
- Keep numeric gain/chase/caps/response, model/engines and user motion choices unchanged unless separately requested. No live input/capture/model execution for guard verification.
