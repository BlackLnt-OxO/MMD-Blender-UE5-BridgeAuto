# 面部 SDF 实验说明

面部 SDF 为实验功能，**ZZZ_Render_PMX 默认关闭**（`sdf.format=none`）。

## 格式与数据

| 格式 | 数据与用途 |
| --- | --- |
| `threshold_lr` | 水平方向单图阈值；1024×1024 EXR，R 为正 yaw，G 为负 yaw，B 为面部覆盖，A=1 |
| `direction_atlas` | 水平及垂直共 114 方向；512 像素格组成 16×9 R16F 图集，约 72 MiB |
| `none` | 不生成或启用 SDF |

单图由水平每 22.5° 的 16 张几何遮罩合成，左右方向独立，不强制镜像 UV。未编码的 0/0.5/1 对应 0°/90°/180°；只表达进入阴影的阈值，不能完整保存“亮→暗→亮”。收据用 `non_monotonic_fraction` 和 `non_monotonic.png` 标记这类区域。

## 平滑、对比与插值

`smooth_sigma=2` 对阈值图做覆盖遮罩归一化高斯平滑，避免眼部 UV 空洞污染周围。`contrast=8` 使用单调 sigmoid 编码，0.25/0.75 约变为 0.018/0.982；材质以同一 SDF_Contrast 编码光照角度。中间灰度不再简单等于角度除以 180，0/0.5/1 标定仍保留。

`sdf.interpolation` 支持 `linear` 和默认 `pchip`，用相邻距离场零交叉合成阈值。PCHIP 为单调三次 Hermite 插值，不逐图 min/max 拉伸。实现依赖 Blender 内置 NumPy，不需要 SciPy 或外部制图程序。

改生成参数后要重新生成，不要只改材质里的 SDF_Contrast。保留的方向遮罩可以手动编辑，后续转换核对哈希再合成。

## 材质与边缘

实际绑定线性 FloatRGBA 半浮点 EXR，无 mipmap，单图约 8 MiB；`face_sdf_R.png` 只是预览。采样接入复制母材质的 Step、If 与明暗颜色分支，适配 UV 裁切和头部方向。

原 Shadow_Smooth 影响普通 N·L，原采样前 BlurIntensity 不等于阴影比较后的柔边。单图不再使用该模糊参数，改用 SDF_EdgePixels：默认 1.5，0 为硬截断，1–2 是窄过渡，4 更柔和。它根据阈值图屏幕导数工作，可直接改实例，无需重烘焙。

面部暗色由 `role_presets.face.vectors.ShadowColor` 决定，默认 RGB `[0.2,0.2,0.2]`、A=0；普通光照也用此颜色。贴图控制转光角度，不能靠拉黑贴图替代调暗部颜色。

## 鼻尖与面颊亮区

`sdf.nose_cutout` 在可识别的鼻尖侧面生成左右独立亮区，默认 end_angle=135。`sdf.cheek_triangle` 控制鼻旁圆角三角亮区，默认 end_angle=98；三角中心编码约 0.671。两者在主图平滑后合成，并输出 nose_cutout.png、cheek_triangle.png。

这些形状是程序的风格预设与几何启发式，不是所有脸型都适用。鼻部不突出、纹素不足、缺少可靠面部或 UV 重叠时会跳过或标记 needs_review。识别成功也需确认位置；可关闭对应区域或修改角度后重新生成。

## 运行方向约定

动态方向由 MMDBridgeCharacter 在骨骼变换后及编辑器更新时写入组件自定义数据索引 20–31；原索引 0 保留。图集配置需登记 `sdf.runtime.data_indices=[20,24,28]`。

当前 FBX 约定：Blender −Y 前方、+X 横轴、+Z 上方，对应 UE +X、−Y、+Z。图集从上到下为 +90° 到 −90° 俯仰；UV 保留 Blender 空间，采样端处理 UE 的 V 翻转。主材质使用 Atmosphere Sun Light Index 0。

旧图集配置通过 sdf_atlas→SDF_Atlas、sdf_coverage→SDF_Coverage 和 UseSDFAtlas 开关绑定。无头骨或面部选择不可靠时保留普通光照并报告复核；生成结果需检查面部转光效果。

返回 [角色材质](../CHARACTER_MATERIALS.md)。
