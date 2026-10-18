"""Headless driver of the desktop program's normal Controller scene workflow."""
import argparse,time,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.controller import Controller
from mmd_bridge.core import Settings,read_json,atomic_json
parser=argparse.ArgumentParser();parser.add_argument('--settings');parser.add_argument('--receipt',required=True);parser.add_argument('--resume');parser.add_argument('--reconvert')
args=parser.parse_args();receipt=Path(args.receipt)
controller=Controller()
if args.reconvert:
    from mmd_bridge.core import Job
    controller.job=Job(args.reconvert)
    controller.reconvert_scene_materials(Settings(**controller.job.data['settings']))
    job=controller.job
elif args.resume:
    controller.resume(args.resume);job=controller.job
else:
    config=read_json(args.settings)
    config.pop('multi_character_mode',None)
    job=controller.start(Settings(**config))
atomic_json(receipt,{'job':str(job.folder),'state':'started','driver':'desktop Controller.start/poll'})
print('PROGRAM_JOB',job.folder,flush=True)
previous=None
while True:
    status=controller.poll()
    key=(status.get('state'),status.get('message'))
    if key!=previous:
        print(key,flush=True);previous=key
    atomic_json(receipt,{'job':str(job.folder),'status':status,'driver':'desktop Controller.start/poll','updated':time.time()})
    if status.get('state') in ('complete','needs_review','failed','stopped') and not controller.processes:
        break
    if status.get('state')=='ue_wait_restart' and controller._same_project_running(job.data['settings']) is False:
        controller.resume(job.folder)
    time.sleep(2)
for handle in controller.log_handles:handle.close()
print('PROGRAM_FINISHED',status.get('state'),flush=True)
