# 转换流程与源码结构

## 数据流

```text
GUI / Settings
  └─ Controller → 独立任务目录、子进程、日志、检查点
      ├─ 角色：PMX/VMD → MMDBridge 物理烘焙 → Blender 4.3 → 双 FBX → UE
      ├─ 场景：Blender 5.2 最终求值结果 → 分层 USD、材质数据与贴图 → UE
      └─ 镜头：VMD → Blender 逐帧采样 → UE Level Sequence
```

UE 通过本地文件邮箱接收请求并返回状态；不依赖远程服务。CameraCutAuto、PhysicsBakeFix 和已弃用 MMM 工具独立于主导入流程。

## 模块

| 路径 | 职责 |
| --- | --- |
| `mmd_bridge/app.py` | Qt 界面、配置与操作入口 |
| `mmd_bridge/core.py` | Settings、任务格式、原子文件写入及资源根目录 |
| `mmd_bridge/controller.py` | 子进程、UE 连接、停止恢复、渲染和材质操作调度 |
| `mmd_bridge/mmd_avi.py` | 外部 MMDBridge 烘焙调度与结果验证 |
| `mmd_bridge/blender_*.py` | Blender 采样、几何与材质转换 |
| `mmd_bridge/ue_*.py` | UE 导入、材质生成、序列和渲染 |
| `mmd_bridge/material_*.py` | 材质分析、转换配方、计划及局部重建 |
| `mmd_bridge/fog_occlusion.py` 等 | 雾方案与运行设置 |
| `plugins/unreal/` | UE Python 桥接及 C++ 视口/几何读取插件 |
| `plugins/mmm/` | MMM 遥测插件源码，暂不维护 |
| `scripts/` | 桌面、DCC、安装、测试与打包入口 |
| `tests/` | GUI、调度、转换规则和适配器回归 |
| `vendor/` | 固定版本的 Python 源码依赖，保留上游许可 |

## 任务与结果

- `job.json`：输入、参数、版本与任务身份。
- `status.json`、`events.jsonl`：状态与步骤进度。
- `manifest.json`：角色／场景导出结果及材质来源。
- `ue_report.json`：实际 UE 资产、检查与失败项。
- `visual/`：参考帧、渲染回执和差分。
- `MaterialSurfaces/`：局部材质切换的事务记录及撤销数据。

恢复依据任务记录和输入哈希；局部材质切换依据实际组件、材质槽和转换来源，不能仅凭名称匹配。保留输入／输出身份和状态检查，避免恢复错误任务或覆盖手工修改。

## 材质与扩展

转换先分析最终有效节点输出，再选择直接节点、局部通道烘焙或明确限制。节点组和连接关系影响有效输入；未连接节点不应改变分类。表面类型与 Alpha 裁切分别处理，避免把叶片透明背景误当光学透射。

局部强制切换复用主转换管线，改变表面分支并只应用到勾选槽位。应用与撤销都验证实际绑定及覆盖状态；已有覆盖必须作为下一次操作的起点保留。

添加新节点或 GUI 控件时，同时检查 `Settings`、预设读取、Controller 参数、Blender／UE 适配器和报告字段。旧任务读取保留兼容，不自动改变已经保存的场景方案。

## 运行与验证

[安装说明](INSTALL_WINDOWS.md) 包含源码环境和 C++ 小插件编译步骤。GUI 入口为 `scripts/desktop_entry.py`。测试使用独立临时目录，不打开用户素材：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup.ps1 -Dev
.\.venv\Scripts\python.exe -m pytest -q
```

部分用例需要 Blender、USD 或真实 UE。Python／Qt 回归不能替代真实导入、材质与撤销、序列播放或渲染检查。新增适配先用小型夹具验证，再在目标软件中检查实际结果。
