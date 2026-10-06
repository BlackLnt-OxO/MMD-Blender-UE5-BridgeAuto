import hashlib

import pytest

from mmd_bridge import controller as module
from mmd_bridge.controller import Controller
from mmd_bridge.core import Job, Settings, atomic_json, read_json


def test_reconversion_archives_evidence_and_preserves_frozen_geometry(tmp_path, monkeypatch):
    controller = Controller(tmp_path/'home')
    job = Job(tmp_path/'job')
    controller.job = job
    original = {'scene':'source.blend', 'scene_format':'usd_stage',
                'scene_scale':1., 'collision_mode':'source',
                'material_strategy':'auto', 'material_bake_fallback':False,
                'material_complex_strategy':'nodes', 'bake_resolution':1024}
    atomic_json(job.path, {'settings':original})
    atomic_json(job.folder/'manifest.json', {'scene':{'old':True}, 'files':{}})
    atomic_json(job.folder/'ue_checkpoint.json', {'old':True})
    atomic_json(job.folder/'scene_checkpoint.json', {'frozen':'unchanged'})
    frozen = job.folder/'scene_checkpoint/Frozen.blend'
    frozen.parent.mkdir(); frozen.write_bytes(b'geometry restore point')
    image = job.folder/'visual/baseline/ue.png'
    image.parent.mkdir(parents=True); image.write_bytes(b'original pixels')
    before = hashlib.sha256(frozen.read_bytes()).hexdigest()
    monkeypatch.setattr(module, 'ensure_blender_job_stopped', lambda folder:None)
    resumed = []
    controller.resume = lambda folder:resumed.append(folder)
    archive = controller.reconvert_scene_materials(Settings(
        material_bake_fallback=True, material_complex_strategy='pbr_bake', bake_resolution=2048))
    assert (archive/'visual/baseline/ue.png').read_bytes() == image.read_bytes()
    assert read_json(archive/'job.json')['settings'] == original
    assert read_json(archive/'ue_checkpoint.json') == {'old':True}
    assert not (job.folder/'ue_checkpoint.json').exists()
    assert read_json(job.folder/'manifest.json') == {'files':{}}
    updated = read_json(job.path)['settings']
    assert updated['material_bake_fallback'] and updated['material_complex_strategy'] == 'pbr_bake'
    assert updated['bake_resolution'] == 2048
    assert updated['scene_scale'] == 1. and updated['collision_mode'] == 'source'
    assert hashlib.sha256(frozen.read_bytes()).hexdigest() == before
    assert resumed == [job.folder]


def test_reconversion_refuses_live_converter(tmp_path):
    controller = Controller(tmp_path/'home')
    controller.job = Job(tmp_path/'job')
    controller.processes = [('scene', object())]
    with pytest.raises(ValueError, match='已停止'):
        controller.reconvert_scene_materials(Settings())
    assert not list(controller.job.folder.glob('PreviousMaterials_*'))


def test_gui_can_select_a_stopped_frozen_scene_without_starting_it(tmp_path):
    controller=Controller(tmp_path/'home')
    job=Job(tmp_path/'job')
    atomic_json(job.path,{'settings':{'scene':'source.blend','scene_format':'usd_stage'}})
    atomic_json(job.folder/'manifest.json',{'files':{}})
    atomic_json(job.folder/'scene_checkpoint.json',{'frozen':'retained'})
    job.status('stopped','kept checkpoint')
    assert controller.load_scene_results(job.folder).folder==job.folder
    assert controller.stopped and not controller.processes
    assert read_json(job.folder/'status.json')['state']=='stopped'


def compile_retry_fixture(tmp_path,monkeypatch):
    controller=Controller(tmp_path/'home');controller.job=Job(tmp_path/'job')
    settings={'scene':'anonymous.blend','scene_format':'usd_stage','material_strategy':'auto',
        'material_bake_fallback':True,'material_complex_strategy':'pbr_bake'}
    atomic_json(controller.job.path,{'settings':settings})
    monkeypatch.setattr(module,'UE_CODE_MODULES',('runtime.py',))
    monkeypatch.setattr(module,'file_hash',lambda path:'current-runtime')
    atomic_json(controller.job.folder/'ue_report.json',{
        'runtime_code_hashes':{'runtime.py':'current-runtime'},
        'material_source_records':[{'id':'failed','name':'Anonymous','strategy_requested':'auto'}],
        'failures':[{'stage':'material','material_id':'failed','error':'原生材质编译失败'}]})
    controller.mailbox=tmp_path/'mailbox'
    atomic_json(controller.mailbox/'request.json',{'id':'current','job':str(controller.job.folder)})
    atomic_json(controller.mailbox/'response.json',{'id':'current','state':'needs_review'})
    return controller,settings


def test_automatic_compile_recovery_waits_for_matching_receipt_and_retries_once(tmp_path,monkeypatch):
    controller,settings=compile_retry_fixture(tmp_path,monkeypatch)
    calls=[]
    def reconvert(cfg):
        calls.append(cfg)
        data=controller.job.data;data['material_bake_retry']=controller._material_retry_sources()
        atomic_json(controller.job.path,data)
    monkeypatch.setattr(controller,'reconvert_scene_materials',reconvert)
    atomic_json(controller.mailbox/'response.json',{'id':'stale','state':'needs_review'})
    assert not controller._poll_material_bake_retry('needs_review',settings)
    atomic_json(controller.mailbox/'response.json',{'id':'current','state':'needs_review'})
    controller.processes=[('scene',object())]
    assert not controller._poll_material_bake_retry('needs_review',settings)
    controller.processes=[]
    assert controller._poll_material_bake_retry('needs_review',settings)
    assert not controller._poll_material_bake_retry('needs_review',settings)
    assert len(calls)==1


def test_previous_runtime_failure_is_revalidated_instead_of_forcing_bake(tmp_path,monkeypatch):
    controller,settings=compile_retry_fixture(tmp_path,monkeypatch)
    path=controller.job.folder/'ue_report.json';report=read_json(path)
    report['runtime_code_hashes']['runtime.py']='previous-runtime';atomic_json(path,report)
    assert controller._material_retry_sources()=={}
    assert not controller._poll_material_bake_retry('needs_review',settings)


@pytest.mark.parametrize('overrides',[{'material_strategy':'nodes'}, {'material_bake_fallback':False},
    {'material_complex_strategy':'nodes'}])
def test_compile_recovery_respects_current_material_policy(tmp_path,monkeypatch,overrides):
    controller,settings=compile_retry_fixture(tmp_path,monkeypatch)
    assert not controller._poll_material_bake_retry('needs_review',{**settings,**overrides})
