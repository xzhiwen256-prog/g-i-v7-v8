# V7 / V8 运行环境、安装与配置流程

更新日期：2026-11-17。适用范围：本仓库的 Windows x64 源码归档。

> **验收边界**：本文根据已有安装脚本、启动代码、requirements、模型/引擎元数据和原机环境文件整理，并核对了相关官方文档。本次没有新建运行环境、安装依赖、启动界面、运行模型、采集屏幕或发送设备输入。下面是供你在目标电脑手动执行的流程，不是“其他电脑已经实测通过”的报告。

## 1. 先选安装目标

| 目标 | Python 包 | 系统/设备条件 | 能说明什么 |
|---|---|---|---|
| 仅检查依赖、查看设置 | Python 3.12、PyTorch CPU 或 GPU 版、通用依赖 | Windows x64；设置查看入口不创建原生输入设备 | 检查安装和 UI，不证明模型、采集或输入正常 |
| PyTorch GPU 推理 | GPU 版 PyTorch、通用依赖 | NVIDIA GPU 与兼容驱动 | 不要求现有 TensorRT 引擎适配；性能与 TensorRT 不同 |
| TensorRT 推理 | GPU 版 PyTorch、TensorRT、ONNX、通用依赖 | 上述条件，加上与目标机器匹配且通过门禁的引擎 | 不是只装一个 TensorRT 包即可完成 |
| 正常管理员启动 | 已配置的 Python 环境、随附输入后台 | 还会涉及 UAC、驱动、虚拟设备和信任证书 | 不是无副作用的环境检测；先读第 8 节 |

第一次建议先完成依赖检查和设置查看，再决定是否进行正常启动。本文不提供规避游戏规则或反作弊系统的配置。

## 2. 原机记录与前置条件

### 2.1 已记录的版本

| 项目 | 记录 / 依据 |
|---|---|
| Python | 3.12.14，来自原机两个 venv 的 pyvenv.cfg；新电脑以 Python 3.12 x64 建环境 |
| torch | 2.7.1+cu128 |
| torchvision | 0.22.1+cu128 |
| TensorRT | tensorrt-cu12 10.9.0.34 |
| ONNX | 1.17.0 |
| NumPy / OpenCV / Pillow | 1.26.4 / 4.10.0.84 / 11.0.0 |
| DXcam / comtypes / MSS | 0.3.0 / 1.4.17 / 9.0.2 |
| 原引擎 GPU | NVIDIA GeForce RTX 5060 Laptop GPU，compute capability 12.0 |
| 输入后台 | HIDMaestro 2.0.3；随附 .NET 10 runtime，归档目录含 10.0.13 runtime |
| 系统记录 | 输入后台旧文档记录 Windows 11 x64 Build 26200；并非其他 Windows 版本的验收证明 |

其余直接依赖以两版的 `requirements-preview.txt`、`requirements-tensorrt.txt` 为准。它们不是包含所有传递依赖、驱动和 wheel 哈希的完整锁文件；不能承诺任意时间安装的所有间接包逐字节相同。

### 2.2 目标电脑先准备

1. Windows x64，优先在受支持、已更新的 Windows 11 上配置。不要将此 Windows 输入/采集流程直接当成 Linux、macOS 或 Windows ARM 的安装方案。
2. Python **3.12 x64**，包含 pip、venv、Tcl/Tk；安装 Python Launcher（`py`）便于选择版本。不要直接选官网最新大版本代替 3.12。原环境为 3.12.14；使用其他 3.12 补丁版本仍需目标机检查，不是本次新增验收。Python 的 3.12.10 发布页提供标准 Windows x64 安装器，可作为明确的安装器入口；安全更新应按 Python 官方发布信息另行核对。
3. Git for Windows，用于私有仓库克隆和后续更新。使用自己的 GitHub 账号登录，不把 Token 填进 URL、脚本或仓库文件。
4. GPU 路径需要对应显卡的 NVIDIA 官方驱动，并支持所用 CUDA 12.8 PyTorch 运行时。不要仅凭 `nvidia-smi` 顶部的 CUDA 字样判断 Python 已装好；它不等于当前 Python 包版本。
5. Microsoft Visual C++ v14 x64 Redistributable，使用微软官方支持版本。这里只需运行库，不要求安装整套 Visual Studio。
6. 网络能访问 GitHub、PyPI、PyTorch wheel 索引及 TensorRT 包下载源。不要用关闭 TLS 校验解决下载问题。
7. 硬盘建议先留出约 **20 GB** 安装与缓存余量；这是预算建议，不是测得的最低配置。原依赖目录接近 9 GB，pip 缓存、下载和备份另占空间；两套独立 GPU 环境会重复占用。没有给出已验证的最低 RAM/显存门槛。

原机的两套共享 venv 没有上传，也不能简单复制成可移植环境。新电脑按下面步骤重新创建；无需安装 Codex，也不需要原机的目录、UUID、授权数据库或 OpenAI API Key 来完成默认本地推理环境配置。

## 3. 克隆仓库

以下命令全部为 **PowerShell**，示例安装到 `D:\Projects\g-i-v7-v8`。没有 D 盘时自行换到有写入权限的位置；优先用短、纯英文路径，避免启动批处理读路径时的编码问题。不要把源码放进 Windows、Program Files 或需要日常提权才能写入的目录。

```powershell
New-Item -ItemType Directory -Path 'D:\Projects' -Force | Out-Null
Set-Location 'D:\Projects'
git clone https://github.com/xzhiwen256-prog/g-i-v7-v8.git
Set-Location 'D:\Projects\g-i-v7-v8'
```

遇到私有仓库 404/Repository not found，先确认当前 GitHub 账号具有访问权限；不要把仓库改为 Public。也可登录后下载 ZIP 并完整解压，但后续不能在 ZIP 目录直接使用 `git pull`。

## 4. 推荐：新电脑建一套共享 GPU 环境

**只在全新克隆中照做**。已有环境先保留，不要反复运行安装脚本覆盖它。以下每条命令成功后再执行下一条；出现错误立即停下，不继续写解释器路径。

### 4.1 创建环境

在仓库根目录执行：

```powershell
py -3.12 -V
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip wheel
```

无需激活环境；下面一直使用明确的 Python 路径，因此也无需修改 PowerShell ExecutionPolicy。

### 4.2 安装 PyTorch GPU 与通用依赖

```powershell
.\.venv\Scripts\python.exe -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cu128
.\.venv\Scripts\python.exe -m pip install -r .\outputs\V7\requirements-preview.txt
.\.venv\Scripts\python.exe -m pip install -r .\outputs\V8\requirements-preview.txt
```

两版当前通用依赖清单一致，第二次安装主要用于显式核对两版要求。不要再执行无版本限制的 `pip install -U torch ultralytics`。本项目不需要音频功能，无须为此安装 torchaudio。

PyTorch 2.7.1 / torchvision 0.22.1 / cu128 的组合来自 [PyTorch 官方历史版本安装表](https://pytorch.org/get-started/previous-versions/)。只读 PyTorch 推理不以安装完整 CUDA 开发工具链为默认前置步骤；但驱动、其他库所需 CUDA DLL 和实际目标机条件仍需核对。缺少具体 DLL 时查官方安装说明，不从不明网站单独下载 DLL。

### 4.3 安装 TensorRT（需要 TensorRT 路径时）

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\outputs\V7\requirements-tensorrt.txt
.\.venv\Scripts\python.exe -m pip install -r .\outputs\V8\requirements-tensorrt.txt
```

两份清单当前均固定 `tensorrt-cu12==10.9.0.34` 和 `onnx==1.17.0`。不要改成不固定版本的 `pip install tensorrt`，也不要混装 cu12/cu13 来试运气。该安装可能需要下载数 GB 的二进制库，长时间下载不等于软件卡死；以 pip 日志和退出结果为准。

**仅使用 PyTorch GPU 时可以不执行本节**：自动后端会在 TensorRT 初始化失败时尝试原有 PyTorch 路径，实际原因见运行详情。此时不能声称取得了 TensorRT 性能。

### 4.4 仅需 CPU 的替代方案

没有可用 NVIDIA GPU 时，在另一份全新克隆或尚未安装 torch 的新环境中，将 4.2 的第一条替换为：

```powershell
.\.venv\Scripts\python.exe -m pip install torch==2.7.1 torchvision==0.22.1 --index-url https://download.pytorch.org/whl/cpu
```

然后安装两版 `requirements-preview.txt`，跳过 TensorRT。CPU 推理不提供 GPU 同等速度保证；不要在现有 GPU 环境中直接用这条命令覆盖安装来切换后端。

### 4.5 现有一键安装脚本的边界

| 脚本 | 实际行为 | 注意 |
|---|---|---|
| `outputs/V*/Setup-Preview.cmd` | 在对应版本目录建 `.venv`，安装 CPU torch 和通用依赖 | 不是 GPU/TensorRT 安装器 |
| `outputs/V*/Setup-Preview-GPU.cmd` | 在对应版本目录建 `.venv`，安装 cu128 torch 和通用依赖，并检查 CUDA | **没有安装 requirements-tensorrt.txt**，不能视为完整 TensorRT 配置 |

它们会先寻找本机 Codex 附带 Python，找不到才执行 `py -3.12`。为了明确新电脑使用哪个解释器，本文推荐上面的手动共享环境流程；不要两种方法混着运行。没有在本次文档更新中修改这些历史脚本。

## 5. 让 V7、V8 都使用这套环境

在仓库根目录执行：

```powershell
$python = (Resolve-Path '.\.venv\Scripts\python.exe').Path
Set-Content -LiteralPath '.\outputs\V7\python-path.txt' -Value $python -Encoding Ascii
Set-Content -LiteralPath '.\outputs\V8\python-path.txt' -Value $python -Encoding Ascii
```

上述 ASCII 写法用于本指南的纯英文安装路径。文件只包含一行完整 `python.exe` 路径，不加额外引号，不填虚拟环境目录本身。

启动器查找顺序是：

1. 对应版本的 `outputs/V7/.venv/Scripts/python.exe` 或 `outputs/V8/.venv/Scripts/python.exe`；
2. 找不到时，读取该版本 `python-path.txt`；
3. 仍无有效解释器则报错。

因此，**仓库根 `.venv` 不会被旧启动器自动发现，必须写上述路径文件**。如果版本目录已经有 `.venv`，它会优先，不能只改路径文件就假定已切换到共享环境。不要为排障直接删除已有环境。

`Start-Preview-CPU.cmd` 还会在没有版本级 `.venv` 时先看 `python-cpu-path.txt`，再看 `python-path.txt`；无论使用 CPU 还是 GPU，普通启动器仍可能创建设备。移动仓库后须重建环境、重新配置路径，不依赖旧 venv 的绝对路径。

这些机器专用路径文件被 `.gitignore` 排除，不要提交到 GitHub。原机双环境的 `.pth` 共享方式不需要在新电脑照搬。

## 6. 先检查安装，不运行模型

在仓库根目录执行。以下检查不导入本项目控制器、不读取录像、不采集屏幕、不发送输入；CUDA 检查可能初始化 GPU 驱动上下文。

```powershell
.\.venv\Scripts\python.exe -c "import sys,struct; print(sys.executable); print(sys.version); print('bits=',struct.calcsize('P')*8)"
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import tkinter,numpy,cv2,PIL,mss,dxcam,comtypes; print('Tk=',tkinter.TkVersion); print('NumPy=',numpy.__version__); print('OpenCV=',cv2.__version__); print('Pillow=',PIL.__version__)"
.\.venv\Scripts\python.exe -c "import torch,torchvision; print('torch=',torch.__version__); print('torchvision=',torchvision.__version__); print('CUDA runtime=',torch.version.cuda); ok=torch.cuda.is_available(); print('CUDA available=',ok); print('GPU=',torch.cuda.get_device_name(0) if ok else 'none')"
```

装了 TensorRT 的机器再执行：

```powershell
.\.venv\Scripts\python.exe -c "import torch,tensorrt,onnx; print('TensorRT=',tensorrt.__version__); print('ONNX=',onnx.__version__)"
```

期望：64 位 Python 3.12；`pip check` 不报告依赖冲突；GPU 路径显示 torch 2.7.1+cu128、torchvision 0.22.1+cu128、CUDA available=True；TensorRT 10.9.0.34、ONNX 1.17.0。CPU 方案显示 CPU torch、CUDA available=False 是正常的。

这些检查通过不代表引擎适配、模型准确率、采集、输入后台或新电脑部署已经通过验收。不要将 `launch_windows_admin.py --check` 当成这种无副作用检查：它仍先申请权限并准备 HID 后台。

## 7. 模型、TensorRT 和新 GPU

### 7.1 模型不能混用

| 版本 | `weights/best.pt` 的 SHA-256 |
|---|---|
| V7 | `f4111a85044a5420f3998c121b679c42924672d3a4fdfa6831d923421dc70cc1` |
| V8 | `66ff4acd68aed070f48dc0841d53e1c8d28a88263c2184cb7413a1a88aa41b54` |

只读检查：

```powershell
Get-FileHash '.\outputs\V7\weights\best.pt' -Algorithm SHA256
Get-FileHash '.\outputs\V8\weights\best.pt' -Algorithm SHA256
```

程序会校验各版权重。不要把 V7 的 best.pt 复制到 V8，或改校验值来绕过报错。`FILE_MANIFEST.json` 记录的是原上传快照中 outputs 文件的哈希，不是后来所有文档提交的完整锁定清单。

### 7.2 现有引擎不是跨电脑通用包

程序根据 `weights/tensorrt/engines.json` 检查模型哈希、GPU 名称、计算能力、TensorRT 版本、引擎文件哈希以及 `validation.passed`；不匹配或规格未获准时不能视为有效加速。

- 原引擎来自 **RTX 5060 Laptop GPU / compute capability 12.0 / TensorRT 10.9.0.34**。仅同属 RTX 50 系列并不足以匹配；即使这些字段相同，也不代表所有驱动/硬件组合已验证。
- V8 历史批准的张量规格为 `1×3×640×640` 和 `1×3×384×640`。
- V8 的 `1×3×736×1280` 文件存在，但历史验证未通过，不能用它做 V7/V8 全屏等条件比较；其他实际张量形状也可能回退。
- `FK_INFERENCE_BACKEND` 默认为 `auto`；初始化失败或规格不支持时可回退 PyTorch。实际后端可能随输入规格变化，应查运行详情，而不是只看文件名。
- `torch` 选项明确走 PyTorch；`tensorrt` 选项要求 TensorRT，失败会报错而不是自动回退。不要在初次部署中强制 TensorRT 掩盖兼容问题。

新电脑可先用 PyTorch 路径完成环境配置；这不是与原机 TensorRT 同速度的承诺。TensorRT 初始化成功时仍会加载 PyTorch 参考模型，不能因为选用 TensorRT 就把 torch 删除。正常运行不会自动下载、重建或验证引擎。NVIDIA 对序列化引擎的兼容性也有条件限制，见[官方说明](https://docs.nvidia.com/deeplearning/tensorrt/latest/architecture/how-trt-works.html)。

如确需在当前 PowerShell 会话明确选择 PyTorch 推理后端，可在正常启动之前设置：

```powershell
$env:FK_INFERENCE_BACKEND = 'torch'  # 只选 PyTorch 推理，不改变输入后端
```

以后需要恢复默认自动选择时，再单独执行：

```powershell
Remove-Item Env:FK_INFERENCE_BACKEND -ErrorAction SilentlyContinue
```

选择好后再启动。无需使用 setx 写成系统/用户永久变量。后端选择不等于 CPU/GPU 选择，也不会免除正常启动的设备/权限副作用。

### 7.3 重建与验证不是一个步骤

**本仓库没有完整上传原先 verification 验证工具和样本，不能据此提供“新电脑重建后自动通过原门禁”的闭环命令。** 不要手工把 `passed` 改为 true，也不要改 GPU 元数据冒充匹配。

V8 的 `build_tensorrt.py` 是现有构建入口，`--shapes` 使用 **高×宽**，例如 `640x640` 或 `384x640`。它会占用 GPU、导出 ONNX，并覆盖所选规格的引擎和 engines.json；新条目的验证状态为 false。重建前必须停用相关程序并单独备份整个 `weights/tensorrt`，在副本中操作。本文不默认要求你执行重建，也不把构建成功视为批准使用。

V7 的同名构建脚本存在第 11 节所列静态入口不一致，不能直接照用。将来若需要在另一种显卡启用 TensorRT，应另行完成构建工具修复（如适用）及独立一致性验证，再发布新的兼容说明。本次仅写文档，不修改或放宽门禁。

## 8. 启动权限、输入后台与设置查看

### 8.1 先只看设置

依赖配置好后，可在仓库根目录手动执行以下已有入口：

```powershell
.\.venv\Scripts\python.exe .\outputs\V8\preview.py --input-unavailable --device cpu
```

V7 将路径中的 V8 换为 V7。这是源码中的“输入未就绪 / 设置查看模式”：使用 UnavailablePointer，不通过管理员启动器创建设备，识别/跟随/扳机被锁定。它仍是打开本项目 UI，不属于前面的纯依赖导入检查；本次未自动执行或在新电脑验收这个命令。

### 8.2 再决定是否正常启动

正常入口为仓库根 `Start-V7.cmd` 或 `Start-V8.cmd`，**一次只运行一个版本**。两版启动器使用相同输入后台协议，不能把它们当成完全隔离的两套设备。

必须先了解下列副作用：

- 启动器调用 `launch_windows_admin.py`，可能触发 Windows UAC 提权；取消提权会导致正常启动失败。
- 它启动/复用 `hid/runtime/dotnet.exe` 与 `hid/app/VirtualKM.dll` 的后台，不只是打开 Tk 窗口。
- 根据随附 HID 上游说明，首次准备设备可能将驱动包加入 Driver Store，并创建本机代码签名证书、加入机器级 Root/TrustedPublisher 信任存储。这属于系统级安装及信任变更，不是普通 Python 包安装。
- `hid/runtime` 已随归档携带 .NET 10 runtime；只运行现有后台不要求安装 .NET SDK。重编 C# 后台才是 SDK 场景，不属于本指南默认配置。
- Python 主界面退出后，健康的 host-mouse 后台可能继续存在，直到显式停止。不要把“关闭界面”当成“驱动卸载”。

**只有在确认来源、许可、系统策略并接受上述变更后，才手动运行正常启动项。** 不接受机器级变更时，停留在依赖检查、设置查看等不创建设备的路径。本文不要求关闭 Secure Boot、杀毒、内存完整性、驱动签名检查或反作弊保护；被系统策略阻止时应停下核对来源和支持条件，不靠降低系统保护解决。

审阅并接受上述系统变更后，如确需正常启动，可在仓库根目录手动执行以下**其中一条**，或双击同名入口：

```powershell
.\Start-V8.cmd
# 或选择 V7（不要同时执行）：.\Start-V7.cmd
```

正常启动如失败，查看本节和第 10 节的日志，不要反复点击多个启动项。输入后台失败时程序可能进入设置查看恢复界面；看见 UI 不代表输入已就绪。`Start-Preview-CPU.cmd` 仍会走该管理员后台，CPU 只指推理设备，不代表不安装/不使用输入后端。

停止后台前先关闭使用它的程序，再手动使用对应版本的 `Stop-Virtual-HID.cmd`；该动作涉及移除本程序设备，不是单纯读状态。驱动卸载涉及共享依赖，应先审阅 `hid/README.md`，不要为排障盲目运行卸载器。

## 9. 首次配置顺序与参数归属

1. 先确认打开的版本、解释器与输入状态；正常使用前关闭另一版控制器。安装本身不会替你开启识别、跟随或自动扳机。
2. 在“常用参数 / 高级设置”核对目标显示器、采集范围、识别输入尺寸及敌方轮廓颜色是否符合自己的环境。新电脑不能直接照搬原机显示器编号、分辨率或窗口记录。
3. 常规编辑先形成草稿，再按“应用全部参数”。有效变更会关闭跟随/扳机，需人工确认后重新开启；某些选项要求先停止识别。档位按钮可能立即应用并重置其他草稿，以 UI 明示为准。
4. 当前 DPI/灵敏度适配是基准估算，不是新电脑的实测校准；不要将示例值或原机 profile 当成通用最佳参数。本次配置文档不会重置、优化或降低用户参数。
5. 只有在你有权使用并明确决定启动采集/识别时，才手动执行相应操作。随后到“运行详情”核对实际采集、设备、推理后端、精度、回退原因和耗时。仅安装包成功不说明使用了哪个后端。
6. V8 友方轮廓冲突保护有“可能漏检”的代价说明；纯头、胸部联合证据限制仍存在。不要通过放宽筛选或修改控制参数来验证安装是否成功。

| 文件/位置 | 作用 | 是否可作为跨电脑原样配置 |
|---|---|---|
| `.settings/adaptation-profile.json` | 灵敏度适配资料 | 否，需核对自己的设备/灵敏度 |
| `.settings/capture-profile.json` | 显示器与采集选择 | 否，显示器布局可能不同 |
| `.settings/foreground-profile.json` | 前台目标身份记录 | 否，目标电脑需重新核对 |
| `.settings/motion-profile.json` | 用户控制选项 | 已归档原 profile，不是通用推荐 |
| `.settings/game-calibration.json` | 校准相关记录 | 不能冒充新机实测 |
| V8 `.settings/friend-guard.json` | 友方轮廓保护选择 | 有明确的误锁/漏检权衡 |
| `python-path.txt` | 当前机器解释器路径 | 必须按本机生成，Git 忽略 |
| 日志、运行诊断、身份/API settings | 会话或个人状态 | 原归档未包含；不要补传密钥 |

这里的 `.settings` 均相对于具体版本目录。V7、V8 各存自己的参数，不要为“修环境”整目录互相覆盖。

## 10. 常见问题

| 现象 | 先查什么 / 如何处理 |
|---|---|
| 私有仓库 404 / Repository not found | GitHub 账号、仓库访问权和 Git 登录；不要改成公开仓库 |
| `py` 不存在或找不到 3.12 | Python x64 与 Launcher 是否安装；也可使用明确的 Python 3.12 安装路径建 venv |
| `Please run Setup-Preview.cmd first` | 启动器实际选中的版本级 `.venv` 与 `python-path.txt` 是否存在；根 `.venv` 必须显式配置路径 |
| 在终端 import 成功，启动项却缺包 | 两者使用了不同解释器；用第 6 节输出的 sys.executable 对照路径文件和启动器优先级 |
| `No module named ...` | 使用目标 `.venv/Scripts/python.exe -m pip` 安装对应固定清单；不要只运行系统 `pip` |
| `pip check` 报冲突 | 停止部署、保留日志，核对 Python/torch/torchvision 与固定清单；不要用 `--no-deps` 强行忽略 |
| CUDA available=False / 只有 CPU | 是否误装 CPU wheel、驱动/显卡是否支持、是否选错解释器；CPU 方案显示 False 本来就正常 |
| `DLL load failed` / WinError 126 | 64 位是否一致、VC++ x64 运行库、包是否完整、具体缺失 DLL 的官方依赖；不要覆盖系统目录 DLL |
| TensorRT 未使用、模型/GPU/运行时不匹配 | 比较 engines.json 的 GPU/版本/模型记录；换显卡不能照搬原引擎；允许自动 PyTorch 回退并核对原因 |
| TensorRT 被强制后直接报错 | 检查当前终端是否设置了 FK_INFERENCE_BACKEND=tensorrt；它不会像 auto 那样回退 |
| 模型校验失败 | 按第 7 节核对对应版本 best.pt；从可信归档恢复，不改校验常量 |
| UAC 取消、HID 未就绪或超时 | 权限、系统策略和输入后台日志；不要并行反复启动，不默认修改安全设置 |
| 页面显示“输入未就绪” | 这是恢复/查看模式，不能据此判断设备已安装成功 |
| 不采集/状态暂停 | 若已经手动开始，检查 UI 的具体原因：前台目标、显示器与输入状态等；先区分安全暂停与安装失败 |
| 参数编辑后没变化 | 是否还是草稿、是否需先停止识别并“应用全部参数”；按 UI 的生效和代价提示操作 |
| 新电脑明显慢 | 核对实际 PyTorch/TensorRT、CPU/CUDA、输入规格与回退原因；不要仅凭 UI 帧数归因，也不要以调低强度冒充修环境 |

日志位于具体版本目录：`ui-startup-console.log`、`ui-startup-error.log`、`hid-launch-error.log`、`preview-error.log`、`hid/app/startup-console.log`、`hid/app/host.log`，以及 `.settings` 中实际生成的诊断 JSON。并非所有文件每次都会存在。

提交问题时提供版本、Python/包版本、GPU/驱动、实际入口、回退原因和脱敏错误片段即可。不要把原始录屏、窗口身份、完整进程诊断、Token、UUID 或旧授权数据库无差别上传。

## 11. 历史工具与归档缺口

以下是**静态检查发现的源码不一致，没有通过实际运行验证**；本次不修程序代码：

- 两版 `video_offline.py` 从 `preview` 导入 `Detector / annotate / MODEL_HASH`，而现有两版 `preview.py` 未导出这几个名称。因此 `Start-Video-Offline.cmd` / 该离线录像脚本不能按旧文档当成已可用的验收入口；须先修复并独立验证。
- V7 `build_tensorrt.py` 从 `preview` 导入 `MODEL_HASH / TorchDetector`，而 V7 现有 `preview.py` 未导出它们。V8 的构建脚本已改从 `v5_runtime` 导入；这不等于新电脑构建和验证已完成。
- 所有 `verification/` 历史树未上传，包含原测试、样本、训练数据及一致性报告。各版 README 和 engines.json 指向它们的链接可能不存在，不能冒充仓库可复现的测试记录。
- V7 旧 README 中的 V4/V5/V6 保留描述、原机绝对路径属于历史文本。本仓库只有 V7/V8；`v5_runtime.py` 是当前内部模块，不能据名字删除。
- V8 的后坐力抖动、实际漏检/误锁及新电脑实测仍有限制。环境安装文档不是控制算法修复报告，也不是最小运行时打包审计。

## 12. 配置完成核对表

- [ ] 使用正确账号获取私有仓库，未公开仓库或提交任何密钥。
- [ ] Python 为 3.12 x64，pip 操作全部指向选定环境。
- [ ] 通用依赖通过导入与 pip check；GPU/CPU 分支符合预期。
- [ ] 如需 TensorRT，已装固定版本，清楚引擎与 GPU/版本/验证门禁的关系。
- [ ] 两版 python-path.txt 指向本机有效解释器，明确版本级 .venv 的优先级。
- [ ] 两版模型哈希正确；未覆盖另一版模型、profile 或引擎验证标记。
- [ ] 已区分依赖检查、设置查看、正常管理员启动及各自副作用。
- [ ] 如选择正常启动，已审阅输入后台来源、许可、驱动和机器信任变更；未关闭系统安全保护。
- [ ] 一次只开一个控制器，显示器/目标记录在本机重新核对；未自动开启采集、跟随或扳机。
- [ ] 未把缺失的历史测试、工具静态入口问题或 PyTorch 回退当成 TensorRT 全部验收通过。

## 官方参考与项目依据

- [Python 3.12 venv：重新创建虚拟环境，而非搬移](https://docs.python.org/3.12/library/venv.html)
- [Python 3.12.10 发布页与 Windows x64 安装器](https://www.python.org/downloads/release/python-31210/)
- [PyTorch 历史版本安装表：2.7.1 / 0.22.1 / cu128 或 CPU](https://pytorch.org/get-started/previous-versions/)
- [NVIDIA TensorRT pip 安装方式](https://docs.nvidia.com/deeplearning/tensorrt/latest/installing-tensorrt/install-pip.html)（最新指南可能使用不同默认版本；本项目仍按固定 10.9.0.34/cu12 清单安装）
- [TensorRT 引擎与兼容性说明](https://docs.nvidia.com/deeplearning/tensorrt/latest/architecture/how-trt-works.html)
- [Microsoft Visual C++ Redistributable 官方下载说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist?view=msvc-170)
- 项目依据：两版 requirements、Setup/Start 脚本、launch_windows_admin.py、preview.py、v5_runtime.py、fast_inference.py、build_tensorrt.py、video_offline.py、weights/tensorrt/engines.json、hid/README.md，以及原机 pyvenv.cfg / dist-info 版本记录。原机记录只用于整理版本表，没有把原机环境或个人路径补传到仓库。
