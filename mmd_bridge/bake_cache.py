"""Reference-aware cleanup of tool-owned scene bake files after native import."""
from pathlib import Path
import re


def material_texture_paths(records):
    paths = set()
    def visit(value):
        if isinstance(value, dict):
            if {'id', 'path', 'color_space'}.issubset(value):
                paths.add(Path(value['path']).resolve())
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
    visit(records)
    return paths


def cleanup_scene_bakes(scene):
    """Only called after structural import success; never delete user textures."""
    usd = scene.get('usd', {})
    root = Path(usd['root_layer']).resolve().parent
    textures = (root / 'Textures').resolve()
    if textures.parent != root or textures.is_symlink():
        raise ValueError('烘焙目录越出受管场景包')
    keep = material_texture_paths(scene.get('materials', []))
    for item in usd.get('files', []):
        path = Path(item['path']).resolve()
        if path.parent == textures:
            keep.add(path)
    # Validate references before deleting anything. A partial package isn't a
    # reason to clean up previous outputs.
    if any(not path.is_file() for path in keep):
        raise ValueError('场景贴图引用不完整，保留现有缓存')
    removed, size = [], 0
    for path in textures.iterdir():
        if not path.is_file() or path.is_symlink() or path.resolve() in keep:
            continue
        if not re.fullmatch(r'(?:T_)?Bake_[A-Za-z0-9_]+\.(?:exr|png)', path.name):
            continue
        if path.resolve().parent != textures:
            raise ValueError('烘焙文件越出受管目录')
        size += path.stat().st_size
        path.unlink()
        removed.append(str(path))
    return {'removed_files': len(removed), 'removed_bytes': size, 'paths': removed,
            'retained_referenced_textures': len(keep)}
