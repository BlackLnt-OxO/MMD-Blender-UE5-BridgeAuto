"""Real PMX material/rig fixture; run with the configured Blender binary."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'vendor/blender_mmd_tools-4.5.13'))
sys.path.insert(0,str(ROOT/'vendor/python'))
sys.path.insert(0,str(ROOT))

import bpy
import numpy as np
from mmd_bridge.blender_host import enable_mmd, select, FBX_DEFAULTS
from mmd_bridge.blender_materials import Materials
from mmd_bridge.blender_character_materials import prepare, face_uv_coverage, material_alpha_samples
from mmd_bridge.character_profiles import load_profiles
from mmd_bridge.face_sdf import (signed_distance, threshold_from_distances, smooth_threshold,
                                 encode_threshold, nose_cutouts, cheek_triangle_cutouts, apply_lit_cutout)
from mmd_bridge.core import atomic_json, stable_id, file_hash

OUT = ROOT/'work/character_material_20261014'
args = sys.argv[sys.argv.index('--')+1:] if '--' in sys.argv else []
tag = args[0] if args else 'smoke'
tile = int(args[1]) if len(args)>1 else 64
chosen = args[2] if len(args)>2 else 'Barbara'

test_mesh=bpy.data.meshes.new('AlphaCollapsedUV')
test_mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0)],[],[(0,1,2)])
test_uv=test_mesh.uv_layers.new()
for item in test_uv.data:item.uv=(.25,.25)
coverage,_=face_uv_coverage(test_mesh,list(test_mesh.polygons),32,check_overlap=False,check_bounds=False)
assert coverage.sum()==1
try:face_uv_coverage(test_mesh,list(test_mesh.polygons),32)
except ValueError:pass
else:raise AssertionError('SDF must still reject collapsed UVs')
test_mat=bpy.data.materials.new('AlphaCollapsedUV')
test_mesh.materials.append(test_mat)
test_obj=bpy.data.objects.new('AlphaCollapsedUV',test_mesh)
test_alpha=np.zeros((4,4),np.float32);test_alpha[1,1]=1
test_samples=material_alpha_samples(SimpleNamespace(meshes=[test_obj]),{'source_name':test_mat.name,'asset_name':''},SimpleNamespace(size=(4,4)),test_alpha)
assert test_samples.tolist()==[1.]
bpy.data.objects.remove(test_obj);bpy.data.meshes.remove(test_mesh);bpy.data.materials.remove(test_mat)

mask = np.zeros((7,8),bool); mask[2:5,2:6] = True
field = signed_distance(mask)
assert np.all(field[mask]>0) and np.all(field[~mask]<0)
for y in range(7):
    for x in range(8):
        target = np.argwhere(mask != mask[y,x])
        expected = np.sqrt(((target-np.array([y,x]))**2).sum(axis=1).min())
        assert abs(abs(field[y,x])-expected)<1e-5
assert np.isfinite(signed_distance(np.ones((3,4),bool))).all()
assert signed_distance(np.zeros((3,4),bool)).max()<0
angles=np.array([0,.5,1],np.float32)
fields=np.array([[[2,1,-2,2]],[[1,-1,-1,1]],[[-3,-2,2,1]]],np.float32)
threshold,relit=threshold_from_distances(fields,angles)
assert np.allclose(threshold,[[.625,.25,0,1]])
assert relit.tolist()==[[False,False,True,False]]
for invalid in ([0,.5,.5],[.1,.5,1]):
    try: threshold_from_distances(fields,invalid)
    except ValueError: pass
    else: raise AssertionError('invalid angle sequence accepted')
values=np.full((19,19),.6,np.float32);coverage=np.ones_like(values,dtype=bool);coverage[7:12,7:12]=False
filtered=smooth_threshold(values,coverage,2.)
assert np.allclose(filtered[coverage],.6,atol=1e-6)
encoded=encode_threshold(np.linspace(0,1,1001),8.)
assert np.all(np.diff(encoded)>0) and np.allclose(encoded[[0,500,1000]],[0,.5,1],atol=1e-6)
assert encoded[250]<.02 and encoded[750]>.98
test=np.random.default_rng(0).random((100,2)).astype(np.float32)
assert np.array_equal(test[:,0]>test[:,1],encode_threshold(test[:,0],8.)>encode_threshold(test[:,1],8.))
assert np.allclose(apply_lit_cutout(np.array([[.2,.2,.2]]),np.array([[0,.5,1]]),.75),[[.2,.475,.75]])
xx,zz=np.meshgrid(np.linspace(-1,1,128),np.linspace(0,2.5,128))
yy=-.25*np.exp(-(xx/.16)**2-((zz-.75)/.2)**2)
positions=np.stack((xx,yy,zz),axis=2);coverage=np.ones(xx.shape,dtype=bool)
right,left,info=nose_cutouts(positions,coverage)
assert info['status']=='generated' and right.max()>.9 and left.max()>.9
assert ((right>0)|(left>0)).sum()<coverage.sum()*.01
triangle_anchor=dict(info,position=[0,info['position'][1],info['position'][2]])
triangle_right,triangle_left,triangle_info=cheek_triangle_cutouts(positions,coverage,triangle_anchor)
assert triangle_info['status']=='generated' and triangle_right.max()>.9 and triangle_left.max()>.9
assert np.allclose(triangle_right,np.fliplr(triangle_left),atol=1e-5)
assert (triangle_right>.5).sum()>(right>.5).sum()*2
cutout=apply_lit_cutout(np.full(xx.shape,.4),triangle_right,98/180)
assert (cutout[triangle_right>.99]>.5).all() and (cutout[triangle_right>.99]<.65).all()
assert .65<encode_threshold(np.array([98/180]),8.)[0]<.69
assert np.all(cutout[triangle_right==0]==np.float32(.4))
try: cheek_triangle_cutouts(positions,coverage,{'position':[float('nan'),0,0]})
except ValueError: pass
else: raise AssertionError('invalid triangle anchor accepted')
positions[...,1]=0
try: nose_cutouts(positions,coverage)
except ValueError: pass
else: raise AssertionError('flat surface wrongly accepted as nose')

profile = load_profiles(ROOT)['ZZZ_Render_PMX']
if tile != 512:
    profile['id']='ZZZ_Render_PMX_'+tag
    profile['sdf']['tile_size']=tile; profile['sdf']['samples']=4
    profile_dir=OUT/tag/'profiles'; profile_dir.mkdir(parents=True,exist_ok=True)
    atomic_json(profile_dir/(profile['id']+'.json'),profile)
else:
    profile_dir=''
samples_path=Path(args[3]) if len(args)>3 else ROOT/'work/bp_render_material_audit_20261013/pmx_samples.json'
samples=json.loads(samples_path.read_text(encoding='utf-8'))
selected=[s for s in samples if s['name']==chosen]
assert len(selected)==1, chosen
sample=selected[0]
enable_mmd()
bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene; scene.unit_settings.system='METRIC'; scene.unit_settings.scale_length=1
scene.render.fps=30; scene.frame_start=1; scene.frame_end=3; scene.frame_set(1)
result=bpy.ops.mmd_tools.import_model(filepath=sample['model'],scale=.08,
    types={'MESH','ARMATURE','MORPHS','DISPLAY'},rename_bones=False,
    clean_model=False,remove_doubles=False,save_log=False,log_level='WARNING')
assert 'FINISHED' in result
armature=next(o for o in scene.objects if o.type=='ARMATURE')
meshes=[o for o in scene.objects if o.type=='MESH' and o.find_armature()==armature]
folder=OUT/tag/chosen; folder.mkdir(parents=True,exist_ok=True)

class ReceiptJob:
    def __init__(self): self.folder=folder
    def status(self,state,message):
        atomic_json(folder/'status.json',{'state':state,'message':message})
        print(message,flush=True)

worker=SimpleNamespace(cfg={'model':sample['model'], 'character_master_material':profile['parents'][0],
    'eye_master_material':'','character_material_profile':profile['id'],
    'character_material_profiles_dir':str(profile_dir)},
    data={'settings':{'model':sample['model']}}, bundle=folder/'Data', job=ReceiptJob(),
    manifest={'bones':[]},meshes=meshes,armature=armature)
worker.bundle.mkdir(exist_ok=True)
worker.materials=Materials(worker.bundle,sample['model'])
for i,bone in enumerate(list(armature.data.bones)):
    original=bone.name; bone.name=f'b_{i:04d}_{stable_id(original)[:8]}'
    worker.manifest['bones'].append({'original':original,'exported':bone.name})
for obj in meshes:
    for i,mat in enumerate(obj.data.materials):
        if mat: worker.materials.analyze(mat,pmx_index=i)
prepare(worker)
records=list(worker.materials.records.values())
copy_spec=next(r['textures']['base_color'] for r in records if r.get('textures',{}).get('base_color'))
copy_image=bpy.data.images.load(copy_spec['path'],check_existing=False)
copy_materials=Materials(folder/'ImageCopyCheck',sample['model'])
copied=copy_materials.image(copy_image)
import stat
copy_target=Path(copied['path'])
assert copy_target.stat().st_mode & stat.S_IWRITE
copy_target.chmod(stat.S_IREAD)
try:
    reused=Materials(folder/'ImageCopyCheck',sample['model']).image(copy_image)
    assert file_hash(reused['path'])==file_hash(copy_spec['path'])
finally:
    copy_target.chmod(stat.S_IREAD|stat.S_IWRITE)
    bpy.data.images.remove(copy_image)
if profile['sdf']['format']=='none':
    adapted=[r for r in records if r.get('character_material')]
    assert adapted and all(r['character_material']['sdf']['format']=='none' for r in adapted)
    assert all(not set(r.get('textures',{}))&{'face_sdf','sdf_atlas','sdf_coverage'} for r in adapted)
    assert all({'lightmap','mn'}<=set(r['textures']) for r in adapted)
atomic_json(folder/'material_diagnostics.json',records)
morph_probe=[]
for obj in meshes:
    if obj.data.shape_keys:
        for key in list(obj.data.shape_keys.key_blocks)[1:]:
            original=key.name;key.name='m_'+stable_id(key.name)
            if original.lower() in ('まばたき','blink','闭眼','閉眼'):
                morph_probe.append((key,original))
armature.name='MMD_Rig'
select(meshes+[armature])
model_path=folder/'model.fbx'
bpy.ops.export_scene.fbx(filepath=str(model_path),**{**FBX_DEFAULTS,'bake_anim':False,'object_types':{'MESH','ARMATURE'}})
head=next((b['exported'] for b in worker.manifest['bones'] if b['original'] in ('頭','头','Head','head')),None)
if head:
    pose=armature.pose.bones[head]; pose.rotation_mode='XYZ'
    for frame,rotation in ((1,(0,0,0)),(2,(0,.4,0)),(3,(.25,0,0))):
        pose.rotation_euler=rotation; pose.keyframe_insert('rotation_euler',frame=frame)
for key,original in morph_probe:
    key.driver_remove('value')
    for frame,value in ((1,0.),(2,1.),(3,0.)):
        key.value=value;key.keyframe_insert('value',frame=frame)
animation_path=folder/'animation.fbx'
bpy.ops.export_scene.fbx(filepath=str(animation_path),**{**FBX_DEFAULTS,'bake_anim':True,'object_types':{'MESH','ARMATURE'}})
manifest={'schema':2,'model':sample['model'],'name':chosen,'bones':worker.manifest['bones'],
          'morph_probe':[{'original':original,'exported':key.name} for key,original in morph_probe],
          'materials':records,'profile':profile,'profile_dir':str(profile_dir),
          'files':{k:{'path':str(p),'sha256':file_hash(p)} for k,p in (('model',model_path),('animation',animation_path))}}
atomic_json(folder/'manifest.json',manifest)
worker.job.status('blender_complete','真实 PMX 材质数据与 FBX 已导出')
print('CHARACTER_FIXTURE_COMPLETE',chosen,flush=True)
