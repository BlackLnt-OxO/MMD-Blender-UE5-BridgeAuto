"""Tests for the VMD name DIRECT / ALIAS routing.

《VMD 骨骼名限制自动 Alias 改造方案》§21 列了必须覆盖的 8 个场景，
本文件逐条对应。核心断言是：**名称不可表示不再是 ERROR，而是路由条件**。
"""
from __future__ import annotations

import pytest

from mmd_bridge.vmd_name_strategy import (
    ALIAS_PREFIX,
    ALIAS_RETRY_PREFIX,
    MAX_BONE_NAME_BYTES,
    VmdNameStrategy,
    analyze_vmd_name_strategy,
    make_alias,
)


def strategy_of(result, name: str) -> VmdNameStrategy:
    for info in result.direct_bones + result.alias_bones:
        if info.original_name == name:
            return info.strategy
    raise AssertionError(f"{name!r} 不在结果里")


# --------------------------------------------------------------------------
# Test 1-5（方案 §21）
# --------------------------------------------------------------------------

def test_1_normal_short_japanese_name_is_direct():
    result = analyze_vmd_name_strategy([(0, "センター"), (1, "スカート_0_0")])
    assert result.strategy is VmdNameStrategy.DIRECT
    assert not result.uses_alias
    assert len(result.direct_bones) == 2


def test_2_ascii_too_long_requires_alias():
    """`SIDE BANGS A_0_1` 是 16 字节。

    即使截断到 15 字节不冲突，也**不**依赖截断 —— 那会把有损操作
    变成系统内部的名称协议（方案 §4）。
    """
    result = analyze_vmd_name_strategy([(0, "SIDE BANGS A_0_1")])
    assert result.strategy is VmdNameStrategy.ALIAS_REQUIRED
    assert strategy_of(result, "SIDE BANGS A_0_1") is VmdNameStrategy.ALIAS_REQUIRED
    assert result.alias_bones[0].alias_name == "PB000000"


def test_3_unencodable_chinese_requires_alias():
    """简体字「发」不在 CP932 里 —— 以前是 ERROR，现在是 ALIAS_REQUIRED。"""
    result = analyze_vmd_name_strategy([(251, "左发_0_1")])
    assert result.strategy is VmdNameStrategy.ALIAS_REQUIRED
    info = result.alias_bones[0]
    assert info.cp932_encodable is False
    assert info.alias_name == "PB000251"


def test_4_truncation_collision_gives_each_bone_a_distinct_alias():
    result = analyze_vmd_name_strategy([
        (247, "Bone_Bronya_C9_Hair_L_01"),
        (248, "Bone_Bronya_C9_Hair_L_02"),
    ])
    assert result.strategy is VmdNameStrategy.ALIAS_REQUIRED
    aliases = [i.alias_name for i in result.alias_bones]
    assert aliases == ["PB000247", "PB000248"]
    assert len(set(aliases)) == 2
    assert all(i.truncated_collision for i in result.alias_bones)


def test_5_multibyte_cut_requires_alias():
    """15 字节边界切在日文字符中间。"""
    result = analyze_vmd_name_strategy([(7, "あ" * 9)])       # 18 字节
    info = result.alias_bones[0]
    assert info.multibyte_cut_risk is True
    assert info.alias_name == "PB000007"


# --------------------------------------------------------------------------
# Test 7-8：Alias 生成规则（方案 §5）
# --------------------------------------------------------------------------

def test_7_ten_thousand_aliases_are_unique_and_vmd_safe():
    bones = [(i, f"Bone_{i}_VeryLongName_That_Exceeds_Limit") for i in range(10_000)]
    result = analyze_vmd_name_strategy(bones)

    aliases = [i.alias_name for i in result.alias_bones]
    assert len(aliases) == 10_000
    assert len(set(aliases)) == 10_000, "Alias 必须全模型唯一"
    for alias in aliases:
        assert alias.isascii()
        assert len(alias.encode("cp932")) <= MAX_BONE_NAME_BYTES


def test_8_alias_falls_back_when_model_already_has_that_name():
    """原 PMX 已有 `PB000247` 时，必须换一个确定性名字，而不是随机。"""
    result = analyze_vmd_name_strategy([
        (0, "PB000247"),                      # 模型自己就占用了这个别名
        (247, "左发_0_1"),                     # 需要 Alias
    ])
    alias = result.index_to_alias[247]
    assert alias != "PB000247"
    assert alias == f"{ALIAS_RETRY_PREFIX}000247"


def test_make_alias_is_deterministic():
    assert make_alias(247, set()) == make_alias(247, set()) == "PB000247"


def test_make_alias_raises_when_both_prefixes_taken():
    taken = {"PB000247", "PX000247"}
    with pytest.raises(ValueError, match="冲突"):
        make_alias(247, taken)


def test_alias_prefix_is_configurable():
    assert make_alias(5, set(), prefix="XX") == "XX000005"


# --------------------------------------------------------------------------
# 映射与统计
# --------------------------------------------------------------------------

def test_alias_map_and_index_map_are_both_kept():
    """必须同时留 alias→original 与 index→alias。

    Bone Index 是唯一不会因改名失效的身份标识；字符串反查只是便利。
    """
    result = analyze_vmd_name_strategy([(247, "左发_0_1")])
    assert result.alias_map == {"PB000247": "左发_0_1"}
    assert result.index_to_alias == {247: "PB000247"}


def test_final_name_prefers_alias():
    result = analyze_vmd_name_strategy([(0, "センター"), (251, "左发_0_1")])
    by_index = {i.bone_index: i for i in result.direct_bones + result.alias_bones}
    assert by_index[0].final_name == "センター"
    assert by_index[251].final_name == "PB000251"


def test_reason_counts_break_down_failures():
    result = analyze_vmd_name_strategy([
        (0, "左发_0_1"),                 # cp932 不可编码
        (1, "SIDE BANGS A_0_1"),         # 超长
    ])
    counts = result.reason_counts()
    assert counts["cp932_unrepresentable"] == 1
    assert counts["too_long"] == 1


def test_to_dict_is_json_serialisable():
    import json
    result = analyze_vmd_name_strategy([(247, "左发_0_1"), (0, "センター")])
    payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert payload["strategy"] == "alias_required"
    assert payload["direct"] == 1
    assert payload["aliased"] == 1
    assert payload["alias_map"]["PB000247"] == "左发_0_1"


def test_empty_input_is_direct():
    result = analyze_vmd_name_strategy([])
    assert result.strategy is VmdNameStrategy.DIRECT
    assert result.targets == 0


def test_never_raises_on_any_name():
    """方案 §2：不能因为名字不满足 VMD 字符串要求而拒绝模型。"""
    weird = [
        (0, ""),
        (1, "😀" * 5),
        (2, "\x00\x01\x02"),
        (3, "あ" * 100),
        (4, "A" * 1000),
    ]
    result = analyze_vmd_name_strategy(weird)
    assert result.strategy is VmdNameStrategy.ALIAS_REQUIRED
    assert len(result.alias_bones) == 5


# --------------------------------------------------------------------------
# 歧义：VMD 的名字槽是定长 15 字节 + NUL 补位，有些名字写进去读不出来
# --------------------------------------------------------------------------

def test_empty_name_requires_alias():
    """空名字写进 VMD 读出来还是空串 —— 无法与"没写过"区分。"""
    result = analyze_vmd_name_strategy([(0, "")])
    assert result.strategy is VmdNameStrategy.ALIAS_REQUIRED
    assert result.alias_bones[0].alias_name == "PB000000"


def test_name_with_embedded_nul_requires_alias():
    """VMD 用 NUL 补位，含 NUL 的名字会被截断在第一个 NUL 处，读出来是前半段。"""
    result = analyze_vmd_name_strategy([(3, "AB\x00CD")])
    assert result.strategy is VmdNameStrategy.ALIAS_REQUIRED
    assert result.alias_bones[0].alias_name == "PB000003"


def test_short_clean_name_is_still_direct():
    """别把正常的短名字也误判成需要 Alias。"""
    result = analyze_vmd_name_strategy([(0, "センター"), (1, "左足"), (2, "A")])
    assert result.strategy is VmdNameStrategy.DIRECT
    assert len(result.direct_bones) == 3
