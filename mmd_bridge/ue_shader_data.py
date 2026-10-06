"""Restore source object/instance data whenever USD regenerates components."""
from pathlib import Path
import unreal as u
from .core import read_json


def asset_dependencies(paths):
    """Persist material/function/texture package dependencies for frame resume."""
    registry=u.AssetRegistryHelpers.get_asset_registry()
    options=u.AssetRegistryDependencyOptions(include_hard_package_references=True,include_soft_package_references=True)
    pending=[str(p).split('.')[0] for p in paths];visited=set()
    while pending:
        package=pending.pop()
        if package in visited or not package.startswith('/Game/'):continue
        visited.add(package)
        pending.extend(str(p) for p in (registry.get_dependencies(u.Name(package),options) or []))
    return sorted(visited)


def restore(actor, records=None):
    if records is None:
        path=Path(actor.get_editor_property('root_layer').file_path)
        if not path.is_absolute():path=Path(u.Paths.project_dir())/path
        content=Path(u.Paths.project_content_dir()).resolve();path=path.resolve()
        if not path.is_relative_to(content) or not (path.parent/'package.json').is_file():return {'restored':0}
        records=read_json(path.parent/'package.json').get('shader_data',[])
    count=0;initialized=set();touched={}
    for item in records:
        component=actor.get_generated_component(item['prim_path']+'/Mesh') or actor.get_generated_component(item['prim_path'])
        if component is None:raise RuntimeError('对象随机值缺少 USD 组件: '+item['prim_path'])
        meshes=([component] if isinstance(component,u.StaticMeshComponent) else
                [c for c in component.get_children_components(True) if isinstance(c,u.StaticMeshComponent)])
        if not meshes:raise RuntimeError('对象随机值缺少网格组件: '+item['prim_path'])
        value=float(item['object_random'])
        if 'instance_index' in item:
            if len(meshes)!=1:raise RuntimeError('实例随机值需要唯一网格组件: '+item['prim_path'])
            mesh=meshes[0]
            if not isinstance(mesh,u.InstancedStaticMeshComponent):raise RuntimeError('实例数据未生成 InstancedStaticMeshComponent')
            key=mesh.get_path_name()
            if key not in initialized:
                mesh.set_num_custom_data_floats(1);initialized.add(key)
            if not mesh.set_custom_data_value(item['instance_index'],0,value,False):raise RuntimeError('实例随机值写入失败')
            touched[key]=(mesh,item['instance_index'],value)
        else:
            for mesh in meshes:mesh.set_custom_primitive_data_float(0,value)
        count+=1
    for mesh,index,value in touched.values():mesh.set_custom_data_value(index,0,value,True)
    return {'restored':count,'instanced_components':len(initialized),'semantic':'blender_cycles_object_random'}
