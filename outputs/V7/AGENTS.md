# V7 independent UI/calibration iteration

- User explicitly requested V7 based on V5. Modify V7 only; preserve the remaining V7/V8 versions and launchers; older versions were deleted by explicit user request on 2026-11-13.
- No recognition/game/model/capture/device/HID/live-card execution for verification. Use fake pointers, fake monitors and harmless offline UI fixtures.
- V7 settings, logs, model cache and diagnostics stay inside V7; shared Python and system HID protocol remain unchanged.
- Do not copy prior personal status/logs/consent. Back up existing V7 files, then write atomically.
- Automatic adaptation is a baseline estimate, not measured in-game calibration. Never apply hardware DPI twice to raw relative counts.

# 用户追加约束：强度与代价透明
- 不以暗中降档、降低增益/追赶/上限、扩大容差或增大响应时间冒充无代价防抖；保留用户当前参数，除非用户明确选择修改。
- 手动输入让权、开火暂停、PD 刹车、柔和容差、滤波/分段延迟以及原有混合中的减弱输出，都须在 UI 明确标出；标称参数不变不代表实际输出强度不变。
- 不默认开启会减弱/暂停辅助的选项；应用前显示草稿的力度/响应代价，应用后保留实际变化说明。

# Cleanup retention (2026-11-13)
- Keep both work/v2-gpu-env and work/fk-gpu-env: the former imports the latter via a .pth file.
- User parameter profiles, runtime code, models, active engines and training source data are protected.
