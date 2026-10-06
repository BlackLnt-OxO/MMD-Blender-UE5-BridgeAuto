"""Independent source analysis, with cooperative stop and a separate receipt."""
import os,subprocess,time,uuid
from pathlib import Path
from .core import atomic_json,read_json,resource_root


def analyze_source(blender,source,output,frame=1,cancel=None):
    executable=Path(blender);source=Path(source)
    if not executable.is_file() or source.suffix.lower()!='.blend' or not source.is_file():
        raise ValueError('请选择有效的 Blender 程序与 .blend 场景')
    folder=Path(output)/('MaterialAnalysis_'+uuid.uuid4().hex[:12]);folder.mkdir(parents=True)
    request={'source':str(source.resolve()),'frame':frame,'report':str(folder/'report.json'),'stop':str(folder/'stop')}
    atomic_json(folder/'request.json',request)
    command=[str(executable),'--background','--factory-startup','--disable-autoexec','--python',
             str(resource_root()/'scripts/blender_analyze_materials.py'),'--',str(folder/'request.json')]
    with (folder/'blender.log').open('w',encoding='utf-8') as log:
        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,
                               creationflags=0x08000000 if os.name=='nt' else 0)
        stopped_at=None
        while child.poll() is None:
            if cancel is not None and cancel.is_set():
                Path(request['stop']).touch(exist_ok=True)
                stopped_at=stopped_at or time.monotonic()
                if time.monotonic()-stopped_at>15:child.terminate();child.wait(timeout=10)
            time.sleep(.2)
        code=child.returncode
    if stopped_at and not Path(request['report']).is_file():
        atomic_json(request['report'],{'complete':False,'state':'stopped','materials':[]})
    if (code and not stopped_at) or not Path(request['report']).is_file():
        raise RuntimeError('材质分析失败，请查看 '+str(folder/'blender.log'))
    return {'folder':str(folder),'report':read_json(folder/'report.json')}
