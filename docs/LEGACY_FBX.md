# 历史一阶段说明（仅用于旧 FBX 任务）

以下为迁移前文档，不能用作新流程说明。

# MMD Bridge

Windows 桌面工具、Blender 适配器和 Unreal 编辑器桥接。角色、Morph、动作与 MMD 镜头保留 FBX/Sequencer；Blender 场景默认通过分层 USD Stage 传递。JSON 清单保存稳定的骨骼、Morph、材质、原型和实例身份。

## 启动

打包后双击 `dist/MMDBridge/MMDBridge.exe`。整个 `MMDBridge` 文件夹必须一起移动，不能只复制 EXE。

开发启动：

```powershell
.\.venv\Scripts\python.exe scripts\desktop_entry.py
```

1. 在“素材与软件”选择 Blender 和 UE。自动扫描常规安装、Steam 多个库和 Epic 安装清单；便携版可手动添加。
2. 选择 PMX、动作 VMD；可附加口型、镜头和 `.blend` 场景。使用“素材预检”检查 VMD 轨道类型。
3. 在“参数”设置 MMD 比例、帧范围、画幅及偏移。默认 0.08、30 FPS，结束帧 0 表示自动检测。
4. 点击“开始流程”。Blender 会在新会话处理源文件和导出副本。
5. 原生 FBX 窗口上出现两按钮模态选择。60 秒全局无输入使用默认配置；点击“取消”关闭本批次倒计时，逐项手动导出。
6. 导出完成后选择或新建 UE 项目。在“任务与日志”安装项目桥接插件，保存并重启 UE，再连接该项目。
7. 完成后打开任务目录内 `ue_report.json`，检查未匹配骨骼、材质近似和生成资源路径。

角色材质槽必须完整保留：导入后逐一校验原始槽身份、数量和分配关系，丢槽、合并或重复均报错，不会以单个材质槽作为成功结果。“平整轨道 / Flatten Tracks”是 Alembic Geometry Cache 选项，当前 FBX 通路没有该开关；未来接入 Alembic 时要求显式关闭该项。

已有 Blender 文件可以填入“角色工作文件”，导出放在其同目录；留空则放在 Windows 实际桌面。场景转换单独输出到场景文件旁的批次目录。源 PMX、VMD、`.blend` 不会被覆盖保存。

## 版本

Blender 执行适配器目前针对 4.3.x。UE 适配器针对 5.7.x。已发现但未适配的其他次版本会列出供选择，执行前明确阻止不支持的组合。

开发开始时本机 UE 为 5.7.2，期间更新为 5.7.4；验收报告记录实际版本，不以安装目录名称判断。

## 输出与恢复

- 角色：`*_Model.fbx`、`*_Animated.fbx`、可选 `*_Camera.fbx`，以及 `*_Data` 中的贴图、清单、骨骼参考采样和镜头数据。
- 场景：`Scene.usda` 用户根层、工具几何/材质层、分块 `.usdc` Payload、贴图和 `package.json`。
- UE：目标内容目录下 `A_<稳定模型ID>` 中的 Character、Materials、Textures、Sequences、Maps、Scene；原始 USD 包位于项目 `MMDBridgeData/A_<ID>/Scene`。
- 任务：默认 `%LOCALAPPDATA%/MMDBridge/jobs`。可用 `MMDBRIDGE_HOME` 指定位置。
- “停止”请求在安全步骤边界生效；不强制结束正在编辑的 Blender/UE。完成的结果保留。
- 完整角色导出后可“恢复已有任务”。角色导出尚未完成时，回到原 Blender 窗口继续；原窗口已关闭则创建新任务。
- 输入内容哈希变化后必须创建新任务。导入器拒绝覆盖目标命名空间中的非工具资产。
- UE 插件安装前备份 `.uproject` 为 `.uproject.mmdb-backup`，保留已有插件和项目配置。

## 当前范围

支持 PMX、骨骼动作、独立口型合并、顶点 Morph、透视 VMD 摄像机，以及基于 Blender 最终依赖图的 USD 静态场景。场景支持 Geometry Nodes/实例原型、Payload、常见 PBR 节点、混合通道烘焙、玻璃和逐三角形碰撞。

骨骼/顶点动画采样属于动画传递，不启用 MMD 刚体或 UE Physics Asset。场景 Ocean 输出参考帧形状，波纹在 UE 中近似重建；不迁移真实 Ocean 几何缓存。

角色使用基础贴图材质显示；PMX Toon/Sphere、材质/UV/组合 Morph 和用户母材质分配留有来源记录与警告。复杂着色不保证跨渲染器逐像素一致。透视镜头使用 CineCamera，含正交切换时使用普通 Camera 并写入投影轨道。常见位移高度转为法线烘焙，不恢复轮廓位移。

第二阶段第一部分已把场景通路升级为 schema v2 USD；使用方法见 [通用 USD 场景教程](docs/USD_SCENE_TUTORIAL.md)。`M_00` / `MF_PSR` 可作为场景通用母材质参考：简单贴图 PBR 完整匹配实际参数时创建子实例，但不修改母材质。角色材质仍属于后续独立阶段。

## 开发与验收

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build.py
```

纯 Python/Qt 测试验证轨道分类、损坏文件、参数、Steam 清单、输入变更、插件安装和界面锁定。真实 DCC 验收在 `work/acceptance` 内的独立 UE 工程进行；`scripts/validate_blender_fbx.py` 回读 FBX 并比较骨骼位置。真实运行结果与限制见 `docs/VALIDATION.md`。

源码采用 GPL-3.0-or-later，与捆绑 mmd_tools 的许可保持一致。依赖版本和来源见 `THIRD_PARTY.md`。

发行包内附 `Source.zip`，包括源码、两端插件、固定版本依赖和构建脚本。解压后可用 Python 3.11 创建虚拟环境，执行 `pip install -e ".[dev]"`，再执行构建脚本。UE 插件请从桌面界面安装，以生成与该项目绑定的连接配置。
