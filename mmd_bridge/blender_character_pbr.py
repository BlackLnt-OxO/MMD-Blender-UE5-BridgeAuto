"""Prepare source-aware character PBR maps inside Blender."""
from pathlib import Path
import math
import shutil

from .core import file_hash, atomic_json, stable_id, asset_name, Cancelled, resource_root
from .character_pbr import plan, texture_files, VERSION, detail_bindings, override_for
from .character_profiles import fingerprint
from .blender_character_materials import prepare_alpha, save_data_image


def analyze(materials, material, index):
    """Keep the FBX slot and source alpha even if this source material is broken."""
    original = material.name
    try:
        return materials.analyze(material, pmx_index=index)
    except Cancelled:
        raise
    except (ValueError, RuntimeError) as exc:
        record = dict(getattr(materials, '_active_record', None) or {})
        identity = record.get('id') or stable_id(materials.source, original)
        mm = material.mmd_material
        record.update(id=identity, name=original, source_name=original, pmx_index=index,
                      material_error=str(exc), warnings=[str(exc)], channels=record.get('channels', {}),
                      textures=record.get('textures', {}), surface='pbr', strategy='nodes',
                      requires_visual_review=True)
        record['mmd'] = {'name_j': mm.name_j, 'name_e': mm.name_e,
            'diffuse_alpha': float(mm.alpha), 'diffuse_color': list(mm.diffuse_color),
            'two_sided': bool(mm.is_double_sided), 'texture_rel_path': mm.texture_rel_path,
            'sphere_texture_rel_path': mm.sphere_texture_rel_path,
            'sphere_texture_type': str(mm.sphere_texture_type)}
        from .blender_materials import MissingTextureError
        sphere = mm.sphere_texture_rel_path
        base = mm.texture_rel_path
        if isinstance(exc, MissingTextureError) and sphere:
            folder = Path(materials.source).parent
            sphere_path = (folder/sphere.replace('\\', '/')).resolve()
            base_path = (folder/base.replace('\\', '/')).resolve() if base else None
            if exc.path == sphere_path and exc.path != base_path:
                record['source_effect_error'] = record.pop('material_error')
        material.name = asset_name('M', original+'_MMDBridge', identity)
        record.update(asset_name=material.name, slot_name=material.name)
        materials.records[identity] = record
        return record


def detail_maps(folder):
    """Package generated, reproducible maps with the job, including wet-off jobs."""
    for kind in ('skin', 'hair', 'cloth', 'satin', 'hosiery', 'leather', 'generic'):
        for spec in detail_bindings(kind).values():
            shutil.copy2(spec['path'], folder/Path(spec['path']).name)


def surface_scale(worker, record):
    """Measure centimetres per UV tile and the predominant UV gravity direction."""
    import numpy as np
    area, uv_area = 0., 0.
    gravity = np.zeros(2)
    triangles = 0
    for obj in worker.meshes:
        if not obj.data.uv_layers.active:
            continue
        indices = {i for i, material in enumerate(obj.data.materials) if material and
                   material.name in (record['source_name'], record['asset_name'])}
        uv = obj.data.uv_layers.active.data
        for polygon in obj.data.polygons:
            if polygon.material_index not in indices:
                continue
            loops = list(polygon.loop_indices)
            for offset in range(1, len(loops)-1):
                tri = [loops[0], loops[offset], loops[offset+1]]
                points = np.array([(obj.matrix_world @ obj.data.vertices[obj.data.loops[i].vertex_index].co)[:]
                                   for i in tri])
                tex = np.array([uv[i].uv[:] for i in tri])
                edges, tex_edges = points[1:]-points[0], tex[1:]-tex[0]
                texture_area = abs(float(np.linalg.det(tex_edges)))*.5
                world_area = float(np.linalg.norm(np.cross(*edges)))*.5
                if texture_area < 1e-10 or world_area < 1e-12:
                    continue
                basis = np.linalg.solve(tex_edges, edges)
                direction = np.linalg.lstsq(basis.T, [0., 0., -1.], rcond=None)[0]
                gravity += direction/max(1e-12, np.linalg.norm(direction))*world_area
                area += world_area; uv_area += texture_area; triangles += 1
    if not triangles:
        return None
    angle = (-90-math.degrees(math.atan2(gravity[1], gravity[0]))+180) % 360-180
    return {'cm_per_uv': 100*math.sqrt(area/uv_area), 'uv_gravity_rotation': angle,
            'triangles': triangles, 'method': 'world_area_over_uv_area_and_projected_gravity',
            'gravity_alignment': 'material_average_requires_visual_review'}


def hair_direction(worker, record):
    """Project each triangle's geometric long axis into its original UV basis."""
    import numpy as np
    angles = []
    for obj in worker.meshes:
        if not obj.data.uv_layers.active:
            continue
        indices = {i for i, material in enumerate(obj.data.materials) if material and
                   material.name in (record['source_name'], record['asset_name'])}
        uv = obj.data.uv_layers.active.data
        for polygon in obj.data.polygons:
            if polygon.material_index not in indices or len(polygon.vertices) < 3:
                continue
            loops = list(polygon.loop_indices)[:3]
            points = np.array([obj.data.vertices[obj.data.loops[i].vertex_index].co[:] for i in loops])
            tex = np.array([uv[i].uv[:] for i in loops])
            edges = points[1:] - points[0]
            try:
                basis = np.linalg.solve(tex[1:]-tex[0], edges)
            except np.linalg.LinAlgError:
                continue
            # PMX hair strips normally follow the longer geometric UV derivative.
            axis = np.linalg.norm(basis, axis=1)
            angles.append((90. if axis[1] >= axis[0] else 0., float(polygon.area)))
    if not angles:
        return 90., 'fallback_uv_v'
    u_weight = sum(weight for angle, weight in angles if angle == 0)
    v_weight = sum(weight for angle, weight in angles if angle == 90)
    return (90. if v_weight >= u_weight else 0.), 'geometry_uv_derivatives'

def hair_flow(worker, record, folder, size=512):
    """Bake a tangent-space field from each connected hair strip's long axis."""
    import numpy as np
    pixels = np.empty((size, size, 4), np.float32)
    fallback_angle, _ = hair_direction(worker, record)
    fallback_angle = math.radians(fallback_angle)
    pixels[:] = [.5+.5*math.cos(fallback_angle), .5+.5*math.sin(fallback_angle), .5, 1.]
    painted = 0
    for obj in worker.meshes:
        if not obj.data.uv_layers.active:
            continue
        slots = {i for i, mat in enumerate(obj.data.materials) if mat and
                 mat.name in (record['source_name'], record['asset_name'])}
        polygons = [p for p in obj.data.polygons if p.material_index in slots]
        parents = {}
        def find(index):
            parents.setdefault(index, index)
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index
        for polygon in polygons:
            vertices = list(polygon.vertices)
            for vertex in vertices[1:]:
                parents[find(vertex)] = find(vertices[0])
        groups = {}
        for vertex in parents:
            groups.setdefault(find(vertex), []).append(vertex)
        axes = {}
        for root, vertices in groups.items():
            points = np.array([obj.data.vertices[v].co[:] for v in vertices])
            if len(points) < 3:
                continue
            _, _, vectors = np.linalg.svd(points-points.mean(0), full_matrices=False)
            axes[root] = vectors[0]
        uv = obj.data.uv_layers.active.data
        for polygon in polygons:
            axis = axes.get(find(polygon.vertices[0]))
            if axis is None:
                continue
            loops = list(polygon.loop_indices)
            for offset in range(1, len(loops)-1):
                triangle = [loops[0], loops[offset], loops[offset+1]]
                tex = np.array([uv[i].uv[:] for i in triangle])
                points = np.array([obj.data.vertices[obj.data.loops[i].vertex_index].co[:] for i in triangle])
                try:
                    basis = np.linalg.solve(tex[1:]-tex[0], points[1:]-points[0])
                    direction = np.linalg.lstsq(basis.T, axis, rcond=None)[0]
                except np.linalg.LinAlgError:
                    continue
                length = np.linalg.norm(direction)
                if length < 1e-8:
                    continue
                direction /= length
                if direction[1] < 0:
                    direction = -direction
                # Repeat whole UV tiles without stretching the original atlas.
                tex -= np.floor(tex.mean(0))
                xy = tex*size-.5
                lo = np.maximum(0, np.floor(xy.min(0)).astype(int))
                hi = np.minimum(size-1, np.ceil(xy.max(0)).astype(int))
                if (hi < lo).any():
                    continue
                yy, xx = np.mgrid[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
                a, b, c = xy
                denominator = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
                if abs(denominator) < 1e-8:
                    continue
                w0 = ((b[1]-c[1])*(xx-c[0])+(c[0]-b[0])*(yy-c[1]))/denominator
                w1 = ((c[1]-a[1])*(xx-c[0])+(a[0]-c[0])*(yy-c[1]))/denominator
                inside = (w0 >= -.01) & (w1 >= -.01) & (w0+w1 <= 1.01)
                patch = pixels[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
                patch[inside, :2] = direction*.5+.5
                painted += int(inside.sum())
    spec = save_data_image(folder/('hair_flow_'+record['id']+'.png'), pixels, size, size)
    spec['lookup'] = False
    spec['sha256'] = file_hash(spec['path'])
    return spec, {'method': 'connected_strip_pca_uv_projection', 'painted_samples': painted,
                  'resolution': size, 'overlapping_uvs': 'shared_direction_requires_visual_review'}


def eye_surface_statistics(worker, record):
    """Resolve ambiguous Eye/Eyes names using only the surface's actual atlas region."""
    import re
    if not re.fullmatch(r'eyes?[ _.-]*[0-9]*',record.get('name',''),re.I):
        return None
    spec=record.get('textures',{}).get('base_color')
    if not spec or not Path(spec['path']).is_file():
        return None
    import bpy
    import numpy as np
    from .blender_character_materials import material_alpha_samples
    image=bpy.data.images.load(spec['path'],check_existing=False)
    try:
        pixels=np.empty(image.size[0]*image.size[1]*4,np.float32)
        image.pixels.foreach_get(pixels)
        samples=np.stack([material_alpha_samples(worker,record,image,pixels[c::4]) for c in range(4)],-1)
        samples=samples[samples[:,3]>.9,:3]
        if len(samples)<128:return None
        saturation=samples.max(1)-samples.min(1)
        return {'samples':len(samples),'mean':samples.mean(0).tolist(),'std':samples.std(0).tolist(),
                'bright_neutral_ratio':float(np.mean((samples.min(1)>.7)&(saturation<.12))),
                'source_sha256':file_hash(spec['path']),'method':'actual_material_uv_region'}
    finally:
        bpy.data.images.remove(image)


def face_scattering_mask(worker, record, folder, size=1024):
    """Find the original frontal nose in MMD axes and rasterize a smooth SSS mask.

    This changes scattering only. Geometry, normals and base artwork are read-only.
    Small/non-head Face slots fall back to uniform scattering with a diagnostic.
    """
    import numpy as np
    pixels = np.ones((size, size), np.float32)
    notes = []
    for obj in worker.meshes:
        if not obj.data.uv_layers.active:
            continue
        mesh = obj.data
        slots = {i for i, mat in enumerate(mesh.materials) if mat and
                 mat.name in (record['source_name'], record['asset_name'])}
        faces = [p for p in mesh.polygons if p.material_index in slots]
        ids = sorted({i for p in faces for i in p.vertices})
        if len(ids) < 64:
            continue
        positions = np.array([(obj.matrix_world @ mesh.vertices[i].co)[:] for i in ids])
        low, high = positions.min(0), positions.max(0)
        width, height = high[0]-low[0], high[2]-low[2]
        if width < .06 or height < .08 or not .4 < width/height < 2.5:
            continue
        center_x = (high[0]+low[0])*.5
        search = positions[(abs(positions[:, 0]-center_x) < width*.1) &
                           (positions[:, 2] > low[2]+height*.2) &
                           (positions[:, 2] < low[2]+height*.6)]
        if not len(search):
            continue
        nose = search[np.argmin(search[:, 1])]
        # A 1-2 cm region on this head, scaled to the source's proportions.
        rx, rz = width*.045, height*.06
        uv = mesh.uv_layers.active.data
        for polygon in faces:
            loops = list(polygon.loop_indices)
            for offset in range(1, len(loops)-1):
                tri = [loops[0], loops[offset], loops[offset+1]]
                tex = np.array([uv[i].uv[:] for i in tri])
                points = np.array([(obj.matrix_world @ mesh.vertices[mesh.loops[i].vertex_index].co)[:] for i in tri])
                tex -= np.floor(tex.mean(0))
                xy = tex*size-.5
                lo = np.maximum(0, np.floor(xy.min(0)).astype(int))
                hi = np.minimum(size-1, np.ceil(xy.max(0)).astype(int))
                if (hi < lo).any(): continue
                yy, xx = np.mgrid[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
                a, b, c = xy
                denominator = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
                if abs(denominator) < 1e-8: continue
                w0 = ((b[1]-c[1])*(xx-c[0])+(c[0]-b[0])*(yy-c[1]))/denominator
                w1 = ((c[1]-a[1])*(xx-c[0])+(a[0]-c[0])*(yy-c[1]))/denominator
                inside = (w0 >= 0) & (w1 >= 0) & (w0+w1 <= 1)
                point = w0[..., None]*points[0]+w1[..., None]*points[1]+(1-w0-w1)[..., None]*points[2]
                field = 1-np.exp(-2*((point[..., 0]-nose[0])/rx)**2-2*((point[..., 2]-nose[2])/rz)**2)
                patch = pixels[lo[1]:hi[1]+1, lo[0]:hi[0]+1]
                patch[inside] = np.minimum(patch[inside], field[inside])
        notes.append({'object': obj.name, 'nose_world_m': nose.tolist(), 'radius_xz_m': [rx, rz]})
    if not notes:
        return None, {'status': 'uniform', 'reason': 'no_plausible_frontal_head_region'}
    for _ in range(3):
        pixels = np.minimum.reduce([pixels, np.roll(pixels, 1, 0), np.roll(pixels, -1, 0),
                                    np.roll(pixels, 1, 1), np.roll(pixels, -1, 1)])
    for _ in range(4):
        pixels = (pixels+np.roll(pixels, 1, 0)+np.roll(pixels, -1, 0)+
                  np.roll(pixels, 1, 1)+np.roll(pixels, -1, 1))/5
    rgba = np.ones((size, size, 4), np.float32); rgba[..., :3] = pixels[..., None]
    spec = save_data_image(folder/('face_sss_'+record['id']+'.png'), rgba, size, size)
    spec.update(lookup=False, sha256=file_hash(spec['path']))
    return spec, {'status': 'generated', 'method': 'original_frontal_nose_gaussian_uv_mask',
                  'resolution': size, 'regions': notes, 'needs_visual_review': True}


def prepare(worker):
    import bpy
    original = worker.data['settings'].get('model') or worker.cfg['model']
    digest = file_hash(original)
    files = texture_files(original)
    generated = worker.bundle/'CharacterPBR'
    generated.mkdir(parents=True, exist_ok=True)
    detail_maps(generated)
    diagnostics = []
    for record in worker.materials.records.values():
        if record.get('pmx_index') is None:
            continue
        note = record.setdefault('character_material', {})
        note.update(profile='character_pbr', role='pbr', sdf={'format': 'none', 'status': 'disabled'})
        try:
            prepare_alpha(worker, record)
            eye_stats=eye_surface_statistics(worker,record)
            if eye_stats:note['uv_color_statistics']=eye_stats
            pbr = plan(record, worker.cfg, digest, files)
            if record.get('material_error'):
                pbr['status'] = 'white'
                pbr['issues'].append(record['material_error'])
            record['textures'].update(detail_bindings(pbr['type'], generated))
            if pbr['type'] in ('skin', 'face', 'cloth', 'satin', 'hosiery', 'leather'):
                scale = surface_scale(worker, record)
                if scale:
                    tile_cm = {'skin': .6, 'face': .6, 'cloth': .35, 'satin': .5,
                               'hosiery': .8, 'leather': .5}[pbr['type']]
                    generated_params = {'DetailTiling': max(1., min(300., scale['cm_per_uv']/tile_cm))}
                    if pbr['type'] in ('satin', 'hosiery'):
                        generated_params['FabricDirection'] = (-90-scale['uv_gravity_rotation']+180) % 360-180
                    if pbr['type'] in ('skin', 'face'):
                        generated_params.update(WetTiling=max(.1, min(100., scale['cm_per_uv']/8.)),
                                                WetRotation=scale['uv_gravity_rotation'])
                    pbr['surface_scale'] = scale
                    pbr['generated_defaults'] = generated_params
                    explicit = override_for(worker.cfg, digest, record['pmx_index']).get('params', {})
                    pbr['params'].update({k: v for k, v in generated_params.items() if k not in explicit})
            for semantic, binding in pbr['textures'].items():
                if not Path(binding['path']).is_file():
                    continue
                image = bpy.data.images.load(binding['path'], check_existing=False)
                try:
                    # Copy into the bundle so jobs remain independent of source folders.
                    spec = worker.materials.image(image)
                    record['textures'][semantic] = spec
                    binding['path'] = spec['path']
                finally:
                    bpy.data.images.remove(image)
            if 'opacity' in pbr['textures']:
                import numpy as np
                from .character_profiles import character_alpha_mode
                image = bpy.data.images.load(pbr['textures']['opacity']['path'], check_existing=False)
                try:
                    pixels = np.empty(image.size[0]*image.size[1]*4, np.float32)
                    image.pixels.foreach_get(pixels)
                    channel = 'rgba'.index(pbr['textures']['opacity'].get('channel', 'r'))
                    values = pixels[channel::4]
                    if pbr['textures']['opacity'].get('invert'):
                        values = 1-values
                    # Reuse material UV coverage for dedicated alpha maps too.
                    from .blender_character_materials import material_alpha_samples
                    values = material_alpha_samples(worker, record, image, values)
                    histogram = np.bincount(np.rint(values*255).astype(np.uint8), minlength=256).tolist()
                    if pbr['alpha_mode'] == note['alpha_mode']:
                        pbr['alpha_mode'] = character_alpha_mode(histogram, pbr['params']['Opacity'])
                finally:
                    bpy.data.images.remove(image)
            if pbr['type'] == 'hair' and 'direction' not in pbr['textures']:
                flow, source = hair_flow(worker, record, generated)
                record['textures']['direction'] = flow
                pbr['textures']['direction'] = {'path': flow['path'], 'channel': 'rgb',
                                               'kind': 'generated', 'sha256': flow['sha256']}
                override = override_for(worker.cfg, digest, record['pmx_index'])
                if 'HairDirection' not in override.get('params', {}):
                    pbr['params']['HairDirection'] = 90.
                pbr['direction_source'] = source
            if pbr['type'] == 'iris' and 'iris_mask' not in pbr['textures']:
                pbr['params']['IrisDepth'] = 0.
            if pbr['type'] == 'face' and pbr['textures'].get('sss_mask', {}).get('kind') in (None, 'generated'):
                mask, evidence = face_scattering_mask(worker, record, generated)
                pbr['face_scattering_source'] = evidence
                if mask:
                    record['textures']['sss_mask'] = mask
                    pbr['textures']['sss_mask'] = {'path': mask['path'], 'channel': 'r',
                                                 'kind': 'generated', 'sha256': mask['sha256']}
            pbr['signature'] = fingerprint({k: v for k, v in pbr.items() if k != 'signature'})
            note['pbr'] = pbr
            record['requires_visual_review'] = True
        except Exception as exc:
            # Continue other material slots; preserve all source opacity information.
            from .core import Cancelled
            if isinstance(exc, Cancelled):
                raise
            note['pbr'] = {'version': VERSION, 'status': 'white', 'type': 'generic',
                           'issues': [str(exc)], 'params': {'Opacity': float(record.get('mmd', {}).get('diffuse_alpha', 1))},
                           'alpha_mode': note.get('alpha_mode', 'masked'), 'textures': {},
                           'matcap': {'enabled': False}}
        diagnostics.append({'id': record['id'], 'name': record['name'], **note['pbr']})
    atomic_json(generated/'diagnostics.json', diagnostics)
