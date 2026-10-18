"""Build continuous hosiery coverage with matched v7 and intermediate values."""
from pathlib import Path
import runpy
import unreal as u
ROOT=Path(__file__).resolve().parents[1]
state=runpy.run_path(str(ROOT/'scripts/refine_character_pbr_textiles_ue.py'))
manager,report,folder,manifest=[state[k] for k in ('manager','report','folder','manifest')]
from mmd_bridge.core import read_json,atomic_json
try:
    previous=read_json(ROOT/'work/character_pbr_20261018/FabricRosy_v7/ue_receipt.json')
    thick_before={}
    report['hosiery_thickness_variants']={}
    for label,value in (('zero',0.),('thin',.35),('intermediate_065',.65),
                        ('medium',1.),('intermediate_150',1.5),('thick',2.5)):
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
            actual=manager.lib.get_material_instance_scalar_parameter_value(instance,'HosieryThickness')
            assert abs(actual-value)<1e-5
            mapping[material.get_path_name()]=instance.get_path_name()
            old=previous['assets']['materials'][record['id']]
            thick_before[material.get_path_name()]=previous['hosiery_thickness_variants']['thick']['overrides'][old]
        atomic_json(folder/('hosiery_'+label+'.json'),mapping)
        report['hosiery_thickness_variants'][label]={'value':value,'overrides':mapping}
    atomic_json(folder/'before_v7_thick.json',thick_before)
    report['before_v7_thick_overrides']=thick_before
    print('PBR_HOSIERY_CONTINUOUS_READY',len(report['hosiery_thickness_variants']))
except Exception:
    import traceback
    report['status']='failed';report['error']=traceback.format_exc();raise
finally:atomic_json(folder/'ue_receipt.json',report)
