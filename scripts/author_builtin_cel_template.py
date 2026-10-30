"""UE-only half of prepare_builtin_cel_template.py; operates on copied assets."""
from pathlib import Path
import json
import unreal as u

config_path = u.SystemLibrary.parse_command_line(u.SystemLibrary.get_command_line())[2]['CelAuthorConfig']
cfg = json.loads(Path(config_path).read_text(encoding='utf-8'))
here = Path(cfg['work'])
lib = u.MaterialEditingLibrary
version = cfg.get('version', 1)
prefix = cfg.get('asset_prefix', '/Game/MMDBridge/BuiltinCel/v1/')
roles = {}
for role, name in cfg['roles'].items():
    instance = u.load_asset('/Game/Character/Materials/NPR/'+name)
    assert instance, name
    values = {'source_instance': '/Game/Character/Materials/NPR/'+name, 'scalars': {}, 'vectors': {}, 'switches': {}}
    for typ, key in [('scalar', 'scalars'), ('vector', 'vectors'), ('static_switch', 'switches')]:
        for n in getattr(lib, 'get_'+typ+'_parameter_names')(instance):
            v = getattr(lib, 'get_material_instance_'+typ+'_parameter_value')(instance, n)
            values[key][str(n)] = [v.r, v.g, v.b, v.a] if key == 'vectors' else v
    roles[role] = values
assets = {rel: u.load_asset('/Game/'+rel) for rel in cfg['bundled']}
assert all(assets.values())
renames = [u.AssetRenameData(a, (prefix+rel).rsplit('/', 1)[0], rel.rsplit('/', 1)[1]) for rel, a in assets.items()]
assert u.AssetToolsHelpers.get_asset_tools().rename_assets(renames)
for rel, a in assets.items():
    if isinstance(a, u.Material):
        if rel == 'Character/Materials/Master/M_NPR':
            a.set_editor_property('translucency_pass', u.MaterialTranslucencyPass.MTP_BEFORE_DOF)
        for node in lib.get_material_expressions(a):
            if isinstance(node, u.MaterialExpressionStaticSwitchParameter):
                if str(node.get_editor_property('parameter_name')) in ('Brush?', '启用边缘光'):
                    node.set_editor_property('default_value', False)
            if isinstance(node, u.MaterialExpressionTextureSampleParameter2D):
                name = str(node.get_editor_property('parameter_name'))
                if name == 'BaseTex':
                    node.set_editor_property('texture', u.load_asset('/Engine/EngineResources/WhiteSquareTexture'))
                elif name == 'MatCapMap':
                    node.set_editor_property('texture', u.load_asset('/Engine/EngineResources/Black'))
        for usage in (u.MaterialUsage.MATUSAGE_SKELETAL_MESH, u.MaterialUsage.MATUSAGE_MORPH_TARGETS):
            lib.set_material_usage(a, usage)
        lib.recompile_material(a)
    assert u.EditorAssetLibrary.save_loaded_asset(a, only_if_is_dirty=False), rel
    assert a.get_path_name().startswith(prefix), a.get_path_name()
(here/'reference_parameters.json').write_text(json.dumps({'version': version, 'roles': roles}, ensure_ascii=False, indent=2), encoding='utf-8')
assert assets['Character/Materials/Master/M_NPR'].get_editor_property('translucency_pass') == u.MaterialTranslucencyPass.MTP_BEFORE_DOF
(here/'native_receipt.json').write_text(json.dumps({'version': version, 'asset_prefix': prefix,
    'filter_translucency_pass': 'BeforeDOF', 'assets': {k: a.get_path_name() for k, a in assets.items()},
    'roles': list(roles)}, indent=2), encoding='utf-8')
print('CEL_TEMPLATE_NATIVE_READY', len(assets))
