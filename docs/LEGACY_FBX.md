# 已弃用的旧角色流程与兼容入口

默认角色流程推荐 **MMDBridge Mode 1 → 角色双 FBX**。以下入口保留，用于既有配置和兼容任务。

## 旧 Blender / FBX（不烘焙物理，已弃用）

该流程曾在 **Blender 4.3** 测试，不支持自动物理烘焙，现标为已弃用。保留入口供既有配置和任务使用。默认主方案使用上游 MMDBridge 先烘焙 VMD，再由 Blender 处理 PMX 与烘焙动作并导出双 FBX；主方案以 Blender 5.2 为主介绍，当前发行包的版本检查见 [安装说明](INSTALL_WINDOWS.md#当前-blender-版本兼容说明)。

在“参数 → 角色导入通路”选择该项。“素材与软件”会显示 **旧 FBX：角色工作文件（可选）**，工具启动 Blender 时可打开指定 `.blend` 作为处理起点。留空使用新的 Blender 会话。

此流程不调用 MMD 进行物理烘焙，不能将实时刚体效果视为已经烘焙。选择 PMX/VMD 时应明确自己需要的动画输入；不满意的导出不要替代已验证的烘焙结果。

角色工作文件与“临时中转目录”是两个不同概念：工作文件是已有 Blender 内容；中转目录负责保存任务、恢复点和导出文件。原 `.blend` 不覆盖保存。

## MMD2Unreal（Legacy）

仅该通路需要匹配 UE 5.8 的 MMD2Unreal 和 KawaiiPhysics。选择该项后，缺失或未启用插件时 GUI 提供下载安装提示；也可使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_upstream_plugins.ps1 -Project "D:/YourProject/YourProject.uproject" -Unreal "C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor.exe"
```

先保存并关闭目标 UE 工程，安装后重新打开并连接。安装器使用登记的预编译版本，检查引擎 BuildId；不兼容时明确停止，不直接替换已有插件。

角色、动作与角色材质由上游导入窗口处理，需要确认其设置。它不是默认 MMDBridge 自动烘焙的替代入口。来源见 [依赖登记](upstream-lock.json) 和 [第三方组件](../THIRD_PARTY.md)。

## MMM 物理烘焙（已弃用）

暂时停止维护，现有独立页面和插件保留。操作见 [使用指南](USAGE.md#mmm-物理烘焙已弃用)。
