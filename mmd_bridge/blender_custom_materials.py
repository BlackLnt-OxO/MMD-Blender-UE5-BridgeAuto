"""Deterministic texture copies for both custom import and live material edits."""
import copy
import shutil
from pathlib import Path

import bpy
import numpy as np

from .core import file_hash, stable_id
from .character_custom_materials import plan


def prepare_texture(spec, folder):
    source = Path(spec['path'])
    digest = file_hash(source)
    if digest != spec['source_sha256']:
        raise ValueError('来源图片在分析后发生变化: ' + str(source))
    channel, invert = spec.get('channel', 'rgb'), spec.get('invert', False)
    key = stable_id('custom_texture_v1', digest, channel, invert, spec.get('color_space', 'srgb'))
    folder = Path(folder) / 'Textures'
    folder.mkdir(parents=True, exist_ok=True)
    supported = source.suffix.lower() in ('.png', '.jpg', '.jpeg', '.bmp', '.tga', '.tif', '.tiff', '.exr', '.hdr')
    if channel == 'rgb' and not invert and supported:
        target = folder / ('T_Custom_' + key + source.suffix.lower())
        if not target.is_file() or file_hash(target) != digest:
            shutil.copyfile(source, target)
    else:
        image = bpy.data.images.load(str(source), check_existing=False)
        output = None
        try:
            image.colorspace_settings.name = 'Non-Color'
            width, height = image.size
            if not width or not height:
                raise ValueError('图片不能解码: ' + str(source))
            data = np.empty(width * height * 4, dtype=np.float32)
            image.pixels.foreach_get(data)
            data = data.reshape((-1, 4))
            if channel != 'rgb':
                values = data[:, 'rgba'.index(channel)].copy()
                if invert:
                    values = 1. - values
                data[:, :3] = values[:, None]
                data[:, 3] = 1.
            elif invert:
                data[:, :3] = 1. - data[:, :3]
            target = folder / ('T_Custom_' + key + ('.exr' if image.is_float else '.png'))
            output = bpy.data.images.new('MMDBridge_Custom_Conversion', width, height,
                                         alpha=True, float_buffer=image.is_float)
            output.colorspace_settings.name = 'Non-Color'
            output.pixels.foreach_set(data.ravel())
            output.filepath_raw = str(target)
            output.file_format = 'OPEN_EXR' if image.is_float else 'PNG'
            output.save()
        finally:
            if output is not None:
                bpy.data.images.remove(output)
            bpy.data.images.remove(image)
    return {'id': key, 'path': str(target), 'name': target.name, 'asset_name': 'T_Custom_' + key,
            'source_sha256': digest, 'sha256': file_hash(target),
            'color_space': 'sRGB' if spec.get('color_space', 'srgb') == 'srgb' else 'Non-Color',
            'normal': spec.get('color_space') == 'normal', 'hdr': target.suffix == '.exr'}


def prepare_record(cfg, source, folder, record=None):
    note = plan(cfg, source)
    result = copy.deepcopy(record or {})
    result.setdefault('id', stable_id(source['model_sha256'], source['pmx_index'], 'custom_v1'))
    result.setdefault('asset_name', 'M_Custom_' + str(source['pmx_index']))
    result.update(name=source['name'], pmx_index=source['pmx_index'])
    result['mmd'] = {'name_j': source['name'], 'name_e': source.get('name_e', ''),
                     'diffuse_color': source['diffuse'][:3], 'diffuse_alpha': source['diffuse'][3],
                     'specular_color': source['specular'], 'shininess': source['shininess'],
                     'ambient_color': source['ambient'], 'sphere_texture_type': source['sphere_mode']}
    result['textures'] = {target: prepare_texture(spec, folder) for target, spec in note['textures'].items()}
    result['character_material'] = {'custom': note}
    result.setdefault('warnings', [])
    return result
