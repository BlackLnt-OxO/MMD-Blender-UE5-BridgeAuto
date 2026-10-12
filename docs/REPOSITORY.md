# 仓库内容

本仓库保存程序源码、插件源码、测试、使用文档和许可。软件与素材路径由 GUI 配置，用户的 Blender／UE 工程、模型、动作和贴图不随仓库分发。

`.venv`、`work`、`outputs`、`build`、`dist`、缓存、原生编译产物和本地环境文件由 `.gitignore` 排除。源码环境与运行说明见 [安装文档](INSTALL_WINDOWS.md)，模块说明见 [转换流程与源码结构](ARCHITECTURE.md)。

GUI 配置默认位于 `%LOCALAPPDATA%/MMDBridge`；转换任务在用户选定的中转目录或桌面 `Middle_*` 目录。克隆程序不会带上之前的任务。恢复任务还需要原任务目录、对应素材和 UE 工程。

`vendor/blender_mmd_tools-4.5.13` 保留上游源码、许可和其自身示例；它不是用户素材备份。`scripts/` 中部分专用测试脚本需要自备对应素材与路径，不属于普通启动步骤。

GitHub：[BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto](https://github.com/BlackLnt-OxO/MMD-Blender-UE5-BridgeAuto)。
