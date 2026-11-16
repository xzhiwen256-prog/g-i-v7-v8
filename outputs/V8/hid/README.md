# 本机虚拟键盘和鼠标

已在本机 Windows 11 x64（Build 26200）实现并验证。

运行 `Start.cmd` 并接受 Windows UAC 提示，会显示控制窗口，并创建：

- Colin Virtual keyboard：本地开发 VID F055，PID 0001。
- Colin Virtual mouse：本地开发 VID F055，PID 0002。

这两个 VID/PID 仅用于本机开发，不代表已向 USB-IF 注册的产品标识。

程序使用 HIDMaestro 2.0.3 的 UMDF2 驱动。Windows 将它们枚举为 Keyboard 和 Mouse，输入经过 HID 报告到达 Windows Raw Input。设备随控制窗口的生命周期创建、移除；关闭窗口或运行 `Stop.cmd` 会移除本程序的两个设备。驱动包保留，以便下次启动使用。没有配置开机自启。

## 使用

### 从移动数据生成 HID 鼠标输入

数据通路为：`dx/dy 数据 → 本机命名管道 → HIDMaestro SubmitMouse → UMDF HID 报告 → Windows 鼠标栈 → WM_INPUT / RAWMOUSE`。

保持 `Start.cmd` 的窗口运行，执行：

```powershell
.\Stream.cmd mouse-data.jsonl --audit
```

`mouse-data.jsonl` 每行一个状态：

```json
{"dx":7,"dy":-3,"dt_ms":20}
{"dx":2,"dy":1,"dt_ms":20}
```

`dx/dy` 是相对移动计数，正数表示右/下，负数表示左/上，每帧范围 -32767..32767。它们不是屏幕像素。`dt_ms` 可省略，默认 0，表示本帧发送前等待的毫秒数；采用普通 Windows 用户态调度，不能保证精确的 1ms 发送周期。确认前一帧已处理再提交下一帧，发送延迟也会占用时间。

`--audit` 检查本虚拟鼠标产生的 Raw Input 累计位移是否与输入相等，结果保存至 `app/stream-verification.json`；它检查累计位移，不保证一帧 HID 对应一条 WM_INPUT。Windows 可以合并移动事件。校验时不要同时运行其他发送本虚拟鼠标输入的程序。

查看 HID 报告字节而不发送输入：

```powershell
.\Stream.cmd mouse-data.jsonl --dry-run
```

当前相对鼠标描述符的数据报告为 7 字节（无 Report ID）：`buttons:uint8 | dx:int16LE | dy:int16LE | wheel:int8 | pan:int8`。按钮占低 5 位，其余为填充。例 `(dx=7,dy=-3)` 对应 `00 07 00 FD FF 00 00`。`--dry-run` 显示与该描述符对应的字节预览；实际发送由 HIDMaestro 根据描述符编码。Windows 的 `RAWMOUSE` 是鼠标栈解析 HID 报告后的结构，和这 7 字节是不同格式。

可选字段 `buttons` 为本帧完整的按钮状态（0..31），`wheel/pan` 为滚轮格数（-127..127），默认均为 0。正常结束和输入出错会尝试释放按钮；若强制终止数据生产进程而按钮仍按下，可运行 `Control.cmd release`。仅移动数据不需要这些字段。

实时喂入：使用 `Stream.cmd -`，向标准输入写入一行一帧的 JSON，并及时 flush，关闭标准输入表示结束。例如 PowerShell：

```powershell
'{"dx":7,"dy":-3,"dt_ms":20}' | .\Stream.cmd - --audit
```

`feed_mouse.py` 是可直接改成你自己数据生产逻辑的 Python 示例（需要另有 Python 3）。其他语言也可以启动 `runtime/dotnet.exe app/VirtualKM.dll stream -` 并写入它的标准输入；无需管理员权限。

### 单次命令

保持控制窗口运行，在本文件所在目录打开 PowerShell：

```powershell
.\Control.cmd status
.\Control.cmd move 30 -10
.\Control.cmd click left
.\Control.cmd wheel 1
.\Control.cmd key 4
.\Control.cmd key 4 2
.\Control.cmd release
.\Control.cmd self-test
.\Stop.cmd
```

`key` 的第一个数字是十进制 USB HID Keyboard Usage：4=A，5=B，40=Enter，41=Escape，44=Space，115=F24。第二个数字为可选修饰键掩码：1=左 Ctrl，2=左 Shift，4=左 Alt，8=左 Win，可相加。每次 `key` 自动松开，`click` 支持 left/right/middle。按字母的实际字符受输入法、键盘布局及 Caps Lock 状态影响。

`move` 为相对位移，`wheel` 为滚轮格数。键鼠输入作用于当前桌面；发送字母前先将焦点放到目标输入框。

控制入口为仅允许当前 Windows 账户访问的本机命名管道 `Colin.VirtualKM.Local.v1`，没有 TCP 监听。启动和创建设备需要管理员权限，后续 `Control.cmd` 可从同一账户的普通 PowerShell 运行。

## 验证

`Control.cmd self-test` 或窗口中的 Verify HID 会发送 F24 按下/松开，以及鼠标 +1/-1 相对位移，不点击。程序按设备实际 HID VID/PID 识别 Raw Input 的来源，检查四类事件，结果写入 [app/verification.json](app/verification.json)。`Passed: true` 表示四项均收到。

[device-status.json](device-status.json) 保存本机设备枚举快照；[app/host.log](app/host.log) 保存安装、运行和验证日志。初次调试的失败记录也保留在日志中，最新结果以 verification.json 为准。

## 安装内容与移除

首次运行通过 HIDMaestro 为用户态驱动创建本机代码签名证书 `HIDMaestroTestCert`，加入 LocalMachine 的 Root 和 TrustedPublisher，并将 UMDF 驱动包加入 Driver Store。驱动和辅助文件按上游实现放在 `%ProgramFiles%\HIDMaestro`。未开启测试签名模式，未修改 Secure Boot，也未要求重启。

此前尝试的 VMulti 已签名包仅枚举鼠标及数位板，缺少键盘，因此已移除其试验设备。它的驱动包仍可能保留在 Driver Store，没有活动的 Pentablet HID 试验设备。

仅停止设备：关闭控制窗口或运行 `Stop.cmd`。

完整卸载 HIDMaestro：先停止本程序，再以管理员身份运行官方卸载工具预览，核对它将移除的内容后执行：

```powershell
.\HIDMaestroUninstall\HIDMaestroUninstall.exe --preview
.\HIDMaestroUninstall\HIDMaestroUninstall.exe --yes
```

如果以后有其他软件使用 HIDMaestro，先核对预览中的共享使用情况。本项目没有使用 USB/IP。

## 源码与依赖

- `Program.cs`、`VirtualKM.csproj`：本机控制窗口、命名管道、键鼠命令、Raw Input 验证源码。
- `app/`：编译后的应用。
- `runtime/`：便携 .NET 10 运行环境，无需安装系统级 .NET。
- `lib/`：HIDMaestro 及上游运行依赖。
- `LICENSE`、`THIRD-PARTY-NOTICES.txt`：上游许可。

若修改源码，使用 .NET 10 SDK 执行 `dotnet publish VirtualKM.csproj -c Release --self-contained false -o app`，并在重建前停止运行中的程序。

上游：[HIDMaestro](https://github.com/hifihedgehog/HIDMaestro)，采用官方 [v2.0.3 发布包](https://github.com/hifihedgehog/HIDMaestro/releases/tag/v2.0.3)。下载包 SHA-256 已与发布页一致：`B1578A391971F4296026B692844292DFC55DDBDDBEABC2BCADA8CC36C6E34CD2`。
