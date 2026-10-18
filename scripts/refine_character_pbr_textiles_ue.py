"""Keep v5 intact and add clothing/hosiery views and matched v5 overrides."""
from pathlib import Path
import runpy
import unreal as u

ROOT = Path(__file__).resolve().parents[1]
state = runpy.run_path(str(ROOT/'scripts/refine_character_pbr_skin_ue.py'))
manager, report, root, folder, manifest = [state[k] for k in ('manager', 'report', 'root', 'folder', 'manifest')]
new_camera, static_sequence, center = [state[k] for k in ('new_camera', 'static_sequence', 'center')]
from mmd_bridge.core import atomic_json, read_json

try:
    previous = read_json(ROOT/'work/character_pbr_20261018/SkinWet_v5/ue_receipt.json')
    report['v5_textile_overrides'], report['textile_checks'] = {}, []
    for record in manifest['materials']:
        pbr = record['character_material']['pbr']
        if pbr['type'] not in ('cloth', 'satin', 'hosiery'): continue
        material = manager.asset(record['id'])
        old = previous['assets']['materials'][record['id']]
        assert u.load_asset(old), old
        report['v5_textile_overrides'][material.get_path_name()] = old
        parent = material.get_editor_property('parent')
        scalars = {str(n) for n in manager.lib.get_scalar_parameter_names(parent)}
        keys = ('Roughness', 'Specular', 'DetailStrength', 'DetailTiling', 'Fuzz', 'Anisotropy',
                'FabricDirection', 'HosieryCoverage', 'HosieryEdgeDensity', 'Opacity', 'EmissiveStrength')
        report['textile_checks'].append({'name':record['name'], 'pmx_index':record['pmx_index'],
            'type':pbr['type'], 'material':material.get_path_name(),
            'shading_model':str(parent.get_editor_property('shading_model')),
            'blend_mode':str(parent.get_editor_property('blend_mode')),
            'normal':manager.lib.get_material_instance_texture_parameter_value(material, 'detail_normal_Tex').get_path_name(),
            'params':{k:manager.lib.get_material_instance_scalar_parameter_value(material,k) for k in keys if k in scalars}})
    atomic_json(folder/'v5_textiles.json', report['v5_textile_overrides'])
    legs = new_camera('PBR_Hosiery', center+u.Vector(205, -28, -105), center+u.Vector(0, -3, -105), 60.)
    macro = new_camera('PBR_HosieryMacro', center+u.Vector(53, -12, -95), center+u.Vector(0, -7, -95), 70.)
    clothing = new_camera('PBR_Clothing', center+u.Vector(155, -12, -38), center+u.Vector(0, 0, -38), 50.)
    full = new_camera('PBR_FullBody', center+u.Vector(460, -15, -65), center+u.Vector(0, 0, -65), 50.)
    report['review_views'].update(hosiery=static_sequence('S_PBR_Hosiery', legs),
        hosiery_macro=static_sequence('S_PBR_HosieryMacro', macro),
        clothing=static_sequence('S_PBR_Clothing', clothing),
        full_body=static_sequence('S_PBR_FullBody', full))
    assert u.get_editor_subsystem(u.LevelEditorSubsystem).save_current_level()
    report['status'] = 'ue_assets_created'
    print('PBR_TEXTILES_READY', len(report['textile_checks']))
except Exception:
    import traceback
    report['status'] = 'failed'; report['error'] = traceback.format_exc()
    raise
finally:
    atomic_json(folder/'ue_receipt.json', report)
