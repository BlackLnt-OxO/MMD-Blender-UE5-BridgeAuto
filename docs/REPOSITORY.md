# 仓库与 GitHub 备份范围

## 源码目录

| 路径 | 用途 |
| --- | --- |
| `mmd_bridge/` | GUI、调度、Blender/UE 转换与数据协议 |
| `plugins/blender/` | 本项目 Blender 插件源码 |
| `plugins/unreal/` | UE Python 桥接和本项目 C++ 视口插件源码 |
| `plugins/mmm/` | 本项目 MMM C# 插件源码；MMM 自身不包含在仓库 |
| `scripts/setup.ps1`、`start.cmd` | 安装桌面 Python 环境与启动 |
| `scripts/build_viewport_helper.py` | 针对安装的 UE 编译视口小插件 |
| `scripts/check_installation.py` | 检查克隆后的运行资源和 Python 导入 |
| `scripts/build.py`、`MMDBridge.spec` | 可选 Windows 发行目录打包 |
| `scripts/` 其他文件 | 生产子进程入口、诊断、回归和历史验收工具 |
| `tests/` | 单元回归、模拟软件接口、专用 Blender/USD 用例 |
| `docs/` | 安装、流程、已知问题和历史验证记录 |
| `vendor/`、`licenses/`、`THIRD_PARTY.md` | 固定版本的小型 Python 源码依赖及其许可，不是外部应用安装包 |

`scripts/` 中带具体场景名的历史验收工具可能仍记录开发机器路径，需要自备素材并调整参数，不属于首次启动步骤。`docs/` 的绝对路径是历史证据，不是主程序默认路径。GUI 不再提供开发者本机素材快捷按钮。

## 不提交的内容

`.venv`、`work`、`outputs`、`build`、`dist`、Python 缓存、C#/C++ 编译产物和本地环境文件由 `.gitignore` 排除。忽略不会删除本地文件。用户的 UE 工程、MMD/Blender 素材、外部软件和本机配置不加入源码仓库。

本机 GUI 配置默认位于 `%LOCALAPPDATA%\MMDBridge`；任务保存在用户选定的中转目录或桌面 `Middle_*` 目录。克隆仓库不会自动带上这些任务或配置。要恢复任务，还需要原任务目录、对应源素材和 UE 工程。

`vendor/blender_mmd_tools-4.5.13` 保留原上游源码、许可和其自身示例；这些不属于用户本机素材。当前仓库不需要 Git LFS。新资产或外部软件不要放入仓库后直接 `git add .`。

## 上传前检查

```powershell
git status --short
git diff --check
git ls-files
```

只提交源码、测试、文档和许可。GitHub 源码备份不等于整个工作电脑或 UE 工程的备份。首次克隆只需 README 和安装文档，不需要旧的 `work/` 验证产物。

本轮仅整理本地仓库；远程仓库创建、可见性、地址和上传由用户后续明确安排。
