import json
import struct
from pathlib import Path
import pytest
from mmd_bridge.core import (inspect_vmd, Settings, Job, atomic_json, stable_id,
                             validate_fbx, recoverable_ue_asset_packages,
                             recoverable_ue_generated_mesh,
                             scene_mesh_record_is_importable, SCHEMA)
from mmd_bridge.discovery import parse_vdf


def vmd(path,bones=0,morphs=0,cameras=0):
    content=b"Vocaloid Motion Data 0002".ljust(30,b"\0")+b"model".ljust(20,b"\0")
    for count,size,n in ((bones,111,15),(morphs,23,15),(cameras,61,0)):
        content+=struct.pack('<I',count)
        for i in range(count):
            content+=(b'A'.ljust(n,b'\0') if n else b'')+struct.pack('<I',30+i)+bytes(size-n-4)
    path.write_bytes(content)
    return path


def test_vmd_discriminates_lip_and_camera(tmp_path):
    lip=inspect_vmd(vmd(tmp_path/'lip.vmd',morphs=3))
    cam=inspect_vmd(vmd(tmp_path/'cam.vmd',cameras=2))
    assert lip['tracks']['camera']==0 and lip['max_frame']==32
    assert cam['tracks']['camera']==2 and cam['tracks']['bone']==0


def test_vmd_rejects_corrupt_count(tmp_path):
    path=vmd(tmp_path/'bad.vmd')
    b=bytearray(path.read_bytes());struct.pack_into('<I',b,50,2**31);path.write_bytes(b)
    with pytest.raises(ValueError,match='截断'):
        inspect_vmd(path)


def test_recovery_only_adopts_manifest_derived_ue_packages():
    root = '/Game/MMDBridge/A_owner'
    manifest = {
        'materials': [{
            'id': 'material_pending', 'asset_name': 'M_Pending',
            'base_color': {'op': 'texture', 'image': {'id': '0123456789abcdef', 'path': 'a.png'}},
        }],
        'scene': {
            'meshes': [{'name': 'SM_Scene_1'}],
            'materials': [{'id': 'material_done', 'asset_name': 'M_Done'}],
            'render_look': {'status': 'converted'},
        },
    }
    checkpoint = {'completed': {'materials': ['material_done']}}

    packages = recoverable_ue_asset_packages(root, manifest, checkpoint)

    assert root + '/Textures/T_0123456789abcdef_N' in packages
    assert root + '/Scene/SM_Scene_1' in packages
    assert root + '/Materials/M_Pending' in packages
    assert root + '/Materials/M_Done' not in packages
    assert root + '/Sequences/MRQ_SourceLook' in packages
    assert root + '/Materials/MRQ_SourceLook' in packages  # Legacy checkpoints.
    assert root + '/Textures/UserTexture' not in packages
    assert recoverable_ue_generated_mesh(root + '/Scene/Generated/SM_Mesh', root, True)
    assert not recoverable_ue_generated_mesh(root + '/Scene/Generated/UserMaterial', root, False)
    assert not recoverable_ue_generated_mesh(root + '/Scene/GeneratedExtra/SM_Mesh', root, True)


def test_scene_mesh_record_rejects_empty_or_degenerate_geometry():
    assert scene_mesh_record_is_importable({'vertices': 3})
    assert scene_mesh_record_is_importable({'vertices': 8, 'polygons': 6})
    assert not scene_mesh_record_is_importable({'vertices': 0})
    assert not scene_mesh_record_is_importable({'vertices': 8, 'polygons': 0})
    assert not scene_mesh_record_is_importable({'vertices': 8, 'polygons': 6, 'degenerate': True})


def test_atomic_json_retries_transient_windows_file_lock(tmp_path, monkeypatch):
    import os
    from mmd_bridge.core import atomic_json, read_json

    replace = os.replace
    calls = []

    def once(source, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError('temporary sharing violation')
        replace(source, target)

    monkeypatch.setattr(os, 'replace', once)
    path = tmp_path / 'status.json'
    atomic_json(path, {'state': 'ready'})
    assert len(calls) == 2
    assert read_json(path) == {'state': 'ready'}


def test_camera_input_is_semantically_checked(tmp_path):
    exe=tmp_path/'blender.exe';exe.touch()
    pmx=tmp_path/'a.pmx';pmx.write_bytes(b'PMX ')
    motion=vmd(tmp_path/'motion.vmd',bones=1)
    lip=vmd(tmp_path/'lip.vmd',morphs=2)
    cfg=Settings(blender=str(exe),model=str(pmx),motion=str(motion),camera=str(lip))
    with pytest.raises(ValueError,match='选反'):
        cfg.validate()


def test_steam_multiple_libraries_and_escaped_paths():
    value=parse_vdf('"libraryfolders" { "0" { "path" "C:\\\\Steam" } "1" { "path" "F:\\\\Games" "apps" { "365670" "42" } } }')
    assert value['libraryfolders']['1']['path']=='F:\\Games'
    assert value['libraryfolders']['1']['apps']['365670']=='42'


def test_invalid_parameters_are_rejected(tmp_path):
    scene=tmp_path/'scene.blend';scene.touch()
    exe=tmp_path/'blender.exe';exe.touch()
    for patch in ({'scale':float('nan')},{'scale':0},{'scene_light_scale':-1},
                  {'fps':0},{'content_root':'/Engine/Bad'},{'start':20,'end':2}):
        with pytest.raises(ValueError):
            Settings(blender=str(exe),scene=str(scene),**patch).validate()


def test_schema_v2_usd_defaults_and_legacy_selector(tmp_path):
    scene=tmp_path/'scene.blend';scene.touch()
    exe=tmp_path/'blender.exe';exe.touch()
    current=Settings(blender=str(exe),scene=str(scene))
    assert SCHEMA == 2
    assert current.scene_format == 'usd_stage'
    assert current.scene_light_scale == 1
    assert current.restore_scene_appearance
    assert current.scene_frame == 1
    assert current.bake_resolution == 2048
    current.validate()
    Settings(blender=str(exe),scene=str(scene),scene_format='fbx_legacy').validate()
    with pytest.raises(ValueError,match='场景通路'):
        Settings(blender=str(exe),scene=str(scene),scene_format='obj').validate()


def test_resume_fingerprints_prevent_stale_export(tmp_path):
    scene=tmp_path/'scene.blend';scene.write_bytes(b'v1')
    exe=tmp_path/'blender.exe';exe.touch()
    job=Job.create(tmp_path/'jobs',Settings(blender=str(exe),scene=str(scene)))
    job.verify_inputs()
    scene.write_bytes(b'v2')
    with pytest.raises(ValueError,match='改变'):
        job.verify_inputs()


def test_new_jobs_keep_previous_asset_package(tmp_path):
    scene=tmp_path/'scene.blend';scene.write_bytes(b'blend')
    exe=tmp_path/'blender.exe';exe.touch()
    settings=Settings(blender=str(exe),scene=str(scene))
    first=Job.create(tmp_path/'jobs',settings)
    second=Job.create(tmp_path/'jobs',settings)
    assert first.data['asset_id'] != second.data['asset_id']


def test_independent_scene_camera_jobs_use_readable_names(tmp_path):
    from mmd_bridge.core import asset_name
    blender = tmp_path/'blender.exe'; blender.touch()
    scene = tmp_path/'street.blend'; scene.touch()
    camera = vmd(tmp_path/'camera.vmd', cameras=1)
    jobs = [Job.create(tmp_path/'jobs', Settings(blender=str(blender), scene=str(scene), flow='scene'))
            for _ in range(2)]
    assert [job.folder.name for job in jobs] == ['Scene_street_01', 'Scene_street_02']
    assert jobs[0].data['asset_id'] == jobs[0].folder.name
    Settings(blender=str(blender), camera=str(camera), flow='camera').validate()
    assert asset_name('M', 'Volume Scatter', 'a') == 'M_Volume_Scatter_01'
    assert asset_name('M', 'Volume Scatter', 'b') == 'M_Volume_Scatter_02'


def test_atomic_json_and_stable_identity(tmp_path):
    path=tmp_path/'nested/a.json'
    atomic_json(path,{'name':'初音ミク'})
    assert json.loads(path.read_text(encoding='utf-8'))['name']=='初音ミク'
    assert stable_id('a','bc')!=stable_id('ab','c')
    assert not list(path.parent.glob('*.tmp'))


def test_fbx_validation_rejects_partial_export(tmp_path):
    path=tmp_path/'bad.fbx';path.write_bytes(b'Kaydara FBX Binary')
    with pytest.raises(ValueError):
        validate_fbx(path)


# --------------------------------------------------------------------------
# Stage 2.11 —— 默认角色通路切换为 fbx_legacy（方案 §31）
# --------------------------------------------------------------------------

def test_default_character_backend_is_fbx_legacy():
    """默认通路是 Blender/FBX，不再是 MMD2Unreal。"""
    assert Settings().character_backend == "fbx_legacy"


def test_backend_default_matches_every_fallback(tmp_path):
    """dataclass 默认值必须与各处的 .get(key, fallback) 一致。

    改造前这两者相反：新建任务是 mmd2unreal，但 job.json 缺键时按 fbx_legacy 走。
    这个测试把"两套默认值"锁死，避免再次分叉。
    """
    from dataclasses import asdict
    payload = asdict(Settings())
    del payload["character_backend"]
    assert payload.get("character_backend", "fbx_legacy") == \
        Settings().character_backend


def test_fbx_backend_requires_model_and_motion(tmp_path):
    """FBX 通路下"只有模型"是无效任务 —— 这正是 MMD2Unreal 与它的差别。"""
    model = tmp_path / "a.pmx"
    model.write_bytes(b"PMX ")
    settings = Settings(model=str(model))
    with pytest.raises(ValueError, match="模型和动作"):
        settings.validate()


def test_fbx_backend_accepts_model_with_motion(tmp_path):
    model = tmp_path / "a.pmx"; model.write_bytes(b"PMX ")
    motion = vmd(tmp_path / "a.vmd", bones=1)
    blender = tmp_path / "blender.exe"; blender.write_bytes(b"MZ")
    settings = Settings(model=str(model), motion=str(motion),
                        blender=str(blender))
    settings.validate()          # 不应抛异常


def test_mmd2unreal_remains_selectable_as_legacy(tmp_path):
    """Legacy 通路必须仍然可用 —— 只降级，不删除。"""
    model = tmp_path / "a.pmx"
    model.write_bytes(b"PMX ")
    settings = Settings(model=str(model), character_backend="mmd2unreal")
    settings.validate()          # 仅模型即可，不抛异常
    assert settings.character_backend == "mmd2unreal"


def test_unknown_backend_is_still_rejected(tmp_path):
    model = tmp_path / "a.pmx"; model.write_bytes(b"PMX ")
    motion = vmd(tmp_path / "a.vmd", bones=1)
    settings = Settings(model=str(model), motion=str(motion),
                        character_backend="something_else")
    with pytest.raises(ValueError, match="未知角色通路"):
        settings.validate()


def test_mmm_path_defaults_to_empty():
    """MMM 目录是可选的：没配也能跑非 MMM 的任务。"""
    assert Settings().mmm_path == ""


def test_mmm_path_is_not_required_by_validation(tmp_path):
    """MMM 尚未接入主流程时，不能因为它没配就判定任务无效。"""
    model = tmp_path / "a.pmx"; model.write_bytes(b"PMX ")
    motion = vmd(tmp_path / "a.vmd", bones=1)
    blender = tmp_path / "blender.exe"; blender.write_bytes(b"MZ")
    Settings(model=str(model), motion=str(motion),
             blender=str(blender), mmm_path="").validate()
