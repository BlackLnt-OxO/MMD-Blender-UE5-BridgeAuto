"""Render matched source-camera frames through the GUI's Controller actions."""
import os, sys, time
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtWidgets import QApplication
from mmd_bridge.app import configure_app
from mmd_bridge.controller import Controller
from mmd_bridge.core import read_json, atomic_json, file_hash
from mmd_bridge.scene_reference import compare
job_path=Path(sys.argv[1]).resolve();controller=Controller();job=controller.load_scene_results(job_path);job.verify_inputs()
source=Path(job.data['settings']['scene']);before=file_hash(source)
for kind in (() if '--finalize-only' in sys.argv else ('blender','ue')):
    controller.render_reference(kind,pipeline='lumen')
    previous=None
    while True:
        status=controller.poll()
        if status!=previous:print(kind,status['state'],status.get('message','')[:160],flush=True);previous=status
        if status['state'] in ('needs_review','failed','stopped') and not controller.processes and not controller.reference_busy():
            if status['state'] != 'needs_review':raise RuntimeError(status['message'])
            break
        time.sleep(2)
out=job.folder/'visual';app=QApplication.instance() or QApplication([]);configure_app(app)
comparison=compare(job.folder)
native=read_json(out/'ue_render.json');reference=read_json(out/'reference.json')
assert native['complete'] and native['frame']==reference['frame'] and native['resolution']==reference['resolution']
assert before==file_hash(source)
atomic_json(out/'program_validation.json',{'matched_frame':True,'frame':native['frame'],
    'resolution':native['resolution'],'source_unchanged':True,'ue_render_complete':True,
    'visual_status':'needs_review','comparison':str(comparison),'difference':read_json(out/'difference.json')})
print('MATCHED_REFERENCE_FRAMES_READY',flush=True)
