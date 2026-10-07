import copy

import pytest

from mmd_bridge.core import atomic_json
from mmd_bridge.material_regression import capture, compare


def job(tmp_path, name, *, color=0.4, texture=b'alpha coverage', runtime='same', function='f'):
    folder = tmp_path / name
    folder.mkdir()
    texture_path = folder / 'mask.png'
    texture_path.write_bytes(texture)
    atomic_json(folder / 'job.json', {'inputs': {'scene': 'source'},
                                    'settings': {'material_workflow': 'legacy_main_v1'}})
    scene = {'materials': [{'id': 'leaf', 'source_name': 'Leaf', 'strategy': 'nodes',
        'source_objects': ['tree'], 'conversion_status': 'converted',
        'channels': {'opacity': {'op': 'texture', 'image': {'id': name,
            'name': name, 'path': str(texture_path), 'color_space': 'Non-Color'}},
            'base_color': {'op': 'function_call', 'function': function, 'args': {}}}}],
        'function_definitions': [{'id': function, 'name': name,
            'body': {'op': 'constant', 'value': color}, 'output_type': 'RGBA'}],
        'prototypes': [{'id': 'geometry-and-uv-hash', 'material_slots': ['leaf']}],
        'instances': [{'prototype_id': 'geometry-and-uv-hash', 'shader_data': {'object_random': 0.2}}]}
    atomic_json(folder / 'manifest.json', {'scene': scene})
    atomic_json(folder / 'ue_report.json', {'root': '/Game/' + name + '/Scene',
        'checks': {'material_compile_errors': []},
        'runtime_code_hashes': {'ue_materials.py': runtime}})
    return folder


def test_new_directory_and_resource_names_are_not_material_drift(tmp_path):
    a = capture(job(tmp_path, 'old', function='old-f'))
    b = capture(job(tmp_path, 'fresh', function='new-f'))
    assert compare(a, b)['technical_gate_passed']


@pytest.mark.parametrize('change', [{'color': 0.6}, {'texture': b'different cutout coverage'}])
def test_shared_function_and_mask_content_drift_block_release(tmp_path, change):
    a = capture(job(tmp_path, 'old'))
    b = capture(job(tmp_path, 'new', **change))
    result = compare(a, b)
    assert not result['technical_gate_passed']
    assert result['differences'] == [{'scope': 'material', 'id': 'leaf', 'name': 'Leaf'}]


def test_coordinate_carrier_and_binding_changes_block_release(tmp_path):
    a = capture(job(tmp_path, 'old'))
    folder = job(tmp_path, 'new')
    from mmd_bridge.core import read_json
    manifest = read_json(folder / 'manifest.json')
    manifest['scene']['prototypes'][0]['id'] = 'changed-uv-hash'
    manifest['scene']['prototypes'][0]['material_slots'] = ['wrong-material']
    atomic_json(folder / 'manifest.json', manifest)
    assert {'scope': 'geometry_signature'} in compare(a, capture(folder))['differences']


def test_same_export_with_changed_renderer_requires_native_visual_regression(tmp_path):
    a = capture(job(tmp_path, 'old'))
    b = capture(job(tmp_path, 'new', runtime='changed'))
    result = compare(a, b)
    assert result['output_contract_equal'] and result['renderer_changed']
    assert not result['technical_gate_passed']


def test_vertex_normal_payload_changes_block_release_even_with_same_topology(tmp_path):
    from mmd_bridge.core import read_json
    snapshots = []
    for name, data in [('old', b'original normals'), ('new', b'changed normals')]:
        folder = job(tmp_path, name)
        payload = folder / 'Payload.usdc'
        payload.write_bytes(data)
        manifest = read_json(folder / 'manifest.json')
        manifest['scene']['usd'] = {'files': [{'relative': 'Geometry/Payload_01.usdc',
                                              'path': str(payload)}]}
        atomic_json(folder / 'manifest.json', manifest)
        snapshots.append(capture(folder))
    result = compare(*snapshots)
    assert not result['technical_gate_passed']
    assert result['differences'] == [{'scope': 'geometry_signature'}]


def test_missing_texture_does_not_pass_as_equal(tmp_path):
    folder = job(tmp_path, 'new')
    (folder / 'mask.png').unlink()
    with pytest.raises(ValueError, match='贴图缺失'):
        capture(folder)


def test_source_and_settings_changes_cannot_replace_baseline(tmp_path):
    a = capture(job(tmp_path, 'old'))
    b = copy.deepcopy(a)
    b['source_sha256'] = 'edited scene'
    b['settings']['bake_resolution'] = 512
    result = compare(a, b)
    assert not result['technical_gate_passed']
    assert result['differences'] == [{'scope': 'source_sha256'}, {'scope': 'settings'}]


def test_rechecking_the_same_job_is_integrity_evidence_not_reimport_evidence(tmp_path):
    snapshot = capture(job(tmp_path, 'same'))
    result = compare(snapshot, snapshot)
    assert result['output_contract_equal']
    assert not result['independent_import'] and not result['technical_gate_passed']


def test_bake_duration_does_not_mask_real_bake_strategy_changes(tmp_path):
    from mmd_bridge.core import read_json
    snapshots = []
    for name, seconds in [('old', 1.1), ('new', 1.2)]:
        folder = job(tmp_path, name)
        manifest = read_json(folder / 'manifest.json')
        manifest['scene']['materials'][0]['basic_pbr_bake'] = {'status': 'baked', 'seconds': seconds}
        atomic_json(folder / 'manifest.json', manifest)
        snapshots.append(capture(folder))
    assert compare(*snapshots)['technical_gate_passed']


def test_failure_diagnostic_text_is_separate_from_output_and_failure_status(tmp_path):
    from mmd_bridge.core import read_json
    snapshots = []
    folders = []
    for name, error in [('old', 'first unsupported branch'), ('new', 'later unsupported branch')]:
        folder = job(tmp_path, name)
        folders.append(folder)
        manifest = read_json(folder / 'manifest.json')
        manifest['scene']['materials'][0]['basic_pbr_bake'] = {'status': 'failed', 'error': error}
        atomic_json(folder / 'manifest.json', manifest)
        snapshots.append(capture(folder))
    assert compare(*snapshots)['technical_gate_passed']
    manifest['scene']['materials'][0]['basic_pbr_bake']['status'] = 'baked'
    atomic_json(folders[1] / 'manifest.json', manifest)
    assert not compare(snapshots[0], capture(folders[1]))['technical_gate_passed']


def test_semantic_geometry_receipt_is_bound_to_actual_payload_bytes(tmp_path):
    from mmd_bridge.core import read_json, file_hash
    folder = job(tmp_path, 'new')
    payload = folder / 'Payload.usdc'
    payload.write_bytes(b'valid payload')
    manifest = read_json(folder / 'manifest.json')
    manifest['scene']['usd'] = {'files': [{'relative': 'Geometry/Payload.usdc', 'path': str(payload)}]}
    atomic_json(folder / 'manifest.json', manifest)
    atomic_json(folder / 'audit/geometry_contract.json', {'schema': 1, 'files': {
        'Geometry/Payload.usdc': {'file_sha256': file_hash(payload), 'semantic_sha256': 'geometry'}}})
    assert capture(folder)['geometry_format'] == 'usd_semantics_import_root_normalized'
    payload.write_bytes(b'changed normals after receipt')
    with pytest.raises(ValueError, match='修改'):
        capture(folder)
