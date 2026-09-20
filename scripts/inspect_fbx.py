import bpy,sys,json
from pathlib import Path
from io_scene_fbx import parse_fbx
folder=Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
job=Path((folder/'work/current_job.txt').read_text().strip())
m=json.loads((job/'manifest.json').read_text())
tree,version=parse_fbx.parse(m['files']['animation']['path'])
result={}
for node in tree.elems:
    if node.id==b'GlobalSettings':
        result['global']=repr(node)
    if node.id==b'Objects':
        result['first_models']=[repr(n.props) for n in node.elems if n.id==b'Model'][:5]
        result['controls']=[repr(n) for n in node.elems if n.id==b'Model' and b'b_0735' in n.props[1]]
        result['shapes']=[repr(n.props) for n in node.elems if n.id==b'Geometry' and n.props[-1]==b'Shape'][:3]
(folder/'work/fbx_info.json').write_text(json.dumps(result,indent=2))
