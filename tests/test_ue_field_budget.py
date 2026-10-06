import ast
from pathlib import Path
from types import SimpleNamespace


def test_field_budget_skips_source_color_and_restores_previous_cap():
    tree=ast.parse((Path(__file__).parents[1]/'mmd_bridge/ue_materials.py').read_text(encoding='utf-8-sig'))
    functions=[x for x in tree.body if isinstance(x,ast.FunctionDef) and
               x.name in ('_field_texture_size','configure_baked_field_textures')]
    class Texture:
        def __init__(self, name, maximum):
            self.name,self.maximum,self.tags=name,maximum,{'MMDBridge.Owner':'owned'}
        def get_editor_property(self,key):
            assert key=='max_texture_size';return self.maximum
        def set_editor_property(self,key,value):
            assert key=='max_texture_size';self.maximum=value
        def get_path_name(self):return self.name
    field=Texture('/Root/Textures/T_Field_C_v2_abcdef123456',0)
    original=Texture('/Root/Textures/T_Leaf_D',0)
    artist_hdr=Texture('/Root/Textures/T_Artist_C',0)
    saved=[]
    native=SimpleNamespace(Texture2D=Texture,load_asset=lambda path: {
        field.name:field,original.name:original,artist_hdr.name:artist_hdr}.get(path),
        EditorAssetLibrary=SimpleNamespace(get_metadata_tag=lambda t,k:t.tags.get(k,''),
        set_metadata_tag=lambda t,k,v:t.tags.__setitem__(k,v),
        save_loaded_asset=lambda t:saved.append(t.name) or True))
    protected=[]
    class Protection:
        def __init__(self,*args):pass
        def verify_asset(self,asset):protected.append(('verify',asset.name))
        def protect_asset(self,asset):protected.append(('save',asset.name))
    namespace={'u':native,'file_hash':lambda p:'abcdef1234567890','Materials':Protection}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'field-budget','exec'),namespace)
    scene={'materials':[{'baked_fields':[{'image_id':'field'}],'channels':{'base_color':{'op':'texture','image':{
        'id':'original','name':'Leaf.png','asset_name':'T_Leaf','hdr':False}},
            'roughness':{'op':'texture','image':{'id':'field','path':'field.hdr','name':'Bake_field_a','asset_name':'T_Field','hdr':True}},
        'emission':{'op':'texture','image':{'id':'artist','name':'Bake_artist.exr','asset_name':'T_Artist','hdr':True}}}}]}
    report={}
    checks=namespace['configure_baked_field_textures'](scene,{'scene_import_profile':'render_optimized'},'/Root',report)
    assert field.maximum==1024 and original.maximum==0 and artist_hdr.maximum==0
    assert len(checks)==1 and saved==[field.name]
    namespace['configure_baked_field_textures'](scene,{'scene_import_profile':'faithful'},'/Root',report)
    assert field.maximum==0 and original.maximum==0 and artist_hdr.maximum==0
    assert saved==[field.name,field.name]
    assert protected==[('verify',field.name),('save',field.name)]*2
