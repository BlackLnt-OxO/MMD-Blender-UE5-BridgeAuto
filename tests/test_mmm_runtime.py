"""Unit tests for MMM discovery, runtime isolation and plugin install/uninstall.

Most cases run against a **synthetic** MMM tree (empty files), because the
version check is the only part that needs a real PE resource.  The synthetic
tree has no version info, so ``validate_mmm_dir`` accepts it — which is exactly
the intended behaviour for a user who manually points at a directory.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mmd_bridge.mmm_runtime import (
    BASELINE_FILES,
    MMM_NOT_FOUND,
    MMM_SDK_MISSING,
    MMM_VERSION_SUPPORTED,
    MMM_VERSION_UNSUPPORTED,
    PLUGIN_DLL_NAME,
    MmmRuntimeError,
    baseline_dir,
    discover_mmm,
    install_plugin,
    plugin_status,
    prepare_runtime,
    read_pe_version,
    restore_baseline,
    runtime_dir,
    runtime_status,
    uninstall_plugin,
    validate_mmm_dir,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_mmm(root: Path, *, exe: str = "MikuMikuMoving64.exe",
             sdk: bool = True, plugins: bool = True) -> Path:
    """Build a minimal MMM-shaped directory tree."""
    root.mkdir(parents=True, exist_ok=True)
    (root / exe).write_bytes(b"MZ")
    if sdk:
        sdk_dir = root / "System" / "x64"
        sdk_dir.mkdir(parents=True, exist_ok=True)
        (sdk_dir / "MikuMikuPlugin.dll").write_bytes(b"MZ")
    if plugins:
        (root / "Plugins").mkdir(parents=True, exist_ok=True)
    return root


def make_dll(path: Path, size: int = 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\x00" * size)
    return path


@pytest.fixture(autouse=True)
def isolated_localappdata(tmp_path, monkeypatch):
    """Keep every runtime path inside tmp_path."""
    local = tmp_path / "LocalAppData"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    return local


# --------------------------------------------------------------------------
# validate_mmm_dir
# --------------------------------------------------------------------------

def test_validate_accepts_synthetic_tree(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    install = validate_mmm_dir(root)
    assert install.directory == root
    assert install.executable.name == "MikuMikuMoving64.exe"
    assert install.plugin_dir == root / "Plugins"


def test_validate_rejects_missing_directory(tmp_path):
    with pytest.raises(MmmRuntimeError) as excinfo:
        validate_mmm_dir(tmp_path / "nope")
    assert excinfo.value.code == MMM_NOT_FOUND


def test_validate_rejects_directory_without_executable(tmp_path):
    root = tmp_path / "not-mmm"
    root.mkdir()
    (root / "readme.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(MmmRuntimeError) as excinfo:
        validate_mmm_dir(root)
    assert excinfo.value.code == MMM_NOT_FOUND
    assert "MikuMikuMoving64.exe" in excinfo.value.message


def test_validate_rejects_missing_sdk(tmp_path):
    root = make_mmm(tmp_path / "MMM", sdk=False)
    with pytest.raises(MmmRuntimeError) as excinfo:
        validate_mmm_dir(root)
    assert excinfo.value.code == MMM_SDK_MISSING


def test_validate_accepts_32bit_executable_name(tmp_path):
    root = make_mmm(tmp_path / "MMM", exe="MikuMikuMoving.exe")
    assert validate_mmm_dir(root).executable.name == "MikuMikuMoving.exe"


def test_validate_unsupported_version_when_required(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    with pytest.raises(MmmRuntimeError) as excinfo:
        validate_mmm_dir(root, require_supported=True)
    assert excinfo.value.code == MMM_VERSION_UNSUPPORTED


def test_validate_records_source(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    assert validate_mmm_dir(root, source="自动检测").source == "自动检测"


# --------------------------------------------------------------------------
# PE version reading
# --------------------------------------------------------------------------

def test_read_pe_version_on_synthetic_file_returns_empty(tmp_path):
    """No version resource -> empty dict, never an exception."""
    exe = tmp_path / "fake.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 64)
    assert read_pe_version(exe) == {}


def test_read_pe_version_on_missing_file_returns_empty(tmp_path):
    assert read_pe_version(tmp_path / "nope.exe") == {}


@pytest.mark.skipif(os.name != "nt", reason="PE 资源只在 Windows 上可读")
def test_read_pe_version_reads_a_real_windows_binary():
    """notepad.exe is guaranteed present and carries a version resource."""
    info = read_pe_version(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                           / "System32" / "notepad.exe")
    assert info.get("FileVersion") or info.get("ProductName")


# --------------------------------------------------------------------------
# Runtime isolation
# --------------------------------------------------------------------------

def test_runtime_paths_follow_the_documented_layout(tmp_path):
    assert runtime_dir() == (tmp_path / "LocalAppData" / "MMDBridge"
                             / "mmm_runtime" / MMM_VERSION_SUPPORTED / "runtime")
    assert baseline_dir().name == "baseline"


def test_runtime_status_before_prepare():
    status = runtime_status()
    assert status["prepared"] is False
    assert status["executable"] == ""


def test_prepare_runtime_copies_and_creates_baseline(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    for name in BASELINE_FILES:
        (root / name).write_text(f"<{name}/>", encoding="utf-8")

    target = prepare_runtime(root)

    assert (target / "MikuMikuMoving64.exe").is_file()
    assert (target / "System" / "x64" / "MikuMikuPlugin.dll").is_file()
    for name in BASELINE_FILES:
        assert (baseline_dir() / name).is_file()


def test_prepare_runtime_does_not_modify_source(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    (root / "Settings.xml").write_text("original", encoding="utf-8")
    before = sorted(p.name for p in root.iterdir())

    prepare_runtime(root)

    assert (root / "Settings.xml").read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in root.iterdir()) == before


def test_prepare_runtime_reuses_existing_copy(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    first = prepare_runtime(root)
    marker = first / "user-added-after-copy.txt"
    marker.write_text("keep me", encoding="utf-8")

    second = prepare_runtime(root)          # 不传 force

    assert second == first
    assert marker.is_file(), "复用时不应重新复制"


def test_prepare_runtime_force_recopies(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    first = prepare_runtime(root)
    marker = first / "user-added-after-copy.txt"
    marker.write_text("keep me", encoding="utf-8")

    prepare_runtime(root, force=True)

    assert not marker.exists(), "force=True 时应重新复制"


def test_runtime_status_after_prepare(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    prepare_runtime(root)
    status = runtime_status()
    assert status["prepared"] is True
    assert Path(status["executable"]).is_file()


def test_prepare_runtime_rejects_invalid_source(tmp_path):
    with pytest.raises(MmmRuntimeError):
        prepare_runtime(tmp_path / "nope")


def test_restore_baseline_writes_files_back(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    for name in BASELINE_FILES:
        (root / name).write_text("baseline", encoding="utf-8")
    target = prepare_runtime(root)

    (target / BASELINE_FILES[0]).write_text("changed by MMM", encoding="utf-8")
    restored = restore_baseline()

    assert BASELINE_FILES[0] in restored
    assert (target / BASELINE_FILES[0]).read_text(encoding="utf-8") == "baseline"


def test_restore_baseline_without_runtime_is_noop(tmp_path):
    assert restore_baseline() == []


# --------------------------------------------------------------------------
# Plugin install / uninstall — the buttons this must back
# --------------------------------------------------------------------------

def test_plugin_status_reports_not_installed(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    status = plugin_status(root)
    assert status["installed"] is False
    assert status["plugin_path"].endswith(PLUGIN_DLL_NAME)


def test_install_plugin_copies_dll(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    dll = make_dll(tmp_path / "build" / PLUGIN_DLL_NAME)

    installed = install_plugin(root, dll)

    assert installed.is_file()
    assert installed.parent == root / "Plugins"
    assert installed.read_bytes() == dll.read_bytes()


def test_install_plugin_reports_installed_status(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    dll = make_dll(tmp_path / "build" / PLUGIN_DLL_NAME)
    install_plugin(root, dll)

    status = plugin_status(root, expected_dll=dll)
    assert status["installed"] is True
    assert status["size"] > 0
    assert status["matches_build"] is True


def test_install_plugin_backs_up_existing(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    existing = root / "Plugins" / PLUGIN_DLL_NAME
    existing.write_bytes(b"OLD")
    dll = make_dll(tmp_path / "build" / PLUGIN_DLL_NAME, size=2048)

    install_plugin(root, dll)

    backups = list((root / "Plugins").glob(PLUGIN_DLL_NAME + ".bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"OLD"


def test_install_plugin_without_plugins_folder_fails(tmp_path):
    root = make_mmm(tmp_path / "MMM", plugins=False)
    dll = make_dll(tmp_path / "build" / PLUGIN_DLL_NAME)
    with pytest.raises(MmmRuntimeError) as excinfo:
        install_plugin(root, dll)
    assert excinfo.value.code == MMM_NOT_FOUND


def test_install_plugin_without_build_fails(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    with pytest.raises(MmmRuntimeError) as excinfo:
        install_plugin(root, tmp_path / "missing" / PLUGIN_DLL_NAME)
    assert "请先构建" in excinfo.value.message


def test_uninstall_plugin_removes_dll(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    dll = make_dll(tmp_path / "build" / PLUGIN_DLL_NAME)
    install_plugin(root, dll)

    assert uninstall_plugin(root) is True
    assert not (root / "Plugins" / PLUGIN_DLL_NAME).exists()
    assert plugin_status(root)["installed"] is False


def test_uninstall_plugin_cleans_leftover_backups(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    plugins = root / "Plugins"
    (plugins / PLUGIN_DLL_NAME).write_bytes(b"CURRENT")
    (plugins / (PLUGIN_DLL_NAME + ".bak-20260101-000000")).write_bytes(b"OLD1")
    (plugins / (PLUGIN_DLL_NAME + ".bak-20260102-000000")).write_bytes(b"OLD2")

    assert uninstall_plugin(root) is True

    assert list(plugins.glob(PLUGIN_DLL_NAME + "*")) == []


def test_uninstall_plugin_on_clean_tree_returns_false(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    assert uninstall_plugin(root) is False


def test_uninstall_plugin_does_not_touch_other_plugins(tmp_path):
    root = make_mmm(tmp_path / "MMM")
    other = root / "Plugins" / "PoseSelectorPlugin.dll"
    other.write_bytes(b"OTHER")
    make_dll(root / "Plugins" / PLUGIN_DLL_NAME)

    uninstall_plugin(root)

    assert other.is_file(), "不能误删其它插件"


def test_uninstall_plugin_without_plugins_folder_fails(tmp_path):
    root = make_mmm(tmp_path / "MMM", plugins=False)
    with pytest.raises(MmmRuntimeError):
        uninstall_plugin(root)


def test_install_uninstall_round_trip_on_runtime(tmp_path):
    """完整往返：复制 Runtime → 装插件 → 卸插件，源目录始终不变。"""
    source = make_mmm(tmp_path / "MMM")
    (source / "Settings.xml").write_text("original", encoding="utf-8")
    dll = make_dll(tmp_path / "build" / PLUGIN_DLL_NAME)

    runtime = prepare_runtime(source)
    install_plugin(runtime, dll)
    assert plugin_status(runtime)["installed"] is True

    uninstall_plugin(runtime)
    assert plugin_status(runtime)["installed"] is False

    # 源目录：既没有插件，配置也没被动过。
    assert not (source / "Plugins" / PLUGIN_DLL_NAME).exists()
    assert (source / "Settings.xml").read_text(encoding="utf-8") == "original"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def test_discover_finds_a_nested_install(tmp_path):
    make_mmm(tmp_path / "apps" / "MMM" / "MikuMikuMoving64_v1292")
    found = discover_mmm(roots=[tmp_path], max_depth=4)
    assert len(found) == 1
    assert found[0].source == "自动检测"


def test_discover_finds_install_at_root(tmp_path):
    make_mmm(tmp_path)
    found = discover_mmm(roots=[tmp_path], max_depth=1)
    assert len(found) == 1


def test_discover_returns_empty_when_nothing_matches(tmp_path):
    (tmp_path / "random").mkdir()
    assert discover_mmm(roots=[tmp_path], max_depth=2) == []


def test_discover_skips_incomplete_installs(tmp_path):
    """缺 SDK 的目录不算候选 —— 避免用户选中一个装不全的 MMM。"""
    make_mmm(tmp_path / "apps" / "Broken", sdk=False)
    assert discover_mmm(roots=[tmp_path], max_depth=3) == []


def test_discover_does_not_descend_into_unrelated_deep_dirs(tmp_path):
    """第 1 层之后只进入名字带线索的目录，否则等于整盘遍历。"""
    deep = tmp_path / "aaa" / "bbb" / "ccc"
    make_mmm(deep)
    # aaa/bbb 都没有 MMM 线索 -> 到不了 ccc
    assert discover_mmm(roots=[tmp_path], max_depth=4) == []
    # 但放宽深度到第 1 层仍能走到 aaa（第 0 层无条件下降）
    assert discover_mmm(roots=[tmp_path], max_depth=1) == []


def test_discover_descends_when_name_has_a_hint(tmp_path):
    # 第 0 层无条件下降；第 1 层的名字带 "miku" 线索 -> 继续下降并在第 2 层找到。
    make_mmm(tmp_path / "aaa" / "MikuMikuMoving")
    found = discover_mmm(roots=[tmp_path], max_depth=3)
    assert len(found) == 1


def test_discover_deduplicates(tmp_path):
    make_mmm(tmp_path / "MMM")
    found = discover_mmm(roots=[tmp_path, tmp_path], max_depth=2)
    assert len(found) == 1


def test_discover_honours_max_children(tmp_path):
    for index in range(10):
        make_mmm(tmp_path / f"MMM{index}")
    found = discover_mmm(roots=[tmp_path], max_depth=1, max_children=3)
    assert len(found) <= 3


def test_discover_with_empty_root_list_finds_nothing(tmp_path):
    make_mmm(tmp_path / "MMM")
    assert discover_mmm(roots=[], max_depth=3) == []


# --------------------------------------------------------------------------
# Single-instance lock (方案 §15)
# --------------------------------------------------------------------------

from mmd_bridge.mmm_runtime import (          # noqa: E402
    LOCK_FILE_NAME,
    MMM_RUNTIME_BUSY,
    acquire_lock,
    clear_stale_lock,
    lock_path,
    lock_status,
    read_lock,
    release_lock,
)

LIVE_PID = os.getpid()


def _write_raw_lock(**fields):
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields, ensure_ascii=False), encoding="utf-8")


def test_lock_status_without_lock():
    status = lock_status()
    assert status["locked"] is False
    assert status["busy"] is False
    assert status["summary"] == "无锁"


def test_acquire_lock_creates_file_with_owner_fields():
    lock = acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert lock is not None
    assert lock.job_id == "job-1"
    assert lock.nonce == "nonce-1"
    assert lock.pid == LIVE_PID
    assert lock.created
    assert lock_path().is_file()


def test_acquire_lock_records_liveness():
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    status = lock_status()
    assert status["busy"] is True
    assert status["stale"] is False


def test_second_job_is_refused_while_first_is_alive():
    """这是锁存在的全部理由：两个 Job 不能同时覆盖 active_job.json。"""
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    with pytest.raises(MmmRuntimeError) as excinfo:
        acquire_lock("job-2", "nonce-2", pid=LIVE_PID)
    assert excinfo.value.code == MMM_RUNTIME_BUSY
    assert "job-1" in excinfo.value.message


def test_same_owner_reacquire_is_idempotent():
    """同一 job_id + nonce 重入不应被自己挡住。"""
    first = acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    second = acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert second.job_id == first.job_id
    assert second.nonce == first.nonce


def test_force_overrides_a_live_lock():
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    lock = acquire_lock("job-2", "nonce-2", pid=LIVE_PID, force=True)
    assert lock.job_id == "job-2"


def test_dead_pid_lock_is_treated_as_stale():
    _write_raw_lock(pid=999999999, job_id="dead", nonce="n",
                    created=datetime.now(timezone.utc).isoformat())
    status = lock_status()
    assert status["locked"] is True
    assert status["busy"] is False
    assert status["stale"] is True


def test_stale_lock_is_taken_over_without_error():
    _write_raw_lock(pid=999999999, job_id="dead", nonce="n",
                    created=datetime.now(timezone.utc).isoformat())
    lock = acquire_lock("job-new", "nonce-new", pid=LIVE_PID)
    assert lock.job_id == "job-new"


def test_clear_stale_lock_removes_only_stale(tmp_path):
    _write_raw_lock(pid=999999999, job_id="dead", nonce="n", created="")
    assert clear_stale_lock() is True
    assert read_lock() is None


def test_clear_stale_lock_leaves_a_live_lock_alone():
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert clear_stale_lock() is False
    assert read_lock() is not None


def test_release_lock_removes_file():
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert release_lock(job_id="job-1") is True
    assert read_lock() is None


def test_release_lock_refuses_to_release_someone_elses_lock():
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert release_lock(job_id="job-2") is False
    assert read_lock() is not None, "不能删掉别的任务的锁"


def test_release_lock_without_job_id_always_releases():
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert release_lock() is True


def test_release_lock_on_missing_file_returns_false():
    assert release_lock(job_id="whatever") is False


def test_corrupt_lock_file_is_treated_as_no_lock():
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert read_lock() is None
    # 损坏的锁不应阻止新任务
    lock = acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert lock.job_id == "job-1"


def test_lock_file_name_matches_the_plan():
    assert LOCK_FILE_NAME == ".lock"
    assert lock_path().name == LOCK_FILE_NAME


def test_lock_age_is_computed_from_created():
    _write_raw_lock(pid=LIVE_PID, job_id="j", nonce="n",
                    created=(datetime.now(timezone.utc)
                             - timedelta(seconds=30)).isoformat())
    lock = read_lock()
    assert lock is not None
    assert 25 <= lock.age <= 40


def test_lock_summary_reports_state():
    acquire_lock("job-1", "nonce-1", pid=LIVE_PID)
    assert "运行中" in read_lock().summary()
    _write_raw_lock(pid=999999999, job_id="dead", nonce="n", created="")
    assert "已失效" in read_lock().summary()


# --------------------------------------------------------------------------
# 界面语言锁定 —— UIA 按显示文本定位控件，语言一变全部失效
# --------------------------------------------------------------------------

def test_force_ui_language_rewrites_existing_node(tmp_path):
    from mmd_bridge.mmm_runtime import force_ui_language
    (tmp_path / "Settings.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<Settings>\n'
        '    <Language>ja</Language>\n    <Volume>80</Volume>\n</Settings>',
        encoding="utf-8")
    assert force_ui_language(tmp_path, "zh-CN") is True
    text = (tmp_path / "Settings.xml").read_text(encoding="utf-8")
    assert "<Language>zh-CN</Language>" in text
    assert "<Volume>80</Volume>" in text, "其余内容必须原样保留"


def test_force_ui_language_is_idempotent(tmp_path):
    from mmd_bridge.mmm_runtime import force_ui_language
    (tmp_path / "Settings.xml").write_text(
        "<Settings><Language>zh-CN</Language></Settings>", encoding="utf-8")
    assert force_ui_language(tmp_path, "zh-CN") is False, "已经是目标语言就不该改"


def test_force_ui_language_inserts_when_node_absent(tmp_path):
    from mmd_bridge.mmm_runtime import force_ui_language
    (tmp_path / "Settings.xml").write_text(
        '<?xml version="1.0"?>\n<Settings>\n    <Volume>80</Volume>\n</Settings>',
        encoding="utf-8")
    assert force_ui_language(tmp_path, "zh-CN") is True
    text = (tmp_path / "Settings.xml").read_text(encoding="utf-8")
    assert "<Language>zh-CN</Language>" in text
    assert text.index("<Language>") < text.index("<Volume>")


def test_force_ui_language_without_settings_file(tmp_path):
    from mmd_bridge.mmm_runtime import force_ui_language
    assert force_ui_language(tmp_path) is False


def test_force_ui_language_tolerates_bom(tmp_path):
    """MMM 的 Settings.xml 带 UTF-8 BOM，不能因此写坏文件。"""
    from mmd_bridge.mmm_runtime import force_ui_language
    (tmp_path / "Settings.xml").write_bytes(
        b"\xef\xbb\xbf<Settings><Language>ja</Language></Settings>")
    assert force_ui_language(tmp_path, "zh-CN") is True
    text = (tmp_path / "Settings.xml").read_text(encoding="utf-8")
    assert "<Language>zh-CN</Language>" in text


def test_default_ui_language_is_the_only_shipped_pack():
    """本机 MMM 发行版只有 zh-CN 资源包，所以锁定值必须是 zh-CN。"""
    from mmd_bridge.mmm_runtime import UI_LANGUAGE
    assert UI_LANGUAGE == "zh-CN"


def test_prepare_runtime_does_not_touch_the_language(tmp_path):
    """prepare_runtime **不得**自动改界面语言。

    用户实际安装的 MMM 发行版只带 ja 语言包，
    强制 zh-CN 会让界面回退到不确定的内置资源 —— 反而更不可控。
    语言适配应由 UIA 驱动（候选表覆盖中/日/英）负责。
    """
    from mmd_bridge.mmm_runtime import prepare_runtime, runtime_dir
    root = make_mmm(tmp_path / "MMM")
    (root / "Settings.xml").write_text(
        '<?xml version="1.0"?>\n<Settings>\n    <Language>ja</Language>\n</Settings>',
        encoding="utf-8")

    prepare_runtime(root)

    text = (runtime_dir() / "Settings.xml").read_text(encoding="utf-8")
    assert "<Language>ja</Language>" in text, "不该改动语言"
    assert "zh-CN" not in text
