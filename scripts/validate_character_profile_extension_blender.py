"""Generate a distinct shader contract using production source discovery and packing."""
import sys
from pathlib import Path
from types import SimpleNamespace
import bpy
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import atomic_json,read_json
from mmd_bridge.blender_character_materials import save_data_image,prepare,material_alpha_samples,pack_texture
from mmd_bridge.character_profiles import load_profiles
OUT=ROOT/'work/character_material_20261014/extension';OUT.mkdir(exist_ok=True)
profile={'version':1,'id':'Reordered_Test','parents':['/Game/CM_1014/Extension/Materials/M_Other'],
 'required':['ColorMap','ControlMap','ColorTint','Alpha'],
 'textures':{'base_color':{'parameter':'ColorMap','color_space':'srgb'},
             'control':{'parameter':'ControlMap','color_space':'data'}},
 'vectors':{'diffuse_color':{'parameter':'ColorTint'}},'scalars':{'opacity':{'parameter':'Alpha'}},
 'role_presets':{'cloth_1':{'scalars':{'Alpha':.65}}},
 'packed_textures':{'control':{'channels':{'r':{'source':'ao','channel':'a'},
    'g':{'source':'ao','channel':'g','invert':True},'b':{'value':.4},'a':{'value':1}}}},
 'source_rules':[{'semantic':'ao','pattern':'Control_AO\\.png'}],'sdf':{'format':'none'}}
profiles=OUT/'profiles';profiles.mkdir(exist_ok=True);atomic_json(profiles/'other.json',profile)
save_data_image(OUT/'Control_AO.png',np.tile([.1,.2,.3,.7],(4,4,1)),4,4)
(OUT/'fixture.pmx').touch()
record={'id':'extension','name':'body','source_name':'body','asset_name':'M_Extension',
        'pmx_index':0,'textures':{},'mmd':{'diffuse_color':[.5,.7,1.], 'diffuse_alpha':.8}}
class Images:
 records={'extension':record}
 def image(self,image): return {'id':'ao','path':image.filepath,'name':'Control_AO','asset_name':'T_Control_AO'}
worker=SimpleNamespace(cfg={'model':str(OUT/'fixture.pmx'),'character_master_material':profile['parents'][0],
 'eye_master_material':'','character_material_profile':profile['id'],'character_material_profiles_dir':str(profiles)},
 data={'settings':{'model':str(OUT/'fixture.pmx')}},materials=Images(),manifest={'bones':[]},bundle=OUT,meshes=[])
prepare(worker)
image=bpy.data.images.load(record['textures']['control']['path'],check_existing=False)
rgba=np.array(image.pixels[:4]);assert np.allclose(rgba,[.7,.8,.4,1],atol=1/255),rgba
atomic_json(OUT/'fixture.json',{'profile':profile,'record':record,'cfg':worker.cfg,'pixels':rgba.tolist()})
print('EXTENSION_PACK_CHECK_OK',rgba)
mesh=bpy.data.meshes.new('AlphaUVTest');mesh.from_pydata([(0,0,0),(1,0,0),(1,1,0),(0,1,0)],[],[(0,1,2,3)])
mat=bpy.data.materials.new('AlphaUVTest');mesh.materials.append(mat)
uv=mesh.uv_layers.new()
for loop in mesh.loops:
    point=mesh.vertices[loop.vertex_index].co;uv.data[loop.index].uv=(point.x*.5,point.y)
obj=bpy.data.objects.new('AlphaUVTest',mesh)
test=SimpleNamespace(meshes=[obj]);record={'source_name':mat.name,'asset_name':mat.name}
image=bpy.data.images.new('AlphaUVTest',4,4)
alpha=np.ones((4,4),np.float32);alpha[:,2:]=.3
assert np.all(material_alpha_samples(test,record,image,alpha)==1)
for point in uv.data: point.uv.x+=.5
assert np.allclose(material_alpha_samples(test,record,image,alpha),.3)
for point in uv.data: point.uv.x+=1
assert np.allclose(material_alpha_samples(test,record,image,alpha),.3)
print('ALPHA_USED_UV_CHECK_OK')
contract=load_profiles(ROOT)['ZZZ_Render_PMX']
for role in ('skin','cloth_1','cloth_2','cloth_3'):
    record={'character_material':{'role':role},'mmd':{'diffuse_alpha':.3,'sphere_texture_type':'1'},'textures':{'sphere':{'path':'fixture'}}}
    spec=pack_texture(OUT,'ramp_'+role,contract['packed_textures']['lightmap'],record,contract)
    image=bpy.data.images.load(spec['path'],check_existing=False);image.colorspace_settings.name='Non-Color'
    assert np.allclose(image.pixels[:4],[contract['region_codes'][role],1,0,1],atol=1/255)
    bpy.data.images.remove(image)
print('FOUR_RAMPS_AND_LIGHTMAP_ALPHA_CHECK_OK')
