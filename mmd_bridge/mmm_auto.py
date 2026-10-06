"""MMM 全自动物理烘焙的状态机（方案《MMM 全自动物理烘焙主流程整合任务》§5）。

设计要点
--------
**显式状态，而不是一串过程代码。**

.. code-block:: text

    STARTING → MMM_READY → MODEL_LOADING → MODEL_READY
             → MOTION_LOADING → MOTION_READY
             → LIPSYNC_LOADING → LIPSYNC_READY      （可选）
             → PHYSICS_READY → BAKING → BAKE_FINISHED
             → EXPORTING → EXPORTED → VALIDATING → SUCCESS

每一步都必须**执行动作 → 验证结果 → 才进入下一状态**。

**绝不用 sleep 当成功判据。** 方案 §5 明令禁止
``click(); sleep(3); click()`` —— 界面动作只是"发起"，
是否真的生效一律由 **Plugin telemetry** 判定（§6 / §7 / §11）。
对话框关闭 ≠ 加载成功：这是本项目最容易写出"假成功"的地方。

**Bake 完成必须组合两个信号**（§12）：
仅看 Marker 到终点不够 —— 可能出现"时间轴跑完但一根物理骨都没烤出关键帧"，
那必须 FAIL，不能继续导出。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import mmm_protocol as proto
from .mmm_ui import MmmUiDriver, MmmUiError

__all__ = [
    "MmmAutoError",
    "MmmAutoRunner",
    "MmmAutoState",
    "MmmBakeResult",
    "MmmStageTimeouts",
]


class MmmAutoState(Enum):
    """流程状态。失败时 ``FAILED`` 会带上最后成功的状态。"""

    STARTING = "STARTING"
    MMM_READY = "MMM_READY"
    MODEL_LOADING = "MODEL_LOADING"
    MODEL_READY = "MODEL_READY"
    MOTION_LOADING = "MOTION_LOADING"
    MOTION_READY = "MOTION_READY"
    LIPSYNC_LOADING = "LIPSYNC_LOADING"
    LIPSYNC_READY = "LIPSYNC_READY"
    PHYSICS_READY = "PHYSICS_READY"
    BAKING = "BAKING"
    BAKE_FINISHED = "BAKE_FINISHED"
    EXPORTING = "EXPORTING"
    EXPORTED = "EXPORTED"
    VALIDATING = "VALIDATING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class MmmAutoError(RuntimeError):
    """某个阶段失败。携带 ``state`` —— 失败发生在哪一步（方案 §19）。"""

    def __init__(self, state: MmmAutoState, message: str):
        super().__init__(f"{state.value}: {message}")
        self.state = state
        self.message = message


@dataclass
class MmmStageTimeouts:
    """分阶段超时（方案 §20）。

    ``bake_per_frame`` 让烘焙超时**随帧数放大** ——
    固定 60 秒对 6754 帧的动作远远不够。
    """

    mmm_ready: float = 120.0
    load_model: float = 180.0
    load_motion: float = 180.0
    load_lipsync: float = 180.0
    physics_ready: float = 30.0
    #: 烘焙基准秒数 + 每帧追加秒数。
    bake_base: float = 60.0
    bake_per_frame: float = 0.02
    export: float = 300.0
    #: 导出文件要"大小连续多少秒不变"才算写完。
    #: 可配置是为了让测试能用很短的窗口 —— 否则每个用例都要真等 2 秒。
    export_quiet: float = 2.0
    validate: float = 300.0

    def bake_seconds(self, end_frame: int) -> float:
        return self.bake_base + max(0, end_frame) * self.bake_per_frame


@dataclass
class MmmBakeResult:
    """MMM 阶段的产出（方案 §23）。"""

    ok: bool
    state: str
    error: str = ""
    model: str = ""
    baked_motion: str = ""
    alias_map: str = ""
    uses_alias: bool = False
    expected_end_frame: int = 0
    validation: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """可 JSON 序列化的结果。

        ``diagnostics`` 里会混进 ``Heartbeat`` 这类对象，
        统一走 :func:`_jsonable` 清洗 —— 否则"写失败诊断"这一步会自己崩，
        **把真正的失败原因吃掉**。
        """
        return {
            "ok": self.ok, "state": self.state, "error": self.error,
            "model": self.model, "baked_motion": self.baked_motion,
            "alias_map": self.alias_map, "uses_alias": self.uses_alias,
            "expected_end_frame": self.expected_end_frame,
            "validation": _jsonable(self.validation),
            "checks": _jsonable(self.checks),
            "diagnostics": _jsonable(self.diagnostics),
        }


class MmmAutoRunner:
    """把已存在的各模块串成无人值守的 MMM 阶段。

    它**不自己实现**任何底层能力 —— 只做编排与判定：
    UIA 交给 :class:`MmmUiDriver`，状态判定交给 telemetry。
    """

    def __init__(
        self,
        *,
        job_dir: str | Path,
        request: proto.JobRequest,
        expected_model: str = "",
        expected_bones: list[str] | None = None,
        expected_lipsync: str = "",
        expected_lipsync_morphs: list[str] | None = None,
        output_vmd: str | Path = "",
        ui: MmmUiDriver | None = None,
        timeouts: MmmStageTimeouts | None = None,
        poll: float = 0.5,
        on_state: Callable[[MmmAutoState, str], None] | None = None,
    ):
        self.job_dir = Path(job_dir)
        self.request = request
        self.expected_model = expected_model
        self.expected_bones = list(expected_bones or [])
        self.expected_lipsync = expected_lipsync
        self.expected_lipsync_morphs = list(expected_lipsync_morphs or [])
        self.output_vmd = Path(output_vmd) if output_vmd else (
            self.job_dir / "baked.vmd")
        self.ui = ui
        self.timeouts = timeouts or MmmStageTimeouts()
        self.poll = poll
        self._on_state = on_state

        self.state = MmmAutoState.STARTING
        self.checks: list[dict[str, Any]] = []
        self.diagnostics: dict[str, Any] = {}

    # -- 基础设施 ------------------------------------------------------

    def _enter(self, state: MmmAutoState, detail: str = "") -> None:
        self.state = state
        if self._on_state:
            self._on_state(state, detail)

    def _check(self, key: str, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"key": key, "name": name, "ok": bool(ok),
                            "detail": detail})

    def _ui(self) -> MmmUiDriver:
        if self.ui is None:
            self.ui = MmmUiDriver(timeout=self.timeouts.load_model,
                                  poll=self.poll)
        return self.ui

    def _snapshot(self, name: str) -> dict[str, Any]:
        return proto.read_snapshot(self.job_dir, name)

    def _state_json(self) -> dict[str, Any]:
        return proto.read_state(self.job_dir)

    def _wait_until(self, predicate: Callable[[], bool], *, timeout: float,
                    what: str, state: MmmAutoState) -> None:
        """轮询直到条件成立。

        **这里没有 sleep 当判据** —— 每个 ``predicate`` 都读 telemetry
        或真实文件，不是"等一会儿应该就好了"。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(self.poll)
        raise MmmAutoError(state, f"等待超时（{timeout:.0f}s）：{what}")

    # -- 各阶段 --------------------------------------------------------

    def _wait_mmm_ready(self) -> None:
        """等插件上线 —— 界面起来不等于插件能上报（§5）。

        **MMM 不会自动启用插件。** 它启动时只构造一个临时实例做探测
        然后 ``Dispose()``（用于填充插件列表），此时 ``Update()``
        永远不会被调用，也就没有任何 telemetry。

        实测：不点插件按钮，``heartbeat.json`` 永远不出现，
        流程会一直卡在「等待插件上线」。
        """
        self._enter(MmmAutoState.STARTING, "等待插件上线")

        # 先看插件是否**已经**在跑。若在跑就**绝不能点** ——
        # 那个按钮是开关，点一下会把已启用的插件关掉。
        already, _why = proto.heartbeat_is_current(
            proto.read_heartbeat(self.job_dir),
            job_id=self.request.job_id, nonce=self.request.nonce)
        if already:
            self._check("plugin_enable", "插件启用", True, "已自行上线，无需点击")
        else:
            self._enable_plugin()

        try:
            heartbeat = proto.wait_for_plugin(
                self.job_dir, job_id=self.request.job_id,
                nonce=self.request.nonce, timeout=self.timeouts.mmm_ready,
                poll=self.poll)
        except Exception as exc:                   # noqa: BLE001
            raise MmmAutoError(
                MmmAutoState.STARTING,
                f"插件未上线：{exc}") from exc
        # ``wait_for_plugin`` 返回的是 Heartbeat **对象**（不是 dict）——
        # 实测踩过：当成 dict 用 ``.get("pid")`` 会抛 AttributeError，
        # 而且那个错误会盖住真正的失败原因。
        self.diagnostics["heartbeat"] = _jsonable(heartbeat)
        self._check("plugin", "插件已上线", True,
                    f"pid={getattr(heartbeat, 'pid', '?')}")
        self._enter(MmmAutoState.MMM_READY, "插件已上线")

    def _enable_plugin(self) -> None:
        """点一下插件按钮把它启用。

        找不到按钮**不立即报错** —— 交给后面的 ``wait_for_plugin`` 超时，
        那样错误信息里会带上"等了多少秒"，比"找不到按钮"更有诊断价值。
        """
        try:
            self._ui().enable_plugin()
            self._check("plugin_enable", "插件启用", True, "已点击插件按钮")
        except MmmUiError as exc:
            self._check("plugin_enable", "插件启用", False, exc.message)
            self.diagnostics["plugin_enable_error"] = exc.message

    def _load_model(self) -> None:
        self._enter(MmmAutoState.MODEL_LOADING, self.expected_model)
        self._ui().load_model(self.expected_model)

        # 对话框关掉**不算**加载成功 —— 必须等 telemetry 报出骨骼数（§6）。
        def loaded() -> bool:
            snap = self._snapshot("model")
            return (int(snap.get("model_count", 0) or 0) > 0
                    and int(snap.get("bone_count", 0) or 0) > 0)

        self._wait_until(loaded, timeout=self.timeouts.load_model,
                         what="telemetry 报出模型骨骼", state=MmmAutoState.MODEL_LOADING)

        snap = self._snapshot("model")
        bone_count = int(snap.get("bone_count", 0) or 0)
        self._check("model", "模型已加载", True,
                    f"{snap.get('active_model_name', '')}，{bone_count} 根骨骼")

        # 进一步核对别名骨骼真的在 —— 否则后面 Bake 出来的东西对不上（§6）。
        if self.expected_bones:
            display = self._snapshot("display_frame")
            present = set(display.get("bones_in_physicsbake") or [])
            missing = [b for b in self.expected_bones[:200] if b not in present]
            self._check("model_bones", "期望骨骼已进入物理枠", not missing,
                        f"缺 {len(missing)} 根" + (f"：{missing[:3]}" if missing else ""))
        self._enter(MmmAutoState.MODEL_READY, f"{bone_count} 根骨骼")

    def _load_motion(self) -> None:
        self._enter(MmmAutoState.MOTION_LOADING, self.request.source_vmd)
        # MMM 的 Motion 以**当前 Marker** 为起始帧，所以不能假设它本来是 0（§7）。
        self._reset_marker()
        self._ui().load_motion(self.request.source_vmd)

        def loaded() -> bool:
            snap = self._snapshot("motion")
            return (int(snap.get("bones_with_frames", 0) or 0) > 0
                    and int(snap.get("max_frame", 0) or 0) > 0)

        self._wait_until(loaded, timeout=self.timeouts.load_motion,
                         what="telemetry 报出动作关键帧",
                         state=MmmAutoState.MOTION_LOADING)
        snap = self._snapshot("motion")
        self._check("motion", "主动作已加载", True,
                    f"{snap.get('bones_with_frames')} 根有帧，"
                    f"max_frame={snap.get('max_frame')}")
        self._enter(MmmAutoState.MOTION_READY,
                    f"max_frame={snap.get('max_frame')}")

    def _reset_marker(self) -> None:
        """把 Marker 归零。

        插件只**读**场景状态，不能写 Marker（SDK 无该 API），
        所以这里通过 UIA 把时间线回到 0 帧。失败不致命 —— 但要记录，
        因为它会影响 Motion 的起始帧（§7）。
        """
        try:
            self._ui().click(names=("从零帧播放", "最初から再生", "Play from 0"))
            self._check("marker", "Marker 已归零", True)
        except MmmUiError as exc:
            self._check("marker", "Marker 已归零", False, exc.message)

    def _load_lipsync(self) -> None:
        """表情 VMD。

        **导入方式与主动作完全一致**（用户确认）—— 同一个「导入动作」按钮。
        """
        if not self.expected_lipsync:
            self._check("lipsync", "表情 VMD", True, "未提供，跳过")
            return
        self._enter(MmmAutoState.LIPSYNC_LOADING, self.expected_lipsync)
        self._reset_marker()

        before = self._morph_key_count()
        self._ui().load_motion(self.expected_lipsync)

        def loaded() -> bool:
            return self._morph_key_count() > before

        self._wait_until(loaded, timeout=self.timeouts.load_lipsync,
                         what="telemetry 报出 morph 关键帧",
                         state=MmmAutoState.LIPSYNC_LOADING)
        after = self._morph_key_count()
        self._check("lipsync", "表情已加载", after > before,
                    f"morph 帧 {before} → {after}")
        self._enter(MmmAutoState.LIPSYNC_READY, f"morph 帧 {after}")

    def _morph_key_count(self) -> int:
        snap = self._snapshot("motion")
        return int(snap.get("morph_frames", 0) or 0)

    def _set_physics(self) -> None:
        self._enter(MmmAutoState.PHYSICS_READY, "设置物理为「总是计算」")
        self._ui().set_physics_always()
        self._check("physics", "物理模式已设置", True, "总是计算")
        # 动作全部就绪后才设置物理（§10），所以这里就是 PHYSICS_READY。

    def _pre_bake_check(self) -> None:
        """Bake 前的显式检查（§10）。全过才允许继续。"""
        problems: list[str] = []
        model = self._snapshot("model")
        if int(model.get("model_count", 0) or 0) <= 0:
            problems.append("没有活动模型")
        motion = self._snapshot("motion")
        if int(motion.get("bones_with_frames", 0) or 0) <= 0:
            problems.append("主动作没有关键帧")
        if self.expected_lipsync and self._morph_key_count() <= 0:
            problems.append("表情没有 morph 关键帧")
        if self.request.end_frame <= 0:
            problems.append("end_frame 无效")

        display = self._snapshot("display_frame")
        present = set(display.get("bones_in_physicsbake") or [])
        missing = [b for b in self.expected_bones if b not in present]
        if missing:
            problems.append(f"{len(missing)} 根期望骨骼不在物理枠")

        self._check("pre_bake", "Bake 前置检查", not problems,
                    "；".join(problems) if problems else "全部通过")
        if problems:
            raise MmmAutoError(MmmAutoState.PHYSICS_READY,
                               "Bake 前置检查未通过：" + "；".join(problems))

    def _bake(self) -> None:
        self._enter(MmmAutoState.BAKING, "触发物理烘焙")
        self._ui().start_record_physics()

        timeout = self.timeouts.bake_seconds(self.request.end_frame)
        deadline = time.time() + timeout
        started = False
        while time.time() < deadline:
            state = self._state_json()
            if state.get("bake_observed"):
                started = True
            if state.get("bake_completed_observed"):
                break
            if state.get("bake_stalled"):
                raise MmmAutoError(MmmAutoState.BAKING,
                                   "插件报告 bake_stalled —— 烘焙卡住")
            time.sleep(self.poll)
        else:
            raise MmmAutoError(
                MmmAutoState.BAKING,
                f"烘焙超时（{timeout:.0f}s，end_frame={self.request.end_frame}）"
                f"；started={started}")

        state = self._state_json()
        self.diagnostics["bake"] = state
        self._check("bake_observed", "观察到进入 PhysicsBaking",
                    bool(state.get("bake_observed")))
        self._check("bake_completed", "观察到烘焙完成",
                    bool(state.get("bake_completed_observed")))

        # §12：Marker 到终点**不等于**成功 —— 必须真的有物理骨关键帧。
        bake_snap = self._snapshot("bake")
        baked = bake_snap.get("baked_bones") or []
        expected_count = int(bake_snap.get("expected_count", 0) or 0)
        self._check("bake_keys", "物理骨确实产生了关键帧",
                    len(baked) > 0,
                    f"{len(baked)}/{expected_count} 根")
        if not baked:
            raise MmmAutoError(
                MmmAutoState.BAKING,
                "时间轴跑完但没有任何物理骨产生关键帧 —— 拒绝导出（§12）")
        self._enter(MmmAutoState.BAKE_FINISHED, f"{len(baked)} 根骨已烘焙")

    def _export(self) -> None:
        self._enter(MmmAutoState.EXPORTING, str(self.output_vmd))
        self.output_vmd.parent.mkdir(parents=True, exist_ok=True)
        if self.output_vmd.exists():
            self.output_vmd.unlink()
        self._ui().export_vmd(str(self.output_vmd))

        # §16：不要文件一出现就认为成功 —— 等它大小稳定。
        self._wait_until(
            lambda: _file_stable(self.output_vmd,
                                 quiet=self.timeouts.export_quiet),
            timeout=self.timeouts.export,
            what="导出文件出现且大小稳定",
            state=MmmAutoState.EXPORTING)
        size = self.output_vmd.stat().st_size
        self._check("export", "VMD 已导出", size > 0,
                    f"{self.output_vmd.name}（{size:,} 字节）")
        self._enter(MmmAutoState.EXPORTED, f"{size:,} 字节")

    # -- 主流程 --------------------------------------------------------

    def run(self) -> MmmBakeResult:
        """按状态机走完整个 MMM 阶段。

        **任何阶段失败都不抛异常** —— 返回带 ``state`` 与 ``error`` 的结果，
        让调用方决定怎么处理（方案 §19：不要只返回一句"失败了"）。
        """
        try:
            self._wait_mmm_ready()
            self._load_model()
            self._load_motion()
            self._load_lipsync()
            self._set_physics()
            self._pre_bake_check()
            self._bake()
            self._export()
            self._enter(MmmAutoState.VALIDATING, "运行 Validator")
        except MmmAutoError as exc:
            self._enter(MmmAutoState.FAILED, exc.message)
            return self._result(ok=False, error=exc.message,
                                failed_state=exc.state.value)
        except Exception as exc:                   # noqa: BLE001
            # **必须先记下失败发生在哪一步。**
            # 这里不能读 self.state —— _enter(FAILED) 会把它改掉，
            # 那样"失败在哪一阶段"就丢了，而那是诊断的关键信息（§19）。
            failed_state = self.state.value
            self._enter(MmmAutoState.FAILED, f"{type(exc).__name__}: {exc}")
            return self._result(ok=False,
                                error=f"{type(exc).__name__}: {exc}",
                                failed_state=failed_state)

        return self._result(ok=True)

    def _result(self, *, ok: bool, error: str = "",
                 failed_state: str = "") -> MmmBakeResult:
        return MmmBakeResult(
            ok=ok,
            state=failed_state or self.state.value,
            error=error,
            model=self.expected_model,
            baked_motion=str(self.output_vmd) if ok else "",
            alias_map=str(self.request.manifest_path),
            uses_alias=bool(self.expected_bones and self.expected_lipsync),
            expected_end_frame=self.request.end_frame,
            checks=list(self.checks),
            diagnostics=dict(self.diagnostics),
        )


def _jsonable(value: Any) -> Any:
    """把任意对象转成可 JSON 序列化的形式。

    诊断数据里会混进 ``Heartbeat`` 这类 dataclass。
    如果直接塞进 ``to_dict()``，写诊断文件时会抛
    ``TypeError: Object of type Heartbeat is not JSON serializable`` ——
    **而且是在"报告失败"的路径上崩**，把真正的错误信息整个吃掉。
    实测踩过，所以这里统一清洗一遍。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    for attr in ("to_dict", "isoformat"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return _jsonable(method())
            except Exception:                      # noqa: BLE001
                break
    return repr(value)


def _file_stable(path: Path, *, quiet: float = 2.0) -> bool:
    """文件存在、非空、且最近 ``quiet`` 秒内大小没变。"""
    try:
        info = path.stat()
    except OSError:
        return False
    if info.st_size <= 0:
        return False
    return (time.time() - info.st_mtime) >= quiet
