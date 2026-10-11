# 源码迁移准备验证 · 2026-10-12

在当前 Windows 电脑上，用 Git 将会包含的文件生成独立源码副本，路径包含中文和空格。副本不带旧 `.venv`、本机配置、输出目录或原生 DLL；不是在另一台物理电脑上做的测试。

已验证：

- Python 3.12.14 新建虚拟环境，从 PyPI 安装 editable 项目和开发依赖，`pip check` 通过。
- `scripts/check_installation.py` 检查运行脚本、vendored Python 源码、插件源码和桌面导入通过。
- 使用独立 `MMDBRIDGE_HOME` 启动 GUI 并正常退出；检测到本机的 Blender 和 UE。没有复制旧配置或启动用户工程。
- 从仓库外的工作目录执行 `start.cmd --smoke-test` 成功，入口不依赖当前目录。
- 独立副本中 41 项相关回归通过，包括安装资源、编译失败保护、GUI、视口接口和 Controller；打包资源测试确认包含手动切换所需 `blender_surface_layer.py`，Source.zip 不含编译缓存。
- Blender 5.2.2 独立后台会话加载本副本的工具模块、固定 mmd_tools、OpenCC、NumPy 和 USD 成功；未打开用户 `.blend`。
- 用本机 UE 5.8.2 的 RunUAT 从源码构建 MMDBridgeViewport 成功，输出与引擎 BuildId `55116800` 一致；只编译本项目插件，没有重编 UE 或部署到用户工程。

原生编译首次使用深层仓库目录时，UBT 报生成路径超过 260 字符。最终脚本改用 `%TEMP%/MMDBridgeNative` 的独立目录，真实编译通过；失败时原有二进制文件保持不变。

证据保存在本机忽略目录 `work/github_ready_20261012`，不作为克隆后运行的依赖，也不上传。没有创建或修改 GitHub 远程，没有上传。

未重跑完整角色烘焙、UE 场景导入或高分辨率渲染，未重新生成完整 PyInstaller EXE。新电脑仍需用户自行安装文档列出的外部软件、编译工具和素材；Ray Traced 黑玻璃保持暂缓。
