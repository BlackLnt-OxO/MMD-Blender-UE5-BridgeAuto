"""Build the current candidate with matched v6 controls and thickness variants."""
from pathlib import Path
import runpy
import unreal as u
ROOT=Path(__file__).resolve().parents[1]
state=runpy.run_path(str(ROOT/'scripts/refine_character_pbr_textiles_ue.py'))
manager,report,folder,manifest=[state[k] for k in ('manager','report','folder','manifest')]
from mmd_bridge.core import read_json,atomic_json
try:
    previous=read_json(ROOT/'work/character_pbr_20261018/ClothHosiery_v6/ue_receipt.json')
    before={}
    for record in manifest['materials']:
        new=manager.asset(record['id']);old=previous['assets']['materials'][record['id']]
        assert u.load_asset(old),old
        before[new.get_path_name()]=old
    atomic_json(folder/'before_v6.json',before)
    report['before_v6_overrides']=before
    report['hosiery_thickness_variants']={}
    for label,value in (('thin',.35),('medium',1.),('thick',2.5)):
        mapping={}
        for record in manifest['materials']:
            if record['character_material']['pbr']['type']!='hosiery':continue
            material=manager.asset(record['id'])
            path=material.get_path_name().split('.')[0]+'_'+label
            instance=u.load_asset(path)
            if instance:manager.verify_asset(instance)
            else:instance=u.EditorAssetLibrary.duplicate_asset(material.get_path_name(),path)
            manager.managed(instance)
            manager.lib.set_material_instance_scalar_parameter_value(instance,'HosieryThickness',value)
            manager.lib.update_material_instance(instance)
            assert u.EditorAssetLibrary.save_loaded_asset(instance,only_if_is_dirty=False)
            manager.protect_asset(instance)
            mapping[material.get_path_name()]=instance.get_path_name()
        atomic_json(folder/('hosiery_'+label+'.json'),mapping)
        report['hosiery_thickness_variants'][label]={'value':value,'overrides':mapping}
    report['fabric_rosy_checks']=[]
    for record in manifest['materials']:
        kind=record['character_material']['pbr']['type']
        if kind not in ('skin','face','cloth','satin','hosiery'):continue
        material=manager.asset(record['id']);parent=material.get_editor_property('parent')
        names={str(n) for n in manager.lib.get_scalar_parameter_names(parent)}
        keys=('Roughness','Specular','Fuzz','FuzzBrightness','FabricRoughnessVariation',
              'SkinRosiness','SSSStrength','SSSDetailPreservation','WetEnabled','HosieryThickness',
              'HosieryCoverage','DetailTiling','EmissiveStrength')
        report['fabric_rosy_checks'].append({'name':record['name'],'type':kind,
            'params':{k:manager.lib.get_material_instance_scalar_parameter_value(material,k) for k in keys if k in names}})
    print('PBR_FABRIC_ROSY_READY',len(report['fabric_rosy_checks']))
except Exception:
    import traceback
    report['status']='failed';report['error']=traceback.format_exc();raise
finally:atomic_json(folder/'ue_receipt.json',report)
