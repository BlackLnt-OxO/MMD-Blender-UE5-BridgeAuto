# 使用文档

首次使用从 [快速上手](QUICK_START.md) 开始，其余文档按功能分类。

| 文档 | 内容 |
| --- | --- |
| [快速上手](QUICK_START.md) | 连接工程、填写素材、启动任务与查看结果 |
| [安装与启动](INSTALL_WINDOWS.md) | 依赖、上游程序目录、UE 插件、源码环境和打包 |
| [日常操作](USAGE.md) | 单独导入、配置、任务恢复、PhysicsBakeFix、CameraCutAuto、局部材质切换 |
| [当前状态与验证边界](PROJECT_STATUS.md) | 源码与发行包区别、当前默认和已验证范围 |
| [克隆与开发接手](DEVELOPMENT_HANDOFF.md) | 新电脑安装、验证、源码备份与任务资产的恢复边界 |
| [内置 PBR 与六档几何](CHARACTER_PBR.md) | 材质分类、参数、贴层、头发仅加权法向、旧排除档与缓存 |
| [UE 参数含义与调节指南](UE_PARAMETERS.md) | 材质实例、全部内置 PBR 配置参数、贴图通道、透明接线、灯光与输出 |
| [散射与柔和阴影](CHARACTER_PBR_SOFT_SHADOW.md) | 当前默认值、手动调整及独立实验区别 |
| [角色材质](CHARACTER_MATERIALS.md) | 内置PBR、官方卡通、ZZZ、自定义母材质和贴图映射 |
| [USD 场景转换](USD_SCENE_TUTORIAL.md) | 参考帧、几何实例、材质策略、碰撞和生成层 |
| [场景光影](SCENE_APPEARANCE.md) | 灯光、曝光、雾、OCIO 和参考图 |
| [显存与画质](MRQ_MEMORY.md) | Lumen 预设、雾精度和排查顺序 |
| [后台渲染](background_render.md) | 已保存 MRQ 预设、单帧检查、输出与恢复 |
| [已知问题](KNOWN_ISSUES.md) | 玻璃、体积、动画迁移和兼容限制 |
| [旧流程](LEGACY_FBX.md) | Blender / FBX、MMD2Unreal 和 MMM 的保留入口 |
| [源码结构](ARCHITECTURE.md) | 调度、DCC 适配器、任务数据和验证方法 |
| [仓库与文件位置](REPOSITORY.md) | 发行包内容及配置、任务、用户文件的位置 |
| [界面改版记录](gui_refresh_changes.md) | v0.2.0 布局、交互，以及移除的界面说明 |
| [SDF 实验说明](advanced/CHARACTER_SDF.md) | 默认关闭的面部方向图实验及参数约定 |

## 后续开发

先读根目录 [AGENTS.md](../AGENTS.md)，再按改动范围查 [源码结构](ARCHITECTURE.md)、[当前状态](PROJECT_STATUS.md) 和 [已知问题](KNOWN_ISSUES.md)。安装与测试命令见 [安装说明](INSTALL_WINDOWS.md#开发与打包)。

## 历史资料

[开发历史与资料索引](PROJECT_HISTORY.md) 解释已替代、撤回和一次性接续记录；[PBR实验归档](history/CHARACTER_PBR_EXPERIMENTS.md) 保留旧阶段说明和失败结果。原生证据在本地，不随源码分发。

公开 EXE 与当前源码的区别见 [源码与发行包](PROJECT_STATUS.md#源码与发行包)。现行教程的界面图来自 **v0.3.0**，包含左侧“角色设置”和补充动作多选；[截图条件与清单](images/gui-v0.3.0/README.md)。历史改版记录保留 v0.2.0 图片。截图中的素材路径为示例。返回 [项目首页](../README.md)。
