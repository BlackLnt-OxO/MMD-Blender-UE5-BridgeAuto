"""VMD 骨骼名的 DIRECT / ALIAS 路由（方案《VMD 骨骼名限制自动 Alias 改造方案》）。

核心原则
--------
> VMD 的 15-byte / CP932 限制只是**中间传输格式**限制，
> 不应该升级成整个模型处理流程的**兼容性**限制。

因此"骨骼名写不进 VMD"不再是致命错误，而是一个**路由条件**：

.. code-block:: text

    DIRECT          → 原名可直接安全写入 VMD，走原流程
    ALIAS_REQUIRED  → 生成短 ASCII Alias，MMM 用 Alias PMX 烘焙，
                      VMD 里存 Alias，进入 Blender 后再恢复原名

为什么**连"安全截断"也不放行**
------------------------------
``SIDE BANGS A_0_1``（16 字节）截断成 ``SIDE BANGS A_0_`` 目前不冲突，
看起来可用。但它把**有损的字符串截断**变成了系统内部的名称协议 ——
一旦模型里出现两个截断后同名的骨，整个映射就会静默错位。
所以只要不能**逐字节无损**写入，一律走 Alias。

Alias 命名
----------
``PB`` + 6 位 bone_index（``PB000247``）。

* **deterministic** —— 同一模型每次生成结果相同；
* **可反查** —— 直接从名字读出 bone index，调试方便；
* **必然可表示** —— 纯 ASCII，8 字节，CP932 必然可编码。

不用随机 ID：随机值会让两次运行产生不同产物，破坏可复现性。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

__all__ = [
    "ALIAS_MAP_FILE_NAME",
    "ALIAS_PREFIX",
    "ALIAS_RETRY_PREFIX",
    "MAX_BONE_NAME_BYTES",
    "VMD_NAME_DIRECT",
    "VMD_NAME_ALIAS_REQUIRED",
    "BoneVmdNameInfo",
    "VmdNameStrategy",
    "VmdRepresentabilityResult",
    "analyze_vmd_name_strategy",
    "make_alias",
]

#: VMD 骨骼名槽位是 15 字节（定长，不足补 0）。
MAX_BONE_NAME_BYTES = 15

#: 默认 Alias 前缀。
ALIAS_PREFIX = "PB"

#: 与原模型已有骨骼名冲突时换用的前缀（方案 §5）。
ALIAS_RETRY_PREFIX = "PX"

#: Alias 映射文件名（方案 §6）。
ALIAS_MAP_FILE_NAME = "vmd_alias_map.json"

VMD_NAME_DIRECT = "VMD_NAME_DIRECT"
VMD_NAME_ALIAS_REQUIRED = "VMD_NAME_ALIAS_REQUIRED"


class VmdNameStrategy(Enum):
    """这个骨骼名能不能直接写进 VMD。"""

    DIRECT = "direct"
    ALIAS_REQUIRED = "alias_required"


@dataclass(frozen=True)
class BoneVmdNameInfo:
    """一根骨骼的名称可表示性分析结果。"""

    bone_index: int
    original_name: str
    strategy: VmdNameStrategy

    cp932_encodable: bool
    cp932_length: int | None

    #: 截断到 15 字节后是否与另一根骨同名。
    truncated_collision: bool = False

    #: 截断位置是否落在多字节字符中间（会产生乱码）。
    multibyte_cut_risk: bool = False

    alias_name: str | None = None

    @property
    def final_name(self) -> str:
        """写进 VMD 时实际使用的名字。"""
        return self.alias_name or self.original_name

    def to_dict(self) -> dict:
        return {
            "bone_index": self.bone_index,
            "original_name": self.original_name,
            "strategy": self.strategy.value,
            "cp932_encodable": self.cp932_encodable,
            "cp932_length": self.cp932_length,
            "truncated_collision": self.truncated_collision,
            "multibyte_cut_risk": self.multibyte_cut_risk,
            "alias": self.alias_name,
        }


@dataclass
class VmdRepresentabilityResult:
    """整个模型的名称策略。

    ``alias_map`` 是 ``alias → original_name``；
    ``index_to_alias`` 是 ``bone_index → alias``。

    **两者都要留。** 内部追踪应优先用 Bone Index ——
    它是唯一不会因改名而失效的身份标识；字符串反查只是便利。
    """

    strategy: VmdNameStrategy
    direct_bones: list[BoneVmdNameInfo] = field(default_factory=list)
    alias_bones: list[BoneVmdNameInfo] = field(default_factory=list)
    alias_map: dict[str, str] = field(default_factory=dict)
    index_to_alias: dict[int, str] = field(default_factory=dict)

    @property
    def uses_alias(self) -> bool:
        return bool(self.alias_bones)

    @property
    def targets(self) -> int:
        return len(self.direct_bones) + len(self.alias_bones)

    def reason_counts(self) -> dict[str, int]:
        """按失败原因分类计数，供日志与诊断用（方案 §19）。"""
        counts = {"cp932_unrepresentable": 0, "too_long": 0,
                  "truncation_collision": 0, "multibyte_cut": 0}
        for info in self.alias_bones:
            if not info.cp932_encodable:
                counts["cp932_unrepresentable"] += 1
                continue
            if info.truncated_collision:
                counts["truncation_collision"] += 1
            if info.multibyte_cut_risk:
                counts["multibyte_cut"] += 1
            if info.cp932_length and info.cp932_length > MAX_BONE_NAME_BYTES:
                counts["too_long"] += 1
        return {k: v for k, v in counts.items() if v}

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy.value,
            "targets": self.targets,
            "direct": len(self.direct_bones),
            "aliased": len(self.alias_bones),
            "reasons": self.reason_counts(),
            "alias_map": dict(self.alias_map),
        }


def make_alias(bone_index: int, taken: Iterable[str], *,
               prefix: str = ALIAS_PREFIX,
               retry_prefix: str = ALIAS_RETRY_PREFIX) -> str:
    """为某根骨生成确定性 Alias。

    先试 ``PB000247``；与原模型已有名字冲突时换 ``PX000247``。
    **绝不使用随机值** —— 两次运行必须得到相同产物。

    Raises:
        ValueError: 两种前缀都被占用（几乎不可能，除非模型已有同名骨）。
    """
    taken_set = set(taken)
    candidate = f"{prefix}{bone_index:06d}"
    if candidate not in taken_set:
        return candidate
    candidate = f"{retry_prefix}{bone_index:06d}"
    if candidate not in taken_set:
        return candidate
    raise ValueError(
        f"bone_index={bone_index} 的 Alias 与模型已有骨骼名冲突："
        f"{prefix}{bone_index:06d} / {retry_prefix}{bone_index:06d} 都被占用")


def _analyze_one(bone_index: int, name: str) -> BoneVmdNameInfo:
    """单根骨的名称可表示性（不含冲突判定，那需要全模型视野）。

    DIRECT 需要同时满足（方案 §4）：

    1. 可以 CP932 编码；
    2. 编码后 ≤ 15 字节；
    3. 编码结果**不会产生歧义**。

    第 3 条容易被忽略：VMD 的名字槽位是**定长 15 字节、用 NUL 补位**的，
    所以

    * **空名字**写进去读出来还是空串 —— 无法与"没写过"区分；
    * **含 NUL 的名字**会被截断在第一个 NUL 处，读出来是前半段 ——
      与另一根叫前半段的骨无法区分。

    两者都不可逆，一律走 Alias。
    """
    if not name or "\x00" in name:
        return BoneVmdNameInfo(
            bone_index=bone_index, original_name=name,
            strategy=VmdNameStrategy.ALIAS_REQUIRED,
            cp932_encodable=True,
            cp932_length=len(name.encode("cp932", errors="replace")))

    try:
        encoded = name.encode("cp932", errors="strict")
    except UnicodeEncodeError:
        return BoneVmdNameInfo(
            bone_index=bone_index, original_name=name,
            strategy=VmdNameStrategy.ALIAS_REQUIRED,
            cp932_encodable=False, cp932_length=None)

    over = len(encoded) > MAX_BONE_NAME_BYTES
    cut_risk = False
    if over:
        try:
            encoded[:MAX_BONE_NAME_BYTES].decode("cp932", errors="strict")
        except UnicodeDecodeError:
            cut_risk = True

    return BoneVmdNameInfo(
        bone_index=bone_index, original_name=name,
        # 超长本身也走 Alias —— 不依赖截断（方案 §4）。
        strategy=(VmdNameStrategy.ALIAS_REQUIRED if (over or cut_risk)
                  else VmdNameStrategy.DIRECT),
        cp932_encodable=True, cp932_length=len(encoded),
        multibyte_cut_risk=cut_risk)


def analyze_vmd_name_strategy(
    bones: Sequence[tuple[int, str]],
) -> VmdRepresentabilityResult:
    """决定每根骨走 DIRECT 还是 ALIAS。

    Args:
        bones: ``(bone_index, name)`` 序列 —— 只包含**需要经过
            MMM → Bake → VMD** 的骨骼（即方案 §4 说的那批目标）。

    Returns:
        :class:`VmdRepresentabilityResult`。**任何情况下都不抛异常** ——
        名称不可表示是路由条件，不是错误。
    """
    infos = [_analyze_one(index, name) for index, name in bones]

    # 截断冲突需要全模型视野：两根不同的骨截断到 15 字节后同名。
    by_truncated: dict[bytes, list[int]] = {}
    for info in infos:
        if not info.cp932_encodable:
            continue
        try:
            key = info.original_name.encode("cp932")[:MAX_BONE_NAME_BYTES]
        except UnicodeEncodeError:                 # pragma: no cover
            continue
        by_truncated.setdefault(key, []).append(info.bone_index)

    colliding = {index for group in by_truncated.values()
                 if len(group) > 1 for index in group}

    direct: list[BoneVmdNameInfo] = []
    alias: list[BoneVmdNameInfo] = []
    alias_map: dict[str, str] = {}
    index_to_alias: dict[int, str] = {}

    # Alias 不能和模型里**任何**已有骨骼名冲突 —— 包括那些走 DIRECT 的。
    taken = {name for _, name in bones}

    for info in infos:
        needs_alias = (info.strategy is VmdNameStrategy.ALIAS_REQUIRED
                       or info.bone_index in colliding)
        if not needs_alias:
            direct.append(info)
            continue

        alias_name = make_alias(info.bone_index, taken)
        taken.add(alias_name)
        resolved = BoneVmdNameInfo(
            bone_index=info.bone_index, original_name=info.original_name,
            strategy=VmdNameStrategy.ALIAS_REQUIRED,
            cp932_encodable=info.cp932_encodable,
            cp932_length=info.cp932_length,
            truncated_collision=info.bone_index in colliding,
            multibyte_cut_risk=info.multibyte_cut_risk,
            alias_name=alias_name)
        alias.append(resolved)
        alias_map[alias_name] = info.original_name
        index_to_alias[info.bone_index] = alias_name

    return VmdRepresentabilityResult(
        strategy=(VmdNameStrategy.ALIAS_REQUIRED if alias
                  else VmdNameStrategy.DIRECT),
        direct_bones=direct, alias_bones=alias,
        alias_map=alias_map, index_to_alias=index_to_alias)
