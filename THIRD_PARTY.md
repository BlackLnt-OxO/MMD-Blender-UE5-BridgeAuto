# 第三方组件

- MMD Tools 4.5.13 — https://github.com/MMD-Blender/blender_mmd_tools/tree/v4.5.13 — GPL-3.0-or-later。完整上游源码与 LICENSE 位于 `vendor/blender_mmd_tools-4.5.13`。
- 下载归档 SHA-256：`e38a70bbf971adefaf92e32229abc60f8e19ebe6f5f161dc0efd92f210f673a6`。
- opencc-python-reimplemented 0.1.7 — Apache-2.0。安装副本及许可位于 `vendor/python`。
- PySide6 / Qt 6.8.3 — LGPL-3.0/GPL/commercial 多许可。使用动态链接，随发行目录保留 PySide6/Qt 组件；用户可以替换兼容动态库。
- PyInstaller 6.16.0 — GPL-2.0-or-later with bootloader exception；构建工具。
- pytest 8.4.2 — MIT；仅用于开发测试。

Blender、Unreal Engine 和用户的模型、动作、场景、贴图不随工具分发。用户素材仅用于本地验收。


## 外部上游（不捆绑实现）

- MMD2Unreal：https://github.com/BlackLnt-OxO/MMD2Unreal ，角色、动作、相机、角色材质由它负责。
- KawaiiPhysics：https://github.com/pafuhana1213/KawaiiPhysics ，MMD2Unreal 必需物理依赖。

用户需从官方仓库安装匹配 UE 5.8 的发行插件；源码克隆不是安装。来源与版本记录在 docs/upstream-lock.json，授权以各上游发布内容为准。
