from pathlib import Path
from mmd_bridge.core import atomic_json,read_json
from mmd_bridge.standalone_render import source_fingerprint
from tests.test_standalone_render import job_fixture


def test_reused_module_asset_changes_invalidate_render_resume(tmp_path):
    job=job_fixture(tmp_path)
    child=job.folder/'modules/scene';child.mkdir(parents=True)
    atomic_json(child/'ue_report.json',{'project':job.data['settings']['project'],'root':'/Game/IndependentScene'})
    atomic_json(child/'manifest.json',{'scene':{}})
    atomic_json(job.folder/'manifest.json',{'composition':[{'job':str(child)}]})
    mesh=tmp_path/'Content/IndependentScene/Scene/Screen.uasset';mesh.parent.mkdir(parents=True);mesh.write_bytes(b'original slot')
    report=read_json(job.folder/'ue_report.json')
    request=dict(project=job.data['settings']['project'],level=report['assets']['level'],
                 sequence=report['assets']['sequence'],preset=report['assets']['render_preset'])
    before=source_fingerprint(job,request)
    mesh.write_bytes(b'authored LED slot')
    assert source_fingerprint(job,request)!=before
