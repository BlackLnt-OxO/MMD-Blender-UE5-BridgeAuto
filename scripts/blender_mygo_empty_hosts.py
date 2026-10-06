"""Inspect skipped source hosts without saving the source blend."""
import bpy,json
from pathlib import Path
OUT=Path(r'D:\Codex_Projects\MMD_Blender_UE5_Autowork\work\material_restoration\full_scene')
job=Path(json.loads((OUT/'job_path.json').read_text(encoding='utf-8'))['job'])
manifest=json.loads((job/'manifest.json').read_text(encoding='utf-8'))
graph=bpy.context.evaluated_depsgraph_get();rows=[]
for item in manifest['scene']['skipped_items']:
    source=bpy.data.objects.get(item['name'])
    if source is None:rows.append(dict(name=item['name'],error='source object absent'));continue
    evaluated=source.evaluated_get(graph)
    mesh=evaluated.to_mesh() if source.type in ('MESH','CURVE','SURFACE','FONT','META') else None
    rows.append(dict(name=source.name,type=source.type,hide_render=source.hide_render,
        raw_vertices=len(source.data.vertices) if source.type=='MESH' else None,
        final_vertices=len(mesh.vertices) if mesh else 0,final_polygons=len(mesh.polygons) if mesh else 0,
        modifiers=[dict(name=m.name,type=m.type,render=m.show_render) for m in source.modifiers]))
    if mesh:evaluated.to_mesh_clear()
(OUT/'empty_host_source_probe.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print('EMPTY_HOST_SOURCE_PROBE',len(rows),flush=True)
