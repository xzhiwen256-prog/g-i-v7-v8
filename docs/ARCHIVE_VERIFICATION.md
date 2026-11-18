# 换电脑：下载完整性验收

## 在目标电脑核对

1. 用有访问权的 GitHub 账号打开本仓库。选中要验收的提交，再克隆或用 **Code → Download ZIP** 下载并完整解压。保留目录结构，不把新文件覆盖到旧副本。
2. 在解压后的项目根目录打开 PowerShell。审阅 [Verify-Archive.ps1](../Verify-Archive.ps1) 后，在系统策略允许的情况下运行：

   ```powershell
   .\Verify-Archive.ps1 -RequireCleanOutputs
   ```

3. 看到 `ARCHIVE OK` 才表示下载文件满足清单。当前清单包含 V7 **745** 个文件、V8 **735** 个文件，共 **1,480** 个；每项检查字节数及 SHA-256。参数 `-RequireCleanOutputs` 还会报告 `outputs` 里的额外文件，所以建议在配置或运行之前检查。

如果某项缺失或校验失败，先核对提交并重新下载到新目录，不要修改清单来使检查通过。若系统策略不允许执行 PowerShell 脚本，不必调整系统保护；可以查看下面的远端检查结果。

## 在 GitHub 上核对

有仓库写权限的维护者可在 **Actions → Verify download on Windows → Run workflow** 手动触发。GitHub 托管的 Windows x64 运行器会全新检出所选提交并执行同一检查，不借用本机的 Python、缓存或项目目录。公开分享检查结果前，注意私有仓库链接仍需访问权限。

## 这项检查不证明什么

- `FILE_MANIFEST.json` 覆盖 `outputs/V7`、`outputs/V8` 的归档文件，不覆盖根目录或后来新增的文档；下载时应以可信仓库的指定提交为准。
- 不导入项目、不加载模型、不安装任何运行库或驱动，不连接设备、不采集屏幕，也不启动游戏。
- 哈希一致证明的是 **归档文件完整**，不是目标电脑的 Python/GPU/驱动兼容或项目可运行。归档未捆绑 Python 虚拟环境。运行环境的条件和已知限制见[配置指南](RUNTIME_SETUP.md)，本检查不替代对应的运行验收。
