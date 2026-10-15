"""Run isolated real-RHI imports and Lumen MRQ for the four approved PMX fixtures."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json,read_json
from mmd_bridge.ue_render_policy import standalone_command

PROJECT=Path(r'F:/Ue5_Project/TESTON_1005_Clean/TESTON_1005_Clean.uproject')
EXE=Path(r'C:/Program Files/Epic Games/UE_5.8/Engine/Binaries/Win64/UnrealEditor-Cmd.exe')
OUT=ROOT/'work/character_material_20261014'
models=sys.argv[1:] or ['Barbara','YYB GenZ Miku','NeuroSama','橘シェリー']
tag=os.environ.get('MMDBRIDGE_CHARACTER_TEST_TAG','full')
for model in models:
    folder=OUT/tag/model
    env=dict(os.environ,MMDBRIDGE_DISABLE_AUTOSTART='1',MMDBRIDGE_CHARACTER_TEST_TAG=tag,
             MMDBRIDGE_CHARACTER_TEST_MODEL=model)
    with (folder/'ue_import_console.log').open('w',encoding='utf8') as log:
        code=subprocess.call([str(EXE),str(PROJECT),'-run=pythonscript',
            '-script='+str(ROOT/'scripts/validate_character_materials_ue.py'),
            '-AllowCommandletRendering','-unattended','-nosound','-nop4',
            '-DisablePlugins=OSC,OpenXR,OpenXREditor',
            '-abslog='+str(folder/'ue_import.log')],env=env,stdout=log,stderr=subprocess.STDOUT)
    receipt=read_json(folder/'ue_receipt.json')
    assert code==0 and receipt['status']=='ue_assets_created',receipt
    run=folder/('render_'+datetime.now().strftime('%H%M%S')); run.mkdir()
    request=dict(receipt['render_assets'],run=str(run),project=str(PROJECT),output=str(run/'images'),
                 pipeline='lumen',frame=None,id='cm_1014_'+model,expected_resolution=[1280,720],capture_gpu_csv=True,
                 face_direction_probe=bool(receipt.get('sdf_textures')),render_vram_guard_enabled=True)
    request['character_animation_probe']=receipt.get('character_animation_probe',{})
    if receipt.get('eye_lighting_frames'):
        request['character_animation_probe']['light_intensities']=True
    atomic_json(run/'request.json',request)
    cfg={'project':str(PROJECT),'unreal':str(EXE)}
    command=standalone_command(cfg,{'assets':receipt['render_assets']},request['preset'],request=request,
                               allow_custom_preset=True)
    command.append('-abslog='+str(run/'ue_process.log'))
    env['MMDBRIDGE_STANDALONE_REQUEST']=str(run/'request.json')
    with (run/'launcher.log').open('w',encoding='utf8') as log:
        code=subprocess.call(command,env=env,stdout=log,stderr=subprocess.STDOUT)
    runtime=read_json(run/'runtime.json')
    if request['character_animation_probe']:
        samples=runtime.get('character_animation_samples',[])
        assert len(samples)>2 and all(s['independent_instances'] for s in samples),samples
        max_difference=max(abs(a-b) for s in samples for key in ('position','rotation') for a,b in zip(s['main'][key],s['outline'][key]))
        assert max_difference<.001,max_difference
        for name in request['character_animation_probe'].get('morphs',[]):
            assert max(abs(s['main']['morphs'][name]-s['outline']['morphs'][name]) for s in samples)<.001
            assert max(s['main']['morphs'][name] for s in samples)-min(s['main']['morphs'][name] for s in samples)>.5
        runtime['animation_pose_max_difference']=max_difference
        if receipt.get('eye_lighting_frames'):
            assert any(s.get('light_intensities')=={'directional':[0.0],'sky':[0.0]} for s in samples),'No actual zero-light sample captured'
    all_images=sorted((run/'images').rglob('*.png'))
    expected=receipt.get('render_frame_count',9)
    capture=receipt.get('capture_frames',list(range(9)))
    images=[all_images[i] for i in capture] if len(all_images)==expected else all_images
    text=(run/'ue_process.log').read_text(encoding='utf8',errors='replace')
    shader_failures=[line for line in text.splitlines() if any(s in line for s in
        ('Failed to compile Material','Skeleton == nullptr','missing usage flag','LoadErrors: While trying to load package /Game/CM_1014'))]
    atomic_json(folder/'acceptance.json',dict(exit_code=code,runtime=runtime,images=[str(p) for p in images],
                capture_frames=capture,render_frame_count=len(all_images),
                shader_failures=shader_failures,complete=code==0 and runtime.get('success') is True and len(all_images)==expected and len(images)==len(capture) and not shader_failures))
    assert code==0 and runtime.get('success') is True and len(all_images)==expected and len(images)==len(capture),(code,runtime,len(all_images))
    assert not shader_failures,shader_failures
    if model=='橘シェリー' and receipt.get('sdf_off_overrides'):
        baseline=run/'sdf_off';baseline.mkdir()
        off=dict(request,run=str(baseline),output=str(baseline/'images'),capture_gpu_csv=False,
                 material_overrides=receipt['sdf_off_overrides'])
        atomic_json(baseline/'request.json',off)
        env['MMDBRIDGE_STANDALONE_REQUEST']=str(baseline/'request.json')
        with (baseline/'launcher.log').open('w',encoding='utf8') as log:
            code=subprocess.call(command[:-1]+['-abslog='+str(baseline/'ue_process.log')],env=env,stdout=log,stderr=subprocess.STDOUT)
        state=read_json(baseline/'runtime.json')
        assert code==0 and state.get('success') is True and len(list((baseline/'images').rglob('*.png')))==expected,state
    print('CHARACTER_MRQ_COMPLETE '+model,flush=True)
