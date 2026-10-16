# 旧流程与兼容入口

新任务建议使用 **MMDBridge Mode 1 → 角色双 FBX**。以下入口用于旧任务兼容，其额外依赖不参与默认流程。

## Blender / FBX

“参数 → 角色通路与兼容”可选择 **旧 Blender / FBX（不烘焙物理）**。该路线曾在 Blender 4.3 测试，直接处理 PMX／VMD，不调用外部 MMDBridge 烘焙物理，现已弃用。

选择后“素材与软件”显示 **旧 FBX：角色工作文件（可选）**，可指定已有 `.blend` 作为处理起点；留空使用新的 Blender 会话。原 `.blend` 不覆盖保存。

工作文件与中转目录不同：前者提供 Blender 内容，后者存任务、检查点和导出结果。实时刚体效果不能视为已烘焙的动画。

## MMD2Unreal

**MMD2Unreal（Legacy）**由上游导入窗口处理角色、动作和材质，使用前确认窗口设置。仅这条路线需要匹配 UE 的 MMD2Unreal 与 KawaiiPhysics。

GUI 在插件缺失或未启用时提供安装提示，也可从源码运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_upstream_plugins.ps1 -Project "D:/YourProject/YourProject.uproject" -Unreal "C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe"
```

先保存并关闭工程，安装后重开并连接。安装器使用 [依赖登记](upstream-lock.json) 的预编译包并核对 BuildId，不兼容时停止，不直接覆盖已有插件。

## MMM 物理烘焙

MMM 工具已弃用、暂不维护，原独立页面、隔离 Runtime 与遥测功能仍保留。默认角色流程不使用 MMM。

从 [MMM 官网](https://sites.google.com/site/mikumikumovingeng/) 的 **MikuMikuMoving version 1.2.9.2 — 03 Jun 2018 — 32bit/64bit** 条目取得 64 位版。在工具页面选择 MMM 目录，准备 Runtime，再安装遥测插件；实际烘焙仍在 MMM 手动完成，页面不会自动触发。

发行包带有遥测 DLL；源码构建见 [插件说明](../plugins/mmm/MMDBridgeMMMPlugin/README.md)。隔离副本避免修改原安装，卸载入口继续保留。

来源和许可见 [第三方组件](../THIRD_PARTY.md)，返回 [日常操作](USAGE.md)。
