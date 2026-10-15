"""Verify a differently named master via JSON only and preserve generic selection."""
import sys
from pathlib import Path
import unreal as u
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mmd_bridge.core import read_json,atomic_json
from mmd_bridge.ue_materials import Materials
OUT=ROOT/'work/character_material_20261014/extension';fixture=read_json(OUT/'fixture.json')
report={'warnings':[],'assets':{},'failures':[],'checks':{},'material_routes':{}}
manager=Materials('/Game/CM_1014/Extension','cm_1014_extension',report,fixture['cfg'])
master=u.load_asset(fixture['profile']['parents'][0])
if not master:
 master=manager.create('M_Other',u.Material,u.MaterialFactoryNew());manager.material=master
 color=manager.node('TextureSampleParameter2D',parameter_name='ColorMap',texture=u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
 control=manager.node('TextureSampleParameter2D',parameter_name='ControlMap',texture=u.load_asset('/InterchangeAssets/gltf/Textures/T_White_Linear'),sampler_type=u.MaterialSamplerType.SAMPLERTYPE_LINEAR_COLOR)
 tint=manager.node('VectorParameter',parameter_name='ColorTint',default_value=u.LinearColor(1,1,1,1))
 result=manager.op('Multiply',(color[0],'RGB'),tint)
 manager.lib.connect_material_property(result[0],result[1],u.MaterialProperty.MP_BASE_COLOR)
 manager.lib.connect_material_property(control[0],'G',u.MaterialProperty.MP_ROUGHNESS)
 alpha=manager.scalar('Alpha',1);manager.lib.connect_material_property(alpha[0],alpha[1],u.MaterialProperty.MP_OPACITY)
 manager.lib.recompile_material(master);manager.managed(master);u.EditorAssetLibrary.save_loaded_asset(master);manager.protect_asset(master)
instance=manager.build_character(fixture['record'])
lib=u.MaterialEditingLibrary
texture=lib.get_material_instance_texture_parameter_value(instance,'ControlMap')
assert texture and not texture.srgb
same_image=fixture['record']['textures']['control']
color_texture=manager.texture(dict(same_image,color_space='sRGB'))
data_texture=manager.texture(dict(same_image,color_space='Non-Color'),data=True)
assert color_texture!=data_texture and color_texture.srgb and not data_texture.srgb
assert abs(lib.get_material_instance_scalar_parameter_value(instance,'Alpha')-.65)<1e-5
assert 'control' in report['material_routes']['extension']['assigned_parameters']
generic=dict(fixture['cfg'],character_material_profile='generic')
generic_manager=Materials('/Game/CM_1014/ExtensionGeneric','cm_1014_extension_generic',report,generic)
generic_instance=generic_manager.build_character(dict(fixture['record'],id='generic',asset_name='M_Generic'))
assert generic_instance.parent==master
assert u.EditorAssetLibrary.get_metadata_tag(generic_instance,'MMDBridge.MaterialRoute')=='character_master'
report.update(status='extension_passed',control_texture=texture.get_path_name(),expected_pixels=fixture['pixels'],
              generic_parent=generic_instance.parent.get_path_name())
atomic_json(OUT/'ue_receipt.json',report)
