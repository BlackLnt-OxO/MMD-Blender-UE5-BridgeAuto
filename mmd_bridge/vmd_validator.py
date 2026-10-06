"""Validation of a physics-baked VMD, including Plan V2 range/name gates.

The MMM stage has exactly one deliverable: ``baked.vmd``.  Everything else in
that stage is a *means* to produce it, so this module is the only place where
"the MMM stage succeeded" is actually decided.  方案 §23 forbids treating "MMM
exited normally" as success, and §35 states the rule this module implements::

    UIA        只负责「要求 MMM 做某件事」
    MMM Plugin 只负责「确认 MMM 内部正在做什么」
    Validator  只负责「证明最终结果真的正确」

Layers
------
``A`` binary structure
    Header, section counts and CP932 name decoding.  Names are read strictly;
    a slot that VMD itself truncated mid-character is reported as a warning,
    anything else undecodable is an error.
``B`` source motion retained
    The source VMD's bone tracks must still be present — the common failure is
    an export setting that wrote only the ``PhysicsBake`` group.
``C`` expected bones present
    Every bone from the repair manifest must appear in ``baked.vmd``.
``D`` bake density
    Those bones must carry keyframes inside the bake range, otherwise the
    physics result never reached the file.
``E`` complete range (when a requested range is supplied)
``F`` processed PMX / bone-name-map consistency (when both are supplied)

Empty-expectation guard
-----------------------
Layer C is a subset assertion, and ``∅ ⊆ anything`` is always true.  A caller
that asks for the check while supplying no names is therefore **refused**
(``fail`` with ``VMD_EXPECTATION_EMPTY``) instead of being handed a silent pass.
Omitting the expectation entirely is a different case: those layers are marked
``skipped`` and the verdict becomes ``inconclusive``, because the run simply
proved less than a full certification would.

The same hazard exists inside the repair manifest and is refused by
:func:`mmd_bridge.pmx_physics_displayframe.build_repair_manifest`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .vmd_reader import (
    MAX_BONE_NAME_BYTES,
    VMD_CP932_DECODE_FAILED,
    VMD_BONE_NAME_UNREPRESENTABLE,
    VMD_INVALID,
    VMD_NAME_TRUNCATED,
    VMDDocument,
    VMDError,
    read_vmd,
)
from .pmx_reader import read_pmx

# --------------------------------------------------------------------------
# Error codes (方案 §25)
# --------------------------------------------------------------------------

VMD_SOURCE_TRACK_LOST = "VMD_SOURCE_TRACK_LOST"
VMD_EXPORT_MISSING_PHYSICS_BONES = "VMD_EXPORT_MISSING_PHYSICS_BONES"
VMD_BAKE_RANGE_INCOMPLETE = "VMD_BAKE_RANGE_INCOMPLETE"
PHYSICS_NOT_BAKED_TO_BONES = "PHYSICS_NOT_BAKED_TO_BONES"
REPAIR_MANIFEST_EMPTY = "REPAIR_MANIFEST_EMPTY"
VMD_EXPECTATION_EMPTY = "VMD_EXPECTATION_EMPTY"
BONE_NAME_MAP_MISMATCH = "BONE_NAME_MAP_MISMATCH"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"

#: Default minimum keyframes for a bone to count as baked.  A single keyframe
#: is not motion, so 2 is the floor for a meaningful bake.
DEFAULT_MIN_FRAMES_PER_BONE = 2

__all__ = [
    "BONE_NAME_MAP_MISMATCH",
    "DEFAULT_MIN_FRAMES_PER_BONE",
    "PHYSICS_NOT_BAKED_TO_BONES",
    "REPAIR_MANIFEST_EMPTY",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "STATUS_FAIL",
    "STATUS_PASS",
    "STATUS_SKIPPED",
    "VMD_BAKE_RANGE_INCOMPLETE",
    "VMD_EXPECTATION_EMPTY",
    "VMD_EXPORT_MISSING_PHYSICS_BONES",
    "IDENTITY_EPSILON",
    "VMD_SOURCE_TRACK_LOST",
    "LayerResult",
    "VMDValidationReport",
    "ValidationIssue",
    "check_bone_names_representable",
    "is_identity_pose",
    "track_carries_motion",
    "validate_baked_vmd",
    "write_validation_json",
]


# --------------------------------------------------------------------------
# Report structures
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = SEVERITY_ERROR
    layer: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "layer": self.layer,
            "message": self.message,
        }


@dataclass
class LayerResult:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.name,
            "status": self.status,
            "detail": self.detail,
            "data": self.data,
        }


@dataclass
class VMDValidationReport:
    baked_path: str = ""
    source_path: str = ""
    ok: bool = False
    #: ``pass`` / ``fail`` / ``inconclusive``.  ``inconclusive`` means the run
    #: could not verify something it was asked to verify — it must never be
    #: treated as success.
    status: str = STATUS_FAIL
    layers: list[LayerResult] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    baked_summary: dict[str, Any] = field(default_factory=dict)
    source_summary: dict[str, Any] = field(default_factory=dict)
    bake_range: tuple[int, int] = (0, 0)
    expected_bone_count: int = 0
    verified_bone_count: int = 0

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    def layer(self, name: str) -> LayerResult | None:
        for entry in self.layers:
            if entry.name == name:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "baked_path": self.baked_path,
            "source_path": self.source_path,
            "ok": self.ok,
            "status": self.status,
            "bake_range": list(self.bake_range),
            "expected_bone_count": self.expected_bone_count,
            "verified_bone_count": self.verified_bone_count,
            "layers": [layer.to_dict() for layer in self.layers],
            "issues": [issue.to_dict() for issue in self.issues],
            "baked_summary": self.baked_summary,
            "source_summary": self.source_summary,
        }

    def summary(self) -> str:
        lines = [
            "[VMD Validation]",
            f"  baked  : {self.baked_path}",
            f"  source : {self.source_path or '(未提供)'}",
            f"  status : {self.status.upper()}",
        ]
        if self.bake_range != (0, 0):
            lines.append(f"  range  : {self.bake_range[0]} .. {self.bake_range[1]}")
        lines.append(
            f"  bones  : 期望 {self.expected_bone_count} / 已验证 {self.verified_bone_count}")
        lines.append("")
        for layer in self.layers:
            lines.append(f"  [{layer.status.upper():8s}] {layer.name} — {layer.detail}")
        if self.issues:
            lines.append("")
            for issue in self.issues:
                lines.append(f"  {issue.severity.upper():7s} {issue.code}: {issue.message}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Pre-flight: can our own bone names survive a VMD round-trip?
# --------------------------------------------------------------------------

def check_bone_names_representable(names: Iterable[str]) -> list[ValidationIssue]:
    """方案 §10 — the names we are about to add must fit a VMD bone slot.

    Only the bones this project *adds* are checked.  Checking every physics bone
    in the model would block on pre-existing author data we do not control and
    must not rewrite; measured over 136 real PMX files that would flag 299
    unencodable and 2061 over-length names, while the bones actually needing
    repair were clean.

    Two failure modes are reported separately because they need different
    responses:

    * unencodable / over-length — the name cannot be written at all;
    * truncation collision — two distinct names share their first 15 bytes, so
      VMD would store them identically and the bake would be attributed to the
      wrong bone.
    """
    issues: list[ValidationIssue] = []
    lengths: dict[bytes, list[str]] = {}

    for name in names:
        try:
            encoded = name.encode("cp932", errors="strict")
        except UnicodeEncodeError:
            issues.append(ValidationIssue(
                code=VMD_BONE_NAME_UNREPRESENTABLE,
                message=f"骨骼名 {name!r} 无法写入 VMD：无法编码为 CP932",
                severity=SEVERITY_ERROR, layer="preflight"))
            continue

        if len(encoded) > MAX_BONE_NAME_BYTES:
            # 超长**不一定是错误**：VMD 会把名字截断到 15 字节。
            # 只有两种情况才是真的不能用：
            #   1) 截断切在多字节字符中间 → 残留半个字符，解码出来是乱码；
            #   2) 截断后与另一个名字相同 → VMD 无法区分，烘焙结果会张冠李戴。
            # 情况 2 由下面的冲突检查负责，这里只判情况 1。
            truncated = encoded[:MAX_BONE_NAME_BYTES]
            try:
                truncated.decode("cp932", errors="strict")
            except UnicodeDecodeError:
                issues.append(ValidationIssue(
                    code=VMD_BONE_NAME_UNREPRESENTABLE,
                    message=(f"骨骼名 {name!r} 编码后 {len(encoded)} 字节，"
                             f"截断到 {MAX_BONE_NAME_BYTES} 字节会切断多字节字符，"
                             f"VMD 中将成为乱码"),
                    severity=SEVERITY_ERROR, layer="preflight"))

        # Deliberately map the *truncated* form even for over-length names:
        # that is exactly how two distinct names end up sharing one slot.
        lengths.setdefault(encoded[:MAX_BONE_NAME_BYTES], []).append(name)

    for prefix, group in lengths.items():
        unique = sorted(set(group))
        if len(unique) > 1:
            issues.append(ValidationIssue(
                code=VMD_BONE_NAME_UNREPRESENTABLE,
                message=(f"骨骼名 {unique} 截断到 {MAX_BONE_NAME_BYTES} 字节后互相冲突"
                         f"（{prefix!r}），VMD 中将无法区分"),
                severity=SEVERITY_ERROR, layer="preflight"))

    return issues


# --------------------------------------------------------------------------
# Layers
# --------------------------------------------------------------------------

def _layer_a(document: VMDDocument) -> tuple[LayerResult, list[ValidationIssue]]:
    """Binary structure and CP932 decoding."""
    issues: list[ValidationIssue] = []

    for entry in document.decode_failures:
        issues.append(ValidationIssue(
            code=VMD_CP932_DECODE_FAILED, message=entry.detail,
            severity=SEVERITY_ERROR, layer="A"))

    for entry in document.truncated_names:
        issues.append(ValidationIssue(
            code=VMD_NAME_TRUNCATED, message=entry.detail,
            severity=SEVERITY_WARNING, layer="A"))

    if document.ik_error:
        issues.append(ValidationIssue(
            code=VMD_INVALID, message=f"IK 段未能解析：{document.ik_error}",
            severity=SEVERITY_WARNING, layer="A"))

    if document.trailing_bytes:
        issues.append(ValidationIssue(
            code=VMD_INVALID,
            message=f"文件末尾有 {document.trailing_bytes} 字节未归属任何区段",
            severity=SEVERITY_WARNING, layer="A"))

    errors = [i for i in issues if i.severity == SEVERITY_ERROR]
    status = STATUS_FAIL if errors else STATUS_PASS
    detail = (
        f"Header v{document.version}；Bone {len(document.bone_frames)} 帧 / "
        f"{len(document.bone_tracks)} 轨；Morph {len(document.morph_frames)} 帧；"
        f"Camera {len(document.camera_frames)}；Light {len(document.light_frames)}；"
        f"Shadow {len(document.shadow_frames)}；IK {len(document.ik_frames)}"
    )
    if errors:
        detail += f"；{len(errors)} 处名称无法解码"

    return LayerResult("A 二进制结构 / CP932", status, detail,
                       data=document.summary()), issues


#: 判定"单位姿态"的容差。位置用米级、旋转用四元数分量级，
#: 1e-6 足以区分"真的是 rest pose"与"任何实际动作"。
IDENTITY_EPSILON = 1e-6


def is_identity_pose(frame) -> bool:
    """这一帧是否等价于 rest pose（位置为 0、旋转为单位四元数）。"""
    if any(abs(value) > IDENTITY_EPSILON for value in frame.position):
        return False
    x, y, z, w = frame.rotation
    if (abs(x) > IDENTITY_EPSILON or abs(y) > IDENTITY_EPSILON
            or abs(z) > IDENTITY_EPSILON):
        return False
    # w 可以是 +1 或 -1：两者表示同一个旋转。
    return abs(abs(w) - 1.0) <= IDENTITY_EPSILON


def track_carries_motion(track) -> bool:
    """这条轨道里有没有**真的会改变画面**的关键帧。

    全单位姿态的轨道（哪怕有几千帧）等价于"没有这条轨道"：
    骨骼没有关键帧时保持 rest pose，正是单位姿态所描述的状态。
    """
    for frame in track.frames:
        if not is_identity_pose(frame):
            return True
    return False


def _layer_b(baked: VMDDocument, source: VMDDocument,
             allowed_bones: set[str] | None = None) -> tuple[LayerResult, list[ValidationIssue]]:
    """Source motion tracks must survive the bake.

    **只对"携带动画"的轨道做要求。**

    实测踩过的坑：`Prhythmatic` 源动作有 283 条骨骼轨道，MMM 导出后只剩 76 条，
    看起来丢了 207 条。但逐帧检查发现那 207 条**全是单位姿态** ——
    205 条只有第 0 帧的单位姿态，另 2 条（左右つま先）有 6755 帧但帧帧都是单位姿态。

    丢掉它们**不损失任何动画**，因此不该判失败。
    反过来，如果按轨道名硬比，一个完全正确的烘焙会被判 FAIL ——
    和"空集恒真"是同一类问题的镜像：**判据没有对准它真正要保证的东西**。
    """
    source_tracks = set(source.bone_names)
    ignored = len(source_tracks - allowed_bones) if allowed_bones is not None else 0
    if allowed_bones is not None:
        source_tracks &= allowed_bones
    baked_tracks = set(baked.bone_names)

    dropped = sorted(source_tracks - baked_tracks)
    meaningful = [name for name in dropped
                  if track_carries_motion(source.bone_tracks[name])]
    empty_dropped = len(dropped) - len(meaningful)

    issues: list[ValidationIssue] = []
    if meaningful:
        preview = ", ".join(repr(n) for n in meaningful[:8])
        if len(meaningful) > 8:
            preview += f" …（共 {len(meaningful)} 条）"
        issues.append(ValidationIssue(
            code=VMD_SOURCE_TRACK_LOST,
            message=(f"source 有 {len(meaningful)} 条**携带动画**的骨骼轨道"
                     f"未出现在 baked.vmd 中：{preview}"),
            severity=SEVERITY_ERROR, layer="B"))

    total_meaningful = sum(1 for name in source_tracks
                           if track_carries_motion(source.bone_tracks[name]))
    status = STATUS_FAIL if meaningful else STATUS_PASS
    detail = (f"source {len(source_tracks)} 轨（其中 {total_meaningful} 条携带动画）"
              f" → 全部保留")
    if empty_dropped:
        detail += f"；另 {empty_dropped} 条空轨道（仅单位姿态）未导出，无动画损失"
    if ignored:
        detail += f"；{ignored} 条不属于目标 PMX 的源轨道不参与比较"

    return LayerResult("B 原 Motion 保留", status, detail,
                       data={"source_tracks": len(source_tracks),
                             "meaningful_tracks": total_meaningful,
                             "empty_dropped": empty_dropped,
                             "ignored_non_model_tracks": ignored,
                             "lost": meaningful[:64]}), issues


def _layer_c(baked: VMDDocument,
             expected: Sequence[str]) -> tuple[LayerResult, list[ValidationIssue]]:
    """Every expected bone must appear in baked.vmd.

    An empty expectation cannot be verified and is reported as ``skipped``, not
    ``pass``: ``∅ ⊆ anything`` is always true, so calling it a pass would be a
    silent no-op dressed up as a check.
    """
    if not expected:
        return LayerResult(
            "C 期望骨骼存在", STATUS_SKIPPED,
            "未提供期望骨骼清单，本层未做任何验证",
            data={"expected": 0, "missing": []}), []

    baked_tracks = set(baked.bone_names)
    missing = sorted(set(expected) - baked_tracks)

    if missing:
        preview = ", ".join(repr(n) for n in missing[:8])
        if len(missing) > 8:
            preview += f" …（共 {len(missing)} 条）"
        layer = LayerResult(
            "C 期望骨骼存在", STATUS_FAIL,
            f"{len(missing)}/{len(set(expected))} 根期望骨骼不在 baked.vmd 中：{preview}",
            data={"expected": len(set(expected)), "missing": missing[:64]})
        return layer, [ValidationIssue(
            code=VMD_EXPORT_MISSING_PHYSICS_BONES, message=layer.detail,
            severity=SEVERITY_ERROR, layer="C")]

    return LayerResult(
        "C 期望骨骼存在", STATUS_PASS,
        f"{len(set(expected))}/{len(set(expected))} 根期望骨骼全部存在",
        data={"expected": len(set(expected)), "missing": []}), []


def _layer_d(baked: VMDDocument, expected: Sequence[str],
             bake_range: tuple[int, int],
             min_frames: int) -> tuple[LayerResult, list[ValidationIssue]]:
    """Expected bones must carry keyframes inside the bake range."""
    if not expected:
        return LayerResult(
            "D 烘焙帧密度", STATUS_SKIPPED,
            "未提供期望骨骼清单，本层未做任何验证",
            data={"min_frames": min_frames, "underfilled": []}), []

    start, end = bake_range
    underfilled: list[tuple[str, int]] = []
    total = 0
    for name in sorted(set(expected)):
        track = baked.bone_track(name)
        if track is None:
            underfilled.append((name, 0))
            continue
        count = len(track.frames_in_range(start, end))
        total += count
        if count < min_frames:
            underfilled.append((name, count))

    if underfilled:
        preview = ", ".join(f"{n!r}({c} 帧)" for n, c in underfilled[:8])
        if len(underfilled) > 8:
            preview += f" …（共 {len(underfilled)} 根）"
        layer = LayerResult(
            "D 烘焙帧密度", STATUS_FAIL,
            (f"{len(underfilled)}/{len(set(expected))} 根骨骼在 "
             f"{start}..{end} 区间内关键帧不足 {min_frames}：{preview}"),
            data={"min_frames": min_frames,
                  "underfilled": [[n, c] for n, c in underfilled[:64]],
                  "total_frames": total})
        return layer, [ValidationIssue(
            code=PHYSICS_NOT_BAKED_TO_BONES, message=layer.detail,
            severity=SEVERITY_ERROR, layer="D")]

    return LayerResult(
        "D 烘焙帧密度", STATUS_PASS,
        (f"{len(set(expected))} 根骨骼在 {start}..{end} 区间内均有 ≥{min_frames} 帧"
         f"（合计 {total} 帧）"),
        data={"min_frames": min_frames, "underfilled": [],
              "total_frames": total}), []


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def _expected_from_manifest(manifest: Mapping[str, Any]) -> list[str]:
    """Read the expectation list out of a repair manifest, refusing a vacuous one."""
    names = list(manifest.get("added_bone_names") or [])
    missing = int(manifest.get("missing_physics_bone_count") or 0)
    if missing > 0 and not names:
        raise ValueError(
            f"{REPAIR_MANIFEST_EMPTY}: 清单声称有 {missing} 根待修复物理骨，"
            "却未列出任何名称；下游的子集断言会因空集恒真而静默通过")
    return names


def validate_baked_vmd(
    baked_path: str | Path,
    *,
    source_path: str | Path | None = None,
    expected_bone_names: Sequence[str] | None = None,
    manifest: Mapping[str, Any] | None = None,
    bake_range: tuple[int, int] | None = None,
    min_frames_per_bone: int = DEFAULT_MIN_FRAMES_PER_BONE,
    model_path: str | Path | None = None,
    bone_name_map: str | Path | None = None,
) -> tuple[VMDValidationReport, str]:
    """Validate a baked VMD against layers A-D and optional V2 gates E/F.

    Args:
        baked_path: the file to certify.
        source_path: the motion that went into MMM, for layer B.
        expected_bone_names: bones that must be present (layer C/D).  Passing an
            empty sequence requests the check with nothing to verify, which is
            refused with ``VMD_EXPECTATION_EMPTY`` (``fail``) rather than being
            reported as a pass.  Passing ``None`` skips the layers, making the
            verdict ``inconclusive``.
        manifest: alternative to ``expected_bone_names``; a repair manifest from
            :func:`mmd_bridge.pmx_physics_displayframe.build_repair_manifest`.
        bake_range: ``(start, end)`` frames for layer D.  Defaults to the baked
            file's own bone frame range.
        min_frames_per_bone: keyframes required inside ``bake_range``.

    Returns:
        ``(report, human_readable_text)``.
    """
    report = VMDValidationReport(
        baked_path=str(baked_path),
        source_path=str(source_path) if source_path else "",
    )

    # -- parse ------------------------------------------------------------
    try:
        baked = read_vmd(baked_path)
    except (VMDError, OSError) as exc:
        code = getattr(exc, "code", VMD_INVALID)
        report.issues.append(ValidationIssue(
            code=code, message=f"无法解析 baked.vmd：{exc}",
            severity=SEVERITY_ERROR, layer="A"))
        report.layers.append(LayerResult(
            "A 二进制结构 / CP932", STATUS_FAIL, "文件无法解析"))
        report.status = STATUS_FAIL
        report.ok = False
        return report, report.summary()

    report.baked_summary = baked.summary()
    model_for_map = None
    if model_path is not None:
        try:
            model_for_map = read_pmx(model_path)
        except (OSError, ValueError):
            pass  # Layer F reports the precise PMX failure below.
    layer_a, issues_a = _layer_a(baked)
    report.layers.append(layer_a)
    report.issues.extend(issues_a)

    # -- expectation ------------------------------------------------------
    # Three distinct cases, and they must not be conflated:
    #   * expectation not requested  -> layers C/D skipped, verdict inconclusive
    #   * requested with an empty list -> refused (the check would be vacuous)
    #   * manifest claims missing bones but lists none -> refused outright
    expected: list[str] = []
    vacuous_request = False
    if manifest is not None:
        try:
            expected = _expected_from_manifest(manifest)
        except ValueError as exc:
            report.issues.append(ValidationIssue(
                code=REPAIR_MANIFEST_EMPTY, message=str(exc),
                severity=SEVERITY_ERROR, layer="C"))
            vacuous_request = True
    elif expected_bone_names is not None:
        expected = list(expected_bone_names)
        vacuous_request = not expected

    if vacuous_request:
        report.issues.append(ValidationIssue(
            code=VMD_EXPECTATION_EMPTY,
            message=("调用方要求校验期望骨骼，但清单为空：本层未验证任何内容，"
                     "因此不能判定为 PASS"),
            severity=SEVERITY_ERROR, layer="C"))

    report.expected_bone_count = len(set(expected))

    # -- layer B ----------------------------------------------------------
    if source_path:
        try:
            source = read_vmd(source_path)
        except (VMDError, OSError) as exc:
            code = getattr(exc, "code", VMD_INVALID)
            report.issues.append(ValidationIssue(
                code=code, message=f"无法解析 source.vmd：{exc}",
                severity=SEVERITY_ERROR, layer="B"))
            report.layers.append(LayerResult(
                "B 原 Motion 保留", STATUS_FAIL, "source 无法解析"))
        else:
            report.source_summary = source.summary()
            allowed = ({bone.name_jp for bone in model_for_map.bones}
                       if model_for_map is not None else None)
            layer_b, issues_b = _layer_b(baked, source, allowed)
            report.layers.append(layer_b)
            report.issues.extend(issues_b)
    else:
        report.layers.append(LayerResult(
            "B 原 Motion 保留", STATUS_SKIPPED, "未提供 source.vmd"))

    # -- layer C / D ------------------------------------------------------
    layer_c, issues_c = _layer_c(baked, expected)
    report.layers.append(layer_c)
    report.issues.extend(issues_c)

    range_requested = bake_range is not None
    if bake_range is None:
        bake_range = baked.bone_frame_range
    report.bake_range = (int(bake_range[0]), int(bake_range[1]))

    layer_d, issues_d = _layer_d(baked, expected, report.bake_range,
                                 min_frames_per_bone)
    report.layers.append(layer_d)
    report.issues.extend(issues_d)

    # V2: source range and index-based PMX name map are mandatory for a full
    # character certification, but remain optional for older validator callers.
    if range_requested:
        actual = baked.bone_frame_range
        covers = actual[0] <= report.bake_range[0] and actual[1] >= report.bake_range[1]
        detail = f"baked {actual[0]}..{actual[1]} / requested {report.bake_range[0]}..{report.bake_range[1]}"
        report.layers.append(LayerResult("E 完整帧范围", STATUS_PASS if covers else STATUS_FAIL, detail))
        if not covers:
            report.issues.append(ValidationIssue(VMD_BAKE_RANGE_INCOMPLETE, detail, layer="E"))

    if model_path is not None or bone_name_map is not None:
        mismatches = []
        if model_path is None or bone_name_map is None:
            mismatches.append("Processed PMX 和骨骼映射必须同时提供")
        else:
            try:
                model = model_for_map or read_pmx(model_path)
                entries = json.loads(Path(bone_name_map).read_text(encoding="utf-8"))
                if len(entries) != len(model.bones):
                    mismatches.append("骨骼映射条数与 PMX 不一致")
                fixed_names = set()
                mapped_indices = set()
                for entry in entries:
                    index, fixed = int(entry["bone_index"]), str(entry["fixed"])
                    if index in mapped_indices:
                        mismatches.append(f"Bone {index} 在映射中重复")
                    mapped_indices.add(index)
                    if index < 0 or index >= len(model.bones) or model.bones[index].name_jp != fixed:
                        mismatches.append(f"Bone {index} 的 PMX 名称与映射不一致")
                    if fixed in fixed_names or len(fixed.encode("cp932")) > MAX_BONE_NAME_BYTES:
                        mismatches.append(f"Bone {index} 的 VMD 名称冲突或超长: {fixed!r}")
                    fixed_names.add(fixed)
                if mapped_indices != set(range(len(model.bones))):
                    mismatches.append("骨骼映射未覆盖所有 PMX Bone Index")
                unknown = set(baked.bone_names) - fixed_names
                if unknown:
                    mismatches.append(f"baked 存在 PMX 之外的骨骼轨: {sorted(unknown)[:8]}")
            except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
                mismatches.append(f"骨骼映射校验无法完成: {exc}")
        detail = "PMX / bone_name_map / baked VMD 一致" if not mismatches else "; ".join(mismatches[:8])
        report.layers.append(LayerResult("F 骨骼名称映射", STATUS_PASS if not mismatches else STATUS_FAIL, detail))
        if mismatches:
            report.issues.append(ValidationIssue(BONE_NAME_MAP_MISMATCH, detail, layer="F"))

    # -- verdict ----------------------------------------------------------
    # A layer that never ran proves nothing, so it can never be "pass".  Only
    # the expectation layers count towards inconclusive: omitting source_path
    # is a legitimate narrower request, whereas omitting the expectation means
    # the physics result was never checked at all.
    failed = any(layer.status == STATUS_FAIL for layer in report.layers)
    unverified = layer_c.status == STATUS_SKIPPED

    if failed or report.errors:
        report.status = STATUS_FAIL
        report.ok = False
    elif unverified:
        report.status = "inconclusive"
        report.ok = False
    else:
        report.status = STATUS_PASS
        report.ok = True

    if layer_d.status == STATUS_PASS:
        report.verified_bone_count = len(set(expected))

    return report, report.summary()


def write_validation_json(report: VMDValidationReport,
                          path: str | Path) -> Path:
    """Persist a report as ``validation.json`` (方案 §6 / §22)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8")
    return target
