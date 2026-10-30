from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from mmd_bridge.core import Job, Settings, atomic_json, read_json
from mmd_bridge.controller import Controller
from mmd_bridge.orchestration import CompositeRun, assembly_manifest, child_folder, module_settings


def settings(tmp_path, **updates):
    cfg = dict(character_backend='mmdbridge_fbx', model='role.pmx', motion='dance.vmd',
               lipsync='face.vmd', scene='stage.blend', camera='camera.vmd',
               character_output_dir=str(tmp_path), blender='scene.exe', character_blender='role.exe')
    return Settings(**(cfg | updates))


def test_modules_clear_foreign_inputs_and_keep_character_software(tmp_path):
    parts = dict(module_settings(settings(tmp_path, additional_characters=[
        {'model':'other.pmx','motion':'other.vmd'}])))
    assert list(parts) == ['scene', 'character_0001', 'character_0002', 'camera']
    assert not any(getattr(parts['scene'], key) for key in ('model','motion','lipsync','camera'))
    assert parts['character_0001'].blender == 'role.exe'
    assert parts['character_0002'].model == 'other.pmx'
    assert parts['character_0002'].lipsync == ''
    assert not any(getattr(parts['camera'], key) for key in ('model','motion','lipsync','scene'))
    assert all(not part.additional_characters for part in parts.values())


@pytest.mark.parametrize('scene', ['missing.blend', ''])
def test_new_composite_defers_branch_validation_instead_of_aborting(tmp_path, scene):
    c = Controller(tmp_path/'home')
    parent = c.start(settings(tmp_path, scene=scene))
    assert parent.data['orchestration_version'] == 1
    assert parent.data['inputs'] == {}
    assert c.orchestrator and not c.processes


@pytest.mark.parametrize('historical,expected', [(False,1),(True,0)])
def test_child_processing_identity_follows_parent_version(tmp_path,historical,expected):
    owner=Controller(tmp_path/'home');owner.start(settings(tmp_path))
    data=owner.job.data
    assert data['geometry_input_identity_version']==1
    if historical:
        data.pop('geometry_input_identity_version');atomic_json(owner.job.path,data)
    owner.orchestrator.factory=lambda:FakeModule([],[])
    owner.poll()
    assert owner.orchestrator.child.geometry_input_identity_version==expected


@pytest.mark.parametrize('saved,expected', [('missing', None), (None, None),
    ('weld_weighted', 'weld_weighted'), ('weld_weighted_except_hair', 'weld_weighted_except_hair'),
    ('weld_weighted_no_hair_weld', 'weld_weighted_no_hair_weld')])
def test_pending_saved_role_does_not_adopt_a_new_geometry_default(tmp_path, saved, expected):
    owner=Controller(tmp_path/'home');owner.start(settings(tmp_path))
    data=owner.job.data
    for item in data['module_jobs']:
        if item['id']!='character_0001':
            item['state']='failed'
        elif saved=='missing':
            item['settings'].pop('character_geometry_mode')
        else:
            item['settings']['character_geometry_mode']=saved
    atomic_json(owner.job.path,data)
    class Capture(FakeModule):
        def start(self,cfg):
            self.cfg=cfg
            return super().start(cfg)
    owner.orchestrator.factory=lambda:Capture([],[])
    owner.poll()
    assert owner.orchestrator.child.cfg.character_geometry_mode==expected


class FakeModule:
    """Native failures are injected at the existing route boundary."""
    def __init__(self, history, failures, export_failures=()):
        self.history, self.failures, self.export_failures = history, failures, export_failures
        self.job = None
        self.processes = []
        self.bake_thread = self.geometry_thread = None
        self.defer_ue = False
        self.mailbox = None

    def start(self, cfg):
        identity = Path(cfg.character_output_dir).name
        self.history.append(('export', identity))
        if identity in self.export_failures:
            raise ValueError('invalid input or exporter exited')
        self.job = Job(Path(cfg.character_output_dir)/'MMDBridge_Jobs'/'Output_01')
        self.job.folder.mkdir(parents=True)
        atomic_json(self.job.path, {'schema':2,'id':identity,'asset_id':identity,
            'settings':asdict(cfg),'inputs':{}})
        atomic_json(self.job.folder/'manifest.json', {'schema':2,'job_id':identity,'files':{},
            'fps':30,'frame_range':[0,600],'materials':[],'warnings':[]})
        self.job.status('exports_done','export done')
        return self.job

    def resume(self, folder):
        self.job = Job(folder)
        identity = self.job.data['id']
        self.history.append(('export' if self.defer_ue else 'import', identity))
        self.job.status('exports_done' if self.defer_ue else
                        'failed' if identity in self.failures else 'complete', 'native result')
        atomic_json(self.job.folder/'ue_report.json', {'job_id':identity,'project':'',
            'assets':{'level':'/Game/Fixture/Map'},'complete':True})

    def poll(self):
        return read_json(self.job.folder/'status.json')

    def stop(self):
        self.job.status('stopped','stopped')


def drive(tmp_path, monkeypatch, failures=(), export_failures=(), extra=()):
    owner = Controller(tmp_path/'home')
    owner.start(settings(tmp_path, additional_characters=list(extra)))
    history = []
    owner.orchestrator.factory = lambda: FakeModule(history, failures, export_failures)
    monkeypatch.setattr('mmd_bridge.orchestration.verified_receipt',
        lambda parent,item: (Job(item['job']), read_json(Path(item['job'])/'manifest.json'),
                             read_json(Path(item['job'])/'ue_report.json')))
    monkeypatch.setattr('mmd_bridge.orchestration.assembly_manifest', lambda parent:
        {'composition':[{'verified':True}], 'module_results':parent.data['module_jobs']})
    for _ in range(35):
        if owner.orchestrator.assembling:
            break
        status = owner.poll()
        if status['state']=='failed':
            break
    return owner, history


@pytest.mark.parametrize('failed', ['scene','character_0001','camera'])
@pytest.mark.parametrize('phase', ['export','import'])
def test_fatal_module_failure_does_not_stop_other_modules(tmp_path, monkeypatch, failed, phase):
    owner, history = drive(tmp_path, monkeypatch,
        failures=[failed] if phase=='import' else [],
        export_failures=[failed] if phase=='export' else [])
    states = {item['id']:item['state'] for item in owner.job.data['module_jobs']}
    assert states[failed]=='failed'
    assert all(state=='complete' for identity,state in states.items() if identity!=failed)
    assert owner.orchestrator.assembling
    assert all(('import', identity) in history for identity in states if identity!=failed)


def test_exports_finish_before_any_ue_import(tmp_path, monkeypatch):
    owner, history = drive(tmp_path, monkeypatch)
    assert [phase for phase,_ in history] == ['export']*3+['import']*3


def test_one_role_failure_does_not_stop_second_role(tmp_path, monkeypatch):
    owner, _ = drive(tmp_path, monkeypatch, failures=['character_0001'],
        extra=[{'model':'same.pmx','motion':'dance.vmd'}])
    states = {item['id']:item['state'] for item in owner.job.data['module_jobs']}
    assert states['character_0002']=='complete'
    assert states['character_0001']=='failed'


def test_all_failures_have_no_false_assembly(tmp_path, monkeypatch):
    owner, _ = drive(tmp_path, monkeypatch, export_failures=['scene','character_0001','camera'])
    assert owner.poll()['state']=='failed'
    assert not owner.orchestrator.assembling


def test_stop_does_not_schedule_another_module(tmp_path, monkeypatch):
    owner = Controller(tmp_path/'home'); owner.start(settings(tmp_path))
    history=[]
    owner.orchestrator.factory=lambda:FakeModule(history, [])
    owner.poll(); owner.stop()
    for _ in range(3): assert owner.poll()['state']=='stopped'
    assert len(history)==1
    assert (owner.job.folder/'stop').exists()


def test_module_paths_cannot_escape_parent(tmp_path):
    owner=Controller(tmp_path/'home'); owner.start(settings(tmp_path))
    with pytest.raises(ValueError,match='超出'):
        child_folder(owner.job,tmp_path/'foreign')


def test_deferred_single_route_does_not_start_ue(tmp_path):
    c=Controller(tmp_path/'home'); c.job=Job(tmp_path/'child'); c.defer_ue=True
    c.launch_ue()
    assert read_json(c.job.folder/'status.json')['state']=='exports_done'
    assert not c.ue_started


def test_resume_reuses_successful_modules_and_retries_failed_role_only(tmp_path, monkeypatch):
    owner, _ = drive(tmp_path, monkeypatch, failures=['character_0001'])
    restored = Controller(tmp_path/'other_home')
    restored.resume(owner.job.folder)
    history=[]
    restored.orchestrator.factory=lambda:FakeModule(history, [])
    for _ in range(15):
        if restored.orchestrator.assembling: break
        restored.poll()
    assert history==[('export','character_0001'),('import','character_0001')]
    assert all(item['state']=='complete' for item in restored.job.data['module_jobs'])


def test_camera_duration_is_shared_without_requiring_a_valid_scene(tmp_path):
    from tests.test_core import vmd
    camera=vmd(tmp_path/'camera.vmd',cameras=4)
    owner=Controller(tmp_path/'home')
    job=owner.start(settings(tmp_path,camera=str(camera),scene='missing.blend'))
    assert job.data['shared_bake_end']==33
