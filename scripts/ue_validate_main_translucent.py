"""Native thin-sheet fixture: Masked, two-sided, independent coverage, legacy diffusion."""
import sys
import traceback
from pathlib import Path
import unreal as u

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mmd_bridge.core import read_json, atomic_json
from mmd_bridge.ue_materials import Materials

OUT = ROOT / 'work/material_restoration/main_translucent_native'
result = {'complete': False, 'materials': [], 'failures': [], 'scattering': []}


class TraceMaterials(Materials):
    def thin_transmission(self, slab, color):
        super().thin_transmission(slab, color)
        mode = slab[0].get_editor_property('sub_surface_type')
        assert mode == u.MaterialSubSurfaceType.MSS_DIFFUSION
        result['scattering'].append(str(mode))


try:
    builder = TraceMaterials('/Game/MMDBridgeValidation/MainTranslucentNative_20261007',
        'MainTranslucentNative_20261007', result, {'restore_scene_appearance': True,
            'material_workflow': 'legacy_main_v1', 'material_conversion_timeout': 120})
    for record in read_json(OUT / 'records.json'):
        instance = builder.build(record, OUT)
        parent = instance.get_editor_property('parent')
        assert parent.get_editor_property('blend_mode') == u.BlendMode.BLEND_MASKED
        assert parent.get_editor_property('two_sided')
        assert u.MaterialEditingLibrary.get_material_property_input_node(parent, u.MaterialProperty.MP_OPACITY_MASK)
        assert u.MaterialEditingLibrary.get_material_property_input_node(parent, u.MaterialProperty.MP_FRONT_MATERIAL)
        assert u.EditorAssetLibrary.get_metadata_tag(parent, 'MMDBridge.ThinSurfaceScattering') == 'legacy_diffusion'
        assert builder.reuse(record)
        result['materials'].append({'name': record['name'], 'instance': instance.get_path_name(),
            'parent': parent.get_path_name(), 'masked': True, 'two_sided': True,
            'opacity_mask_connected': True, 'front_material_connected': True,
            'legacy_diffusion': True, 'saved_reload_verified': True})
    assert result['scattering'], 'new fixture must exercise the native diffusion helper'
    result['complete'] = True
except Exception:
    result['failures'].append(traceback.format_exc())
    u.log_error(result['failures'][-1])
atomic_json(OUT / 'ue_result.json', result)
print('MAIN_TRANSLUCENT_UE_PASS', result['complete'], len(result['materials']), flush=True)
