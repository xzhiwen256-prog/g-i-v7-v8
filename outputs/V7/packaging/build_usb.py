"""Owner-side static build. Never launches the model, driver or customer entry."""
from pathlib import Path
import ast
import hashlib
import json
import os
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
V7 = HERE.parent
PROJECT = V7.parent.parent
DESKTOP = Path(r'C:\Users\Colin\Desktop\v')
NAME = 'V7客户U盘完整包'
OUTPUT = DESKTOP/NAME
STAGING = HERE/'staging'/'V7-USB.building'
RELEASE = 'V7-USB-20261107-freeze241-r1'
PYTHON = Path(sys.base_prefix)
PACKAGES = (PROJECT/'work/fk-gpu-env/Lib/site-packages', PROJECT/'work/v2-gpu-env/Lib/site-packages')
SKIP_DIRS = {'__pycache__','.git','.backups','.pytest_cache','.mypy_cache','include','libs','torchgen'}
SKIP_SUFFIX = {'.pyc','.pyo','.pdb','.lib','.whl','.obj','.exp','.ilk'}
SOURCE_FILES = tuple(sorted(json.loads((V7/'verification/v7-delivery.json').read_text('utf-8'))['current_source_sha256']))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def write(path, text, encoding='utf-8'):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.building.tmp')
    temporary.write_text(text,encoding=encoding,newline='\n')
    os.replace(temporary,path)


def js(path,data):
    write(path,json.dumps(data,ensure_ascii=False,indent=2)+'\n')


def copy(source, target):
    if source.is_symlink() or source.is_junction():
        raise ValueError('Do not package linked files: '+str(source))
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists() or sha(source)!=sha(target):
        shutil.copy2(source,target)


def tree(source,target,*,standard_library=False):
    for root,dirs,files in os.walk(source):
        dirs[:]=[d for d in dirs if d not in SKIP_DIRS and not (standard_library and d=='site-packages')]
        for name in files:
            p=Path(root)/name
            if p.suffix.casefold() in SKIP_SUFFIX or name in ('direct_url.json',) or name.endswith('.pth'):
                continue
            copy(p,target/p.relative_to(source))


def main():
    if OUTPUT.exists():
        raise RuntimeError('Final output already exists; refusing to overwrite customer files')
    if not STAGING.resolve().is_relative_to((HERE/'staging').resolve()) or not OUTPUT.resolve().is_relative_to(DESKTOP.resolve()):
        raise ValueError('Build paths escaped the named source/output directories')
    if not (HERE/'assets/vc_redist.x64.exe').is_file():
        raise ValueError('Missing verified Microsoft prerequisite installer')
    if sha(HERE/'assets/vc_redist.x64.exe')!='cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b':
        raise ValueError('Microsoft installer differs from the signature-verified download')
    baseline=json.loads((V7/'verification/v7-delivery.json').read_text('utf-8'))['current_source_sha256']
    if any(sha(V7/n)!=h for n,h in baseline.items()):
        raise ValueError('Latest V7 source verification baseline changed')
    STAGING.mkdir(parents=True,exist_ok=True)
    payload=STAGING/'payload';runtime=payload/'runtime';app=payload/'app'
    print('Copying isolated Python standard library...',flush=True)
    for name in ('python.exe','pythonw.exe','python3.dll','python312.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt'):
        copy(PYTHON/name,runtime/name)
    for name in ('DLLs','tcl'):
        tree(PYTHON/name,runtime/name)
    tree(PYTHON/'Lib',runtime/'Lib',standard_library=True)
    # An explicit relative search path replaces both venv absolute paths and .pth
    # links to the developer machine. Site-package directories are consolidated.
    write(runtime/'python312._pth','.\nLib\nDLLs\np\nimport site\n')
    print('Copying complete GPU/CPU runtime dependencies (this may take several minutes)...',flush=True)
    for source in PACKAGES:
        tree(source,runtime/'p')
    print('Copying latest V7 and public defaults...',flush=True)
    for name in SOURCE_FILES:
        copy(V7/name,app/name)
    copy(V7/'version.json',app/'version.json')
    copy(V7/'LICENSE.upstream',app/'LICENSE.upstream')
    tree(V7/'vendor',app/'vendor')
    copy(V7/'weights/best.pt',app/'weights/best.pt')
    engines=json.loads((V7/'weights/tensorrt/engines.json').read_text('utf-8'))
    for entry in engines['engines']:
        if not entry.get('validation',{}).get('passed') or Path(entry['file']).name!=entry['file']:
            raise ValueError('Unvalidated/unsafe engine entry')
        src=V7/'weights/tensorrt'/entry['file']
        if sha(src)!=entry['sha256']:
            raise ValueError('Engine hash mismatch')
        copy(src,app/'weights/tensorrt'/entry['file'])
    # Existing runtime will only approve these cached engines for the exact
    # GPU/capability/TRT profile in this manifest; all other GPUs use fallback.
    js(app/'weights/tensorrt/engines.json',engines)
    tree(V7/'driver',app/'driver')
    tree(V7/'hid/runtime',app/'hid/runtime')
    for p in (V7/'hid/app').iterdir():
        if p.is_file() and p.suffix in ('.dll','.exe') or p.is_file() and p.name in ('VirtualKM.deps.json','VirtualKM.runtimeconfig.json'):
            copy(p,app/'hid/app'/p.name)
    tree(V7/'hid/HIDMaestroUninstall',app/'hid/HIDMaestroUninstall')
    for name in ('LICENSE','THIRD-PARTY-NOTICES.txt'):
        copy(V7/'hid'/name,app/'hid'/name)
    for name in ('launcher.py','usb_support.py'):
        copy(HERE/name,STAGING/name)
    for name in ('usb_support.py','runtime_probe.py','customer_admin.py','app_entry.py'):
        copy(HERE/name,payload/name)
    copy(HERE/'assets/vc_redist.x64.exe',payload/'prerequisites/vc_redist.x64.exe')
    js(app/'.settings/foreground-profile.json',dict(allowed_executable_names=['VALORANT-Win64-Shipping.exe']))
    js(app/'.settings/adaptation-profile.json',dict(schema=1,dpi=800,sensitivity=.3,base_limit=480,basis_level=6))
    js(app/'.settings/capture-profile.json',dict(scope='center',input_size=640))
    js(app/'.settings/motion-profile.json',dict(gain=2.68,step_limit=480,aim_height_ratio=.12,response_ms=6,chase_strength=6.,
       predictive=True,enemy_filter=True,soft_assist=False,tolerance_px=3.,pause_on_fire=False,fire_release_ms=0,
       control_algorithm='legacy',pd_kp=1.,pd_kd=.25,manual_fire_priority=False,paced_corrections=False))
    calibration=json.loads((V7/'.settings/game-calibration.json').read_text('utf-8-sig'))
    # Explicit numeric calibration only, no personal settings/cache/paths.
    js(app/'.settings/game-calibration.json',{k:calibration[k] for k in ('game','sensitivity','resolution','zoomed',
       'horizontal_fov_degrees','degrees_per_count_at_sens1','step_limit','calibrated_in_game')})
    write(STAGING/'开始.cmd','''@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0payload\\runtime\\pythonw.exe" goto missing
"%~dp0payload\\runtime\\pythonw.exe" -I -B "%~dp0launcher.py"
if errorlevel 1 pause
exit /b
:missing
echo Missing package runtime. Copy the WHOLE V7 customer folder, not only this file.
pause
exit /b 1
''','utf-8')
    write(STAGING/'使用说明.txt','''V7 客户 U 盘完整包 — 2026-11-07

1. 把整个“V7客户U盘完整包”文件夹复制到 U 盘，客户双击“开始.cmd”。不能只复制开始文件。
2. 首次运行请先退出游戏，阅读证书 / 用户态驱动配置提示，明确同意后自行确认 Windows UAC。复制和校验可能需要数分钟。
3. V7 窗口打开后，首次填入客户本机鼠标 DPI 和游戏内基础灵敏度并应用。800 / 0.3 是默认示例，不是检测到的客户数据。
4. 手动点击“开始识别”，回到游戏后按 F8 开启 / 关闭跟随。默认不开启识别或跟随。

无需客户预装 Python、.NET、CUDA Toolkit 或另外下载大依赖。需要 Windows 10/11 x64（Build 19041 或更新）；NVIDIA 显卡仍需合适的官方驱动。CPU 兼容模式不保证实时性能。不支持 Windows ARM64 / 32 位。
建议 16 GB 以上 U 盘，本机预留至少 12 GB 空间。程序会将文件配置到当前用户 LocalAppData/V7PortableClient，配置完成后可弹出 U 盘；不安装开机自启。
如果 DLL/运行库检查失败，启动器提供包内 Microsoft 签名的 VC++ 安装入口，需要客户另行确认，不会静默安装显卡驱动。

重要：现有 HIDMaestro 2.0.3 会安装自签名证书到本机 Root / TrustedPublisher 信任库和 UMDF 用户态驱动。不同意请不要配置。程序不会关闭安全软件、修改 Secure Boot、内存完整性、测试签名或绕过游戏拦截。
输入后台已连接，不代表 Windows 或游戏实际接受了移动。若游戏拒绝虚拟输入，本包不能保证解决。不要把“识别有框/有目标”当作游戏输入成功。
TensorRT 缓存只对匹配 GPU / 架构 / 版本使用，其他机器使用兼容识别路径，性能可能不同；不在首次配置中自动编译新模型。

本包基于最新 V7 普通版（包含切桌面死锁路径修补），没有擅自加入 V6 卡密系统。
不含开发者授权缓存、管理员密钥、个人日志、设备缓存或安装同意记录。客户产生的日志、同意记录和参数只留在该客户电脑，不回传。
本次只完成静态打包、完整性和隔离启动器测试，没有进行陌生客户电脑的游戏 / 输入驱动实测，不保证不封号或任何电脑即插即用。

来源与许可：Python、PyTorch、TensorRT、YOLOv5、HIDMaestro 等原组件许可保留在 payload 内。校验清单用于防止复制损坏，不是发行者数字签名或反逆向保护。
''','utf-8-sig')
    print('Hashing all deliverable files and checking privacy/path independence...',flush=True)
    files={}
    forbidden=('owner-only','license-cache','admin-key','consent.json','runtime-diagnostic','hid-launch.json','.backups','__pycache__','startup-console.log','host.log','python-path.txt')
    for p in sorted(STAGING.rglob('*')):
        if not p.is_file():continue
        name=p.relative_to(STAGING).as_posix()
        if any(t in name.casefold() for t in forbidden):raise ValueError('Private artifact: '+name)
        if p.suffix in ('.py','.cmd','.pth','.json','.txt') and p.stat().st_size<4*2**20:
            text=p.read_text('utf-8-sig',errors='ignore')
            if 'C:\\Users\\Colin\\Documents\\Codex' in text or 'C:/Users/Colin/Documents/Codex' in text:
                raise ValueError('Developer path leaked into '+name)
        if p.suffix=='.py' and (name.startswith('payload/app/') and '/vendor/' not in name or name in ('launcher.py','usb_support.py','payload/usb_support.py','payload/app_entry.py','payload/runtime_probe.py','payload/customer_admin.py')):
            ast.parse(p.read_text('utf-8-sig'),filename=name)
        files[name]=dict(bytes=p.stat().st_size,sha256=sha(p))
    critical=[n for n in files if n in ('launcher.py','usb_support.py','payload/customer_admin.py','payload/app_entry.py',
                 'payload/runtime_probe.py','payload/usb_support.py','payload/runtime/python.exe','payload/runtime/pythonw.exe',
                 'payload/runtime/python312.dll','payload/runtime/python312._pth','payload/app/weights/best.pt',
                 'payload/app/hid/app/HIDMaestro.Core.dll','payload/app/hid/app/VirtualKM.dll','payload/prerequisites/vc_redist.x64.exe')
              or n.startswith('payload/app/') and n.endswith('.py') and '/' not in n[len('payload/app/'):]]
    manifest=dict(schema=1,release_id=RELEASE,files=files,critical_files=critical,
                  driver_component_sha256=files['payload/app/hid/app/HIDMaestro.Core.dll']['sha256'],
                  local_payload_bytes=sum(e['bytes'] for n,e in files.items() if n.startswith('payload/')),
                  gpu_engine_cache_profile={k:engines[k] for k in ('gpu_name','compute_capability','tensorrt')},
                  game_input_verified=False,recognition_autostart=False,follow_autostart=False,
                  source_revision='desktop-freeze-20261107',offline_source_tests=241)
    js(STAGING/'MANIFEST.json',manifest)
    if any(sha(V7/n)!=h for n,h in baseline.items()):raise RuntimeError('Source changed during build')
    # Both resolved absolute paths are constrained immediately before directory move.
    assert STAGING.resolve().is_relative_to((HERE/'staging').resolve())
    assert OUTPUT.resolve().is_relative_to(DESKTOP.resolve()) and not OUTPUT.exists()
    os.rename(STAGING,OUTPUT)
    report=dict(output=str(OUTPUT),files=len(files),bytes=sum(e['bytes'] for e in files.values()),
                largest_file_bytes=max(e['bytes'] for e in files.values()),manifest_sha256=sha(OUTPUT/'MANIFEST.json'),
                revision=manifest['source_revision'],no_private_artifacts=True,original_sources_unchanged=True,
                runtime_native_model_or_hid_executed=False,customer_game_input_verified=False)
    js(HERE/'build-result.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()
