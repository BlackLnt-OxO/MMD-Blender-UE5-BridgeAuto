"""Only new validation assets; actual UE function graphs, compilation and reload."""
import sys,traceback,os,json
from pathlib import Path
ROOT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork')
sys.path.insert(0,str(ROOT))
import unreal as u
from mmd_bridge.core import read_json,atomic_json,file_hash
from mmd_bridge.ue_materials import Materials
from mmd_bridge.process import render_memory_info

out=ROOT/'work/material_functions_implementation'
report={'warnings':[],'complete':False,'project':u.Paths.get_project_file_path(),'engine':u.SystemLibrary.get_engine_version()}
try:
    assert Path(u.Paths.get_project_file_path()).stem=='TESTON1_1005_02',report['project']
    report['input_types']=[str(v) for v in u.FunctionInputType]
    assert u.SystemLibrary.get_console_variable_int_value('r.Substrate'), 'Substrate must be enabled for shader functions'
    converter=Materials('/Game/MMDBridgeValidation/FunctionsV2/A_fixture','FunctionValidation',report,
                        {'restore_scene_appearance':True,'use_scene_master_material':False})
    folders=['blender_fixture','blender_railway']
    if (out/'blender_tree/report.json').is_file():folders.append('blender_tree')
    for folder in folders:
        for record in read_json(out/folder/'report.json')['materials']:
            instance=converter.build(record,out/folder)
            material=instance.get_editor_property('parent')
            front=u.MaterialEditingLibrary.get_material_property_input_node(material,u.MaterialProperty.MP_FRONT_MATERIAL)
            assert isinstance(front,u.MaterialExpressionMaterialFunctionCall), type(front)
            functions=[]
            visited=set()
            def visit(asset):
                if asset.get_path_name() in visited:return
                visited.add(asset.get_path_name());converter.verify_asset(asset)
                assert isinstance(asset,u.MaterialFunction)
                expressions=u.MaterialEditingLibrary.get_material_function_expressions(asset)
                assert any(isinstance(e,u.MaterialExpressionFunctionOutput) for e in expressions)
                functions.append({'path':asset.get_path_name(),'expressions':len(expressions)})
                for expr in expressions:
                    if isinstance(expr,u.MaterialExpressionMaterialFunctionCall):visit(expr.get_editor_property('material_function'))
            visit(front.get_editor_property('material_function'))
            assert functions
            values=u.MaterialEditingLibrary.get_scalar_parameter_names(material)
            assert values, 'Preset inputs must remain editable'
            assert converter.reuse(record),record['name']
            report.setdefault('materials',[]).append({'name':record['name'],'path':material.get_path_name(),'functions':functions,
                'parameters':[str(v) for v in values]})
            report['memory']=render_memory_info()
    assert report['materials'][0]['path']==report['materials'][1]['path'],'Identical topology must share a parent'
    report.update(complete=True,source_sha256=file_hash(ROOT/'mmd_bridge/ue_materials.py'))
except Exception:
    report.update(error=traceback.format_exc());u.log_error(report['error'])
atomic_json(out/'ue_result.json',report)
u.log('FUNCTION_NATIVE_RESULT='+json.dumps({'complete':report['complete'],'error':report.get('error')},ensure_ascii=False))
