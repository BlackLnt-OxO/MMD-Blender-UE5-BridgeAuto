from types import SimpleNamespace
from mmd_bridge.core import atomic_json, file_hash, read_json
from mmd_bridge.usd_scene_optimizer import remap_scene, needs_import_compaction, prepare_import_compaction


def test_compaction_validates_cached_geometry_and_can_restore_original_manifest(tmp_path):
    geometry=tmp_path/'Geometry.usda';geometry.write_text('original')
    original={'scene':{'route':'usd_stage','instances':[{'prim_path':'/Tree'}]}}
    atomic_json(tmp_path/'manifest_before_import_compaction.json',original)
    prepared={'scene':{'route':'usd_stage','scatter_batches':[{'prim_path':'/Batch'}],
        'import_compaction':{'version':2,'geometry':str(geometry),'geometry_sha256':file_hash(geometry)}}}
    atomic_json(tmp_path/'manifest.json',prepared)
    states=[]
    job=SimpleNamespace(folder=tmp_path,data={'settings':{'optimize_usd_scatter':True}},
                        status=lambda *args:states.append(args))
    assert not needs_import_compaction(job)
    geometry.write_text('damaged')
    assert needs_import_compaction(job)
    job.data['settings']['optimize_usd_scatter']=False
    assert needs_import_compaction(job)
    prepare_import_compaction(job)
    assert read_json(tmp_path/'manifest.json')==original
    assert not needs_import_compaction(job)
    assert states[-1][0]=='scene_done'


def test_remap_keeps_source_names_transforms_and_instance_indices():
    scene = {'instances': [
        {'name':'Tree emitter','label':'leaf 1','prim_path':'/Owner/Leaf1','matrix':[[1]],'prototype_id':'tree'},
        {'name':'Tree emitter','label':'leaf 2','prim_path':'/Owner/Leaf2','matrix':[[2]],'prototype_id':'tree'},
        {'name':'Glass','prim_path':'/Glass'}],
        'prototypes':[{'id':'tree','prim_path':'/Owner/Leaf1'}],
        'hierarchy':[{'name':'Tree emitter','prim_path':'/Owner','parts':[
            {'name':'leaf 1','prim_path':'/Owner/Leaf1'}, {'name':'leaf 2','prim_path':'/Owner/Leaf2'}]}]}
    optimization = {'mapping':{'/Owner/Leaf1':{'prim_path':'/Owner/Scatter','instance_index':0},
                               '/Owner/Leaf2':{'prim_path':'/Owner/Scatter','instance_index':1}},
                    'batches':[{'prim_path':'/Owner/Scatter','instances':2}], 'batched_instances':2}
    remap_scene(scene, optimization)
    assert scene['instances'][0]['matrix'] == [[1]] and scene['instances'][1]['matrix'] == [[2]]
    assert scene['instances'][0]['label'] == 'leaf 1'
    assert [x['instance_index'] for x in scene['instances'][:2]] == [0,1]
    assert scene['instances'][0]['source_prim_path'] == '/Owner/Leaf1'
    assert scene['instances'][2] == {'name':'Glass','prim_path':'/Glass'}
    assert scene['hierarchy'][0]['prim_path'] == '/Owner'
    assert scene['hierarchy'][0]['parts'] == [{'name':'Scatter batch','prim_path':'/Owner/Scatter'}]
    assert scene['prototypes'][0]['prim_path'] == '/Owner/Scatter'
