"""Native file parity, one-copy storage, cache reuse and no-file constants."""
import sys, json
from pathlib import Path
import bpy, numpy as np, OpenImageIO as oiio
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from mmd_bridge.blender_materials import Materials, write_bake_image
from mmd_bridge.core import atomic_json
OUT=ROOT/'work/bake_storage_20261004'/bpy.app.version_string.replace(' ','_')
OUT.mkdir(parents=True,exist_ok=True)
bpy.ops.wm.read_factory_settings(use_empty=True)
image=bpy.data.images.new('Signed HDR regression',512,512,alpha=True,float_buffer=True)
image.colorspace_settings.name='Non-Color'
y,x=np.mgrid[0:512,0:512]
expected=np.stack([x/71.-3,y/127.-2,(x+y)/13.,np.ones_like(x)],axis=-1).astype(np.float32)
image.pixels.foreach_set(expected.ravel())
image.filepath_raw=str(OUT/'native_raw.exr');image.file_format='OPEN_EXR';image.save()
write_bake_image(image,OUT/'compressed.exr',True)
def load(path):
    inp=oiio.ImageInput.open(str(path));spec=inp.spec();pixels=inp.read_image(format=oiio.FLOAT);inp.close()
    return pixels,spec
native,_=load(OUT/'native_raw.exr');compressed,spec=load(OUT/'compressed.exr')
assert np.array_equal(native,compressed),float(np.max(np.abs(native-compressed)))
assert np.array_equal(expected[::-1],compressed)
assert spec.get_string_attribute('compression')=='zip',spec.to_xml()
assert all(str(t)=='float' for t in (spec.channelformats or [spec.format]))
raw_size=(OUT/'native_raw.exr').stat().st_size;zip_size=(OUT/'compressed.exr').stat().st_size
assert zip_size<raw_size*.3,(raw_size,zip_size)
bpy.data.images.remove(image)
checks=[{'case':'signed_float32_zip','pixel_error':0,'raw_bytes':raw_size,'zip_bytes':zip_size}]
bpy.ops.mesh.primitive_plane_add(size=2);obj=bpy.context.object
mat=bpy.data.materials.new('Native field');mat.use_nodes=True;obj.data.materials.append(mat)
noise=mat.node_tree.nodes.new('ShaderNodeTexNoise');noise.inputs['Scale'].default_value=4
converter=Materials(OUT/'field',str(Path(__file__).resolve()),resolution=32)
converter.surface_policy={'category':'small'};converter.isolated_baking=True
image_count=len(bpy.data.images)
first=converter.bake(obj,mat,None,'field_noise','reuse-test',from_output=noise.outputs['Fac'])
second=converter.bake(obj,mat,None,'field_noise','reuse-test',from_output=noise.outputs['Fac'])
assert first['image']==second['image']
assert converter.bake_stats['written']==1 and converter.bake_stats['reused']==1,converter.bake_stats
assert len(list((OUT/'field/Textures').iterdir()))==1
assert len(bpy.data.images)==image_count,(image_count,len(bpy.data.images))
checks.append({'case':'one_copy_reuse','stats':dict(converter.bake_stats),'image_count_stable':True})
noise.inputs['Scale'].default_value=7
changed=converter.bake(obj,mat,None,'field_noise','reuse-test',from_output=noise.outputs['Fac'])
assert converter.bake_stats['written']==2,converter.bake_stats
assert len(list((OUT/'field/Textures').iterdir()))==1
checks.append({'case':'changed_source_node_invalidates_cache','written':converter.bake_stats['written']})
random=mat.node_tree.nodes.new('ShaderNodeObjectInfo')
sample=Materials(OUT/'constant',str(Path(__file__).resolve()),resolution=32)
sample.surface_policy={'category':'small'};sample.isolated_baking=True
record={'warnings':[],'baked_channels':[]}
value=sample.procedural_field(random.outputs['Random'],mat,obj,'uniform-cache',record)
assert value['op']=='constant' and not list((OUT/'constant/Textures').iterdir()),value
sample.field_cache={}
again=sample.procedural_field(random.outputs['Random'],mat,obj,'uniform-cache',record)
assert value==again and sample.bake_stats['constant_samples']==1 and sample.bake_stats['constant_reused']==1,sample.bake_stats
assert len(bpy.data.images)==image_count
checks.append({'case':'constant_without_texture','stats':sample.bake_stats})
group=bpy.data.node_groups.new('Anonymous constant group','ShaderNodeTree')
group.interface.new_socket(name='Result',in_out='OUTPUT',socket_type='NodeSocketFloat')
group_output=group.nodes.new('NodeGroupOutput');operation=group.nodes.new('ShaderNodeMath')
operation.operation='LOGARITHM';operation.inputs[0].default_value=8;operation.inputs[1].default_value=2
group.links.new(operation.outputs[0],group_output.inputs['Result'])
instance=mat.node_tree.nodes.new('ShaderNodeGroup');instance.node_tree=group
group_sample=Materials(OUT/'group_constant',str(Path(__file__).resolve()),resolution=32)
group_sample.surface_policy={'category':'small'};group_sample.isolated_baking=True;group_sample.group_stack=[instance]
record={'warnings':[],'baked_channels':[]}
first=group_sample.procedural_field(operation.outputs[0],mat,obj,'group-uniform-cache',record)
group_sample.field_cache={}
again=group_sample.procedural_field(operation.outputs[0],mat,obj,'group-uniform-cache',record)
assert first==again and group_sample.bake_stats['constant_samples']==1 and group_sample.bake_stats['constant_reused']==1,group_sample.bake_stats
assert not list((OUT/'group_constant/Textures').iterdir())
checks.append({'case':'nested_group_temporary_socket_does_not_break_reuse','stats':group_sample.bake_stats})
atomic_json(OUT/'result.json',{'complete':True,'version':bpy.app.version_string,'checks':checks})
print('BAKE_STORAGE_NATIVE_PASS',json.dumps(checks),flush=True)
