import json
import struct
from pathlib import Path
import pytest
from mmd_bridge.core import (inspect_vmd, Settings, Job, atomic_json, stable_id,
                             validate_fbx, recoverable_ue_asset_packages,
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
        },
    }
    checkpoint = {'completed': {'materials': ['material_done']}}

    packages = recoverable_ue_asset_packages(root, manifest, checkpoint)

    assert root + '/Textures/T_0123456789abcdef_N' in packages
    assert root + '/Scene/SM_Scene_1' in packages
    assert root + '/Materials/M_Pending' in packages
    assert root + '/Materials/M_Done' not in packages
    assert root + '/Textures/UserTexture' not in packages


def test_scene_mesh_record_rejects_empty_or_degenerate_geometry():
    assert scene_mesh_record_is_importable({'vertices': 3})
    assert scene_mesh_record_is_importable({'vertices': 8, 'polygons': 6})
    assert not scene_mesh_record_is_importable({'vertices': 0})
    assert not scene_mesh_record_is_importable({'vertices': 8, 'polygons': 0})
    assert not scene_mesh_record_is_importable({'vertices': 8, 'polygons': 6, 'degenerate': True})


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
    for patch in ({'scale':float('nan')},{'scale':0},{'fps':0},{'content_root':'/Engine/Bad'},{'start':20,'end':2}):
        with pytest.raises(ValueError):
            Settings(blender=str(exe),scene=str(scene),**patch).validate()


def test_schema_v2_usd_defaults_and_legacy_selector(tmp_path):
    scene=tmp_path/'scene.blend';scene.touch()
    exe=tmp_path/'blender.exe';exe.touch()
    current=Settings(blender=str(exe),scene=str(scene))
    assert SCHEMA == 2
    assert current.scene_format == 'usd_stage'
    assert current.scene_frame == 1
    assert current.bake_resolution == 0
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
