# 克隆与开发接手

本仓库保存完整开发源码、测试、固定资源、第三方源码和项目说明。公开仓库与个人备份仓库可推送同一分支和提交；修改远程地址不改变安装方法。Git 保存开发状态，Windows 发行包供直接运行，两者分别保管。

## 新电脑恢复

1. 克隆目标仓库，切换到需要接手的分支或提交。普通克隆即可，不需要拉取子模块；`vendor/blender_mmd_tools-4.5.13`、`vendor/python` 和 `resources` 已入库。
2. 安装 Windows x64 Python 3.12（也支持 3.11）、Git，然后在仓库根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -Dev
.\.venv\Scripts\python.exe scripts/check_installation.py
.\.venv\Scripts\python.exe -m pip check
New-Item -ItemType Directory -Force work/agent_checks | Out-Null
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp work/agent_checks/fresh_checkout_01
```

每次测试换一个未使用的临时目录名。首次安装需要联网获取 Python 依赖，不能直接复制另一电脑的 `.venv`。Python 未自动识别时给 `setup.ps1` 添加 `-PythonExe "C:\Tools\Python312\python.exe"`。

隔离日常配置后检查桌面启动：

```powershell
$env:MMDBRIDGE_HOME = Join-Path $PWD 'work/handoff_smoke_01'
.\.venv\Scripts\python.exe scripts/desktop_entry.py --smoke-test
Get-Content "$env:MMDBRIDGE_HOME/smoke.json"
Remove-Item Env:MMDBRIDGE_HOME
```

源码克隆没有原生插件二进制，启动检查中的原生插件资源可能为 `false`；这与 GUI 启动失败不同。运行真实任务前按 [安装说明](INSTALL_WINDOWS.md#ue-插件与-buildid) 为实际 UE 编译、安装插件，核对 BuildId。Blender、UE、上游 MMDBridge 和用户素材也须自行准备。

## 接手入口与当前范围

v0.4.0 收录六档几何、UE 材质配置复用、三层 Cel 和 Frame Rate Upgrade；最近五阶段提交与原生验收范围见 [更新说明](releases/v0.4.0.md) 和 [当前状态](PROJECT_STATUS.md)。新配置、已有任务和旧切分脚本分别保留其版本含义。安装到 UE 的原生插件还需核对实际二进制：相同 BuildId 不表示旧 DLL 已有新类型或接口，更新后保存并重启 UE。

先读根目录 [AGENTS.md](../AGENTS.md)，再读 [当前状态](PROJECT_STATUS.md)、[架构](ARCHITECTURE.md) 和 [已知问题](KNOWN_ISSUES.md)。当前功能、历史实验与本机证据分开记录，不能把旧报告当作新提交的验收结果。

2026-10-24 源码批次包括有序补充 VMD 与多角色整轨道合并、自选母材质与逐槽贴图映射、场景求值网格缺失材质槽修复、面部与所有皮肤默认粉嫩度 0.35，以及 16 位颜色 PNG 源编码修正。补充动作合并仍需结合实际素材完成本轮全流程验收；贴图编码已有 UE 导入、GPU 采样和保存重开证据，但不代表重新验证了完整角色场景。

修改前用 `git status`、`git log` 确认当前提交与本地改动。修改新配置时检查 GUI、保存载入、子任务、缓存、DCC 适配器和回执；旧任务继续按自己的版本解释。重新打包使用 `scripts/build.py`，详见 [开发与打包](INSTALL_WINDOWS.md#开发与打包)。发行包中的 `Source.zip` 也包含本说明和开发约定，但不含 Git 历史。

v0.3.0 以功能基线 `b2b5398` 为基础，包含新记录的 Alpha 接线 v1、工作区内独立的“角色设置”页，以及 [UE 参数指南](UE_PARAMETERS.md) 和 24 张原生例图。此前两个仓库已同步该功能基线，私有仓库实际克隆、资源检查和隔离 GUI 启动通过，检查复用现有 Python 依赖。旧公开 v0.2.0 与本地 `20261024-705a681` 包不含后两项功能。源码标签、应用版本、原生插件 BuildId 和任务处理版本分别核对，见 [源码与发行包](PROJECT_STATUS.md#源码与发行包)。

## Git 之外的恢复材料

克隆可恢复开发和测试环境，继续具体导入任务还需要原 PMX/VMD、贴图、Blender 文件、模拟缓存、任务中转目录及 UE 工程。它们以及本地原生验收图片、日志、`work`、`outputs`、个人配置均不属于源码备份，需另外备份并保持路径或明确重定位。

部分原生或本机夹具测试需要额外软件与素材；跳过须保留原因。Python 测试与 GUI 启动检查不能替代 Blender 导出、UE 导入、动画及画面验收。不要为让克隆测试通过而自动下载模型、修改日常配置或恢复已撤回实验。

推送前核对 `git remote -v`。公开仓库和备份仓库分别添加远程地址并推送需要的分支、标签；未推送的本地提交不在远程备份中。EXE、发行 ZIP、虚拟环境和构建缓存保留在 Git 外，发布二进制时另行上传发行附件。
