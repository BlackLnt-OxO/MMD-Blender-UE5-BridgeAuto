"""Original-PMX texture choices, shared by desktop review and Blender copies."""
from pathlib import Path
from .core import file_hash
from .character_pbr import override_for
from .character_profiles import fingerprint


def source_bindings(cfg, model, index):
    return {key:value for key,value in override_for(cfg,file_hash(model),index).get('textures',{}).items()
            if key in ('base_color','sphere')}


def missing_sources(model, cfg):
    from .pmx_reader import read_pmx
    path=Path(model);pmx=read_pmx(path);rows=[]
    digest=file_hash(path)
    for mat in pmx.materials:
        manual=override_for(cfg,digest,mat['pmx_index']).get('textures',{})
        for semantic,key in (('base_color','texture_index'),('sphere','sphere_index')):
            if semantic=='sphere' and mat['sphere_mode']==0:continue
            index=mat[key]
            reference=pmx.textures[index] if 0<=index<len(pmx.textures) else ''
            binding=manual.get(semantic,{})
            target=Path(binding['path']) if binding else path.parent/reference.replace('\\','/')
            if (binding or reference) and not target.is_file():
                rows.append({'pmx_index':mat['pmx_index'],'name':mat['name'],'semantic':semantic,
                    'reference':reference,'missing_path':str(target),'alpha':mat['diffuse'][3]})
    return rows


def binding_signature(cfg):
    if cfg.get('character_material_profile')=='character_pbr':return None
    model=cfg.get('model','')
    if not model or not Path(model).is_file():return None
    mappings={}
    for index in cfg.get('character_material_overrides',{}).get(file_hash(model),{}):
        bindings=source_bindings(cfg,model,int(index))
        if bindings:
            mappings[index]={key:dict(value,sha256=file_hash(value['path']) if Path(value['path']).is_file() else 'missing')
                             for key,value in bindings.items()}
    global_bindings=cfg.get('character_pbr_options',{}).get('textures',{})
    for key,value in global_bindings.items():
        if key in ('base_color','sphere'):
            mappings['global:'+key]=dict(value,sha256=file_hash(value['path']) if Path(value['path']).is_file() else 'missing')
    return fingerprint(mappings) if mappings else None


def check_checkpoint(cfg, manifest):
    current=binding_signature(cfg)
    saved=manifest.get('character_texture_overrides_signature')
    if current!=saved:
        raise ValueError('角色手动贴图或映射已改变，请新建任务；已有导出和检查点保留')


def apply_source_bindings(material, cfg, model, index):
    """Only touch this Blender import copy; PMX fields and source files stay intact."""
    from mmd_tools.core.material import FnMaterial
    bindings=source_bindings(cfg,model,index)
    for semantic,binding in bindings.items():
        path=Path(binding['path'])
        if not path.is_file():raise ValueError('手动角色贴图不存在：'+str(path))
        fn=FnMaterial(material)
        if semantic=='base_color':fn.create_texture(str(path.resolve()))
        else:fn.create_sphere_texture(str(path.resolve()))
    return {key:dict(value,sha256=file_hash(value['path'])) for key,value in bindings.items()}
