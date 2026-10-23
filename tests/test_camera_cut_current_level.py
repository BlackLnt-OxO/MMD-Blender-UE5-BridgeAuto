import ast,contextlib,os,sys,traceback
from pathlib import Path
from types import SimpleNamespace as N
import pytest
from mmd_bridge.core import Job,atomic_json,read_json


@pytest.mark.parametrize('mode',['probe','detect','bake','verify'])
def test_camera_cut_bridge_scopes_work_to_current_level(tmp_path,mode):
    source=Path('mmd_bridge/ue_host.py')
    tree=ast.parse(source.read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if getattr(n,'name',None) in ('_CameraCutTee','run_camera_cut_request')]
    script=tmp_path/'ue_camera_cut_baker.py'
    script.write_text("""import json,os
from pathlib import Path
cfg=json.loads(Path(os.environ['CAMCUT_CONFIG']).read_text(encoding='utf-8'))
name='probe.json' if cfg['MODE']=='probe' else cfg['MODE']+'_native.json'
(Path(cfg['REPORT_DIR'])/name).write_text(json.dumps({'verification':{'pass':True},'config':cfg}),encoding='utf-8')
""",encoding='utf-8')
    world=N(get_path_name=lambda:'/Game/CurrentSession/Map.Map')
    native=N(UnrealEditorSubsystem=object(),get_editor_subsystem=lambda _:N(get_editor_world=lambda:world))
    scope=dict(__file__=str(tmp_path/'ue_host.py'),Path=Path,Job=Job,atomic_json=atomic_json,
               read_json=read_json,os=os,sys=sys,contextlib=contextlib,traceback=traceback,u=native)
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),scope)
    session=tmp_path/'session'
    scope['run_camera_cut_request']({'session':str(session),'mode':mode,'cut_frames':[20]})
    assert read_json(session/'status.json')['state']=='camera_cut_complete'
    result=read_json(session/('probe.json' if mode=='probe' else mode+'_native.json'))
    assert result['config'].get('SOURCE_LEVEL')==(None if mode=='probe' else '/Game/CurrentSession/Map')
    assert result['config']['CUT_FRAMES']==[20]
