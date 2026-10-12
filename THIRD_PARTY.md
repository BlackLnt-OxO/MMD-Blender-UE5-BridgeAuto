# 第三方组件

- MMD Tools 4.5.13 — https://github.com/MMD-Blender/blender_mmd_tools/tree/v4.5.13 — GPL-3.0-or-later。完整上游源码与 LICENSE 位于 `vendor/blender_mmd_tools-4.5.13`。
- 下载归档 SHA-256：`e38a70bbf971adefaf92e32229abc60f8e19ebe6f5f161dc0efd92f210f673a6`。
- opencc-python-reimplemented 0.1.7 — Apache-2.0。安装副本及许可位于 `vendor/python`。
- PySide6 / Qt 6.8.3 — LGPL-3.0/GPL/commercial 多许可。使用动态链接，随发行目录保留 PySide6/Qt 组件；用户可以替换兼容动态库。
- PyInstaller 6.16.0 — GPL-2.0-or-later with bootloader exception；构建工具。
- pytest 8.4.2 — MIT；仅用于开发测试。

Blender、Unreal Engine 和用户的模型、动作、场景、贴图不随工具分发。用户素材仅用于本地验收。

## 应用图标

`assets/app-icon.png` 与 `.ico` 使用生成的初音未来形象插画，未复制 MMD 或 Unreal 的应用图标文件。初音未来（Hatsune Miku）角色权益属于 Crypton Future Media, INC.，角色形象的使用参见 [Piapro 角色使用说明](https://piapro.jp/license/character_guideline/)。本项目源码许可不授予第三方角色或商标的额外权利。


## 外部上游（不捆绑实现）

- [MMDBridge（rintrint/mmdbridge）](https://github.com/rintrint/mmdbridge)：默认角色流程使用的外部 MMD/VMD 烘焙程序，包含 `mmdbridge_scripts/mmdbridge_vmd_mode1.py`；从其 [Releases](https://github.com/rintrint/mmdbridge/releases) 下载配套程序。上游源码采用 MIT，分发内容以各自许可为准。本仓库不捆绑该程序；它与本项目的 UE `MMDBridge` 桥接插件不同。原始项目为 [uimac/mmdbridge](https://github.com/uimac/mmdbridge)。
- [MMD2Unreal](https://github.com/BlackLnt-OxO/MMD2Unreal)：仅 GUI 的“MMD2Unreal（Legacy）”角色通路使用，负责该通路的角色、动作、相机和角色材质。
- [KawaiiPhysics](https://github.com/pafuhana1213/KawaiiPhysics)：旧 MMD2Unreal 通路的物理依赖；不是默认 MMDBridge Mode 1 或 Blender 场景流程的必需项。
- [MikuMikuMoving（MMM）](https://sites.google.com/site/mikumikumovingeng/)：已弃用的可选 MMM 工具（暂停维护，现有功能保留）使用 **MikuMikuMoving version 1.2.9.2 — 03 Jun 2018 — 32bit/64bit** 下载条目中的 64 位版；默认角色流程不依赖它。MMM 软件不随本仓库分发，授权以官网为准。

选择旧 MMD2Unreal 通路时，用户需安装匹配 UE 5.8 的发行插件；源码克隆不是安装。这两项 UE 插件的来源与版本记录在 `docs/upstream-lock.json`，授权以各上游发布内容为准。外部 MMDBridge 和 MMM 由用户另行下载安装。
