"""Prepare only the source data requested by a registered character shader."""
import json
import math
from pathlib import Path

from .character_profiles import (load_profiles, resolve_profile, material_role, is_eye_material,
                                 source_values, channel_value, discover_sources, fingerprint, requested_character_parent)
from .core import resource_root, atomic_json, file_hash, Cancelled


def save_data_image(path, pixels, width, height, hdr=False, resolution=None):
    import bpy
    import numpy as np
    from .blender_materials import write_bake_image
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    image = bpy.data.images.new('MMDBridge_Data_' + path.stem, width, height,
                                alpha=True, float_buffer=hdr)
    try:
        image.colorspace_settings.name = 'Non-Color'
        image.pixels.foreach_set(np.asarray(pixels, dtype=np.float32).reshape(-1))
        if resolution is not None and (width,height) != (resolution,resolution):
            image.scale(resolution,resolution)
        write_bake_image(image, path, hdr)
    finally:
        bpy.data.images.remove(image)
    return {'id': fingerprint([str(path), file_hash(path)])[:24], 'path': str(path),
            'name': path.stem, 'asset_name': 'T_' + path.stem, 'color_space': 'Non-Color',
            'hdr': hdr, 'generated_bake': True, 'lookup': True}


def pack_texture(folder, semantic, recipe, record, profile):
    import bpy
    import numpy as np
    expressions = recipe['channels']
    constants = source_values(record, profile)
    images, loaded = {}, []
    try:
        for expr in expressions.values():
            source = expr.get('source')
            if source and source not in constants:
                spec = record.get('textures', {}).get(source)
                if not spec:
                    raise ValueError('缺少打包纹理来源: ' + source)
                image = bpy.data.images.load(spec['path'], check_existing=False)
                image.colorspace_settings.name = 'Non-Color'
                loaded.append(image)
                images[source] = image
        sizes = {tuple(image.size) for image in images.values()}
        if len(sizes) > 1:
            raise ValueError('局部控制图尺寸不一致，拒绝隐式重采样')
        width, height = next(iter(sizes), (4, 4))
        colors = {}
        for source, image in images.items():
            array = np.empty(width*height*4, dtype=np.float32)
            image.pixels.foreach_get(array)
            colors[source] = array.reshape(height, width, 4)
        rgba = np.empty((height, width, 4), dtype=np.float32)
        for channel, channel_name in enumerate('rgba'):
            expr = expressions[channel_name]
            source = expr.get('source')
            if source in colors:
                index = expr.get('channel', 'r')
                value = (colors[source][..., :3] @ np.array([.2126, .7152, .0722])
                         if index == 'luminance' else colors[source][..., 'rgba'.index(index)])
                if expr.get('invert'):
                    value = 1-value
                value = np.clip(value*float(expr.get('scale', 1)), 0, 1)
            else:
                value = channel_value(expr, constants)
            if not np.isfinite(value).all():
                raise ValueError('控制图含非有限数据')
            rgba[..., channel] = value
        digest = fingerprint({'recipe': recipe, 'constants': constants,
                              'images': {s: file_hash(record['textures'][s]['path']) for s in images}})[:20]
        path = Path(folder) / (semantic + '_' + digest + '.png')
        if path.is_file():
            return {'id': digest, 'path': str(path), 'name': path.stem, 'asset_name': 'T_' + path.stem,
                    'color_space': 'Non-Color', 'lookup': True, 'generated_bake': True}
        return save_data_image(path, rgba, width, height)
    finally:
        for image in loaded:
            bpy.data.images.remove(image)


def face_uv_coverage(mesh, polygons, size, check_overlap=True, check_bounds=True, surface_positions=False):
    import numpy as np
    uv = mesh.uv_layers.active
    if uv is None:
        raise ValueError('面部没有 UV')
    points = np.array([uv.data[i].uv[:] for p in polygons for i in p.loop_indices])
    if not len(points) or not np.isfinite(points).all() or (check_bounds and (points.min() < -1e-5 or points.max() > 1.00001)):
        raise ValueError('面部 UV 不在单个 0–1 区域')
    lo, hi = points.min(axis=0), points.max(axis=0)
    extent = hi-lo
    if (extent < 1e-5).any():
        if check_overlap or surface_positions:
            raise ValueError('面部 UV 范围退化')
        extent=np.maximum(extent,1e-5)
    transform = np.concatenate((1/extent, -lo/extent))
    coverage = np.zeros((size, size), dtype=np.uint16)
    positions = np.zeros((size,size,3),dtype=np.float32) if surface_positions else None
    mesh.calc_loop_triangles()
    selected = {p.index for p in polygons}
    for triangle in mesh.loop_triangles:
        if triangle.polygon_index not in selected:
            continue
        a, b, c = [(np.array(uv.data[i].uv)*transform[:2]+transform[2:])*size
                   for i in triangle.loops]
        det = (b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
        if abs(det) < 1e-7:
            if check_overlap or surface_positions:
                raise ValueError('面部存在零面积 UV 三角形')
            # PMX palette faces can collapse UVs to a texel; alpha still uses that texel.
            indices=np.clip(np.floor([a,b,c]).astype(int),0,size-1)
            coverage[indices[:,1],indices[:,0]]=1
            continue
        lower = np.maximum(0, np.floor(np.minimum(np.minimum(a, b), c)).astype(int))
        upper = np.minimum(size, np.ceil(np.maximum(np.maximum(a, b), c)).astype(int))
        xx, yy = np.meshgrid(np.arange(lower[0], upper[0])+.5, np.arange(lower[1], upper[1])+.5)
        wa = ((b[1]-c[1])*(xx-c[0])+(c[0]-b[0])*(yy-c[1]))/det
        wb = ((c[1]-a[1])*(xx-c[0])+(a[0]-c[0])*(yy-c[1]))/det
        valid = (wa > 1e-6) & (wb > 1e-6) & (wa+wb < 1-1e-6)
        coverage[lower[1]:upper[1], lower[0]:upper[0]] += valid
        if positions is not None:
            coords = np.array([mesh.vertices[i].co[:] for i in triangle.vertices],dtype=np.float32)
            interpolated = wa[...,None]*coords[0]+wb[...,None]*coords[1]+(1-wa-wb)[...,None]*coords[2]
            positions[lower[1]:upper[1],lower[0]:upper[0]][valid] = interpolated[valid]
    if check_overlap and (coverage > 1).any():
        raise ValueError('面部 UV 重叠，无法独立保存阴影')
    if not (coverage > 0).any() or (check_overlap and (coverage > 0).sum() < size*size*.01):
        raise ValueError('面部 UV 有效覆盖不足')
    result = (coverage > 0, transform.tolist())
    return (*result,positions) if surface_positions else result


def material_alpha_samples(worker,record,image,alpha):
    """Ignore unused atlas regions when choosing the mesh's blend mode."""
    import numpy as np
    samples=[]
    for obj in worker.meshes:
        slots={i for i,mat in enumerate(obj.data.materials) if mat and mat.name in (record['source_name'],record['asset_name'])}
        polygons=[p for p in obj.data.polygons if p.material_index in slots]
        if not polygons:
            continue
        coverage,transform=face_uv_coverage(obj.data,polygons,512,check_overlap=False,check_bounds=False)
        yy,xx=np.nonzero(coverage)
        u=((xx+.5)/512-transform[2])/transform[0]
        v=((yy+.5)/512-transform[3])/transform[1]
        u=np.mod(u,1.);v=np.mod(v,1.)
        width,height=image.size
        samples.append(alpha.reshape(height,width)[np.clip((v*height).astype(int),0,height-1),
                                                    np.clip((u*width).astype(int),0,width-1)])
    if not samples:
        raise ValueError('无法确定透明度对应的材质 UV 区域')
    return np.concatenate(samples)


def bake_face(worker, obj, material, record, profile, head_bone):
    import bpy
    import bmesh
    import numpy as np
    from mathutils import Vector
    from .face_sdf import (direction_grid, light_vector, signed_distance, threshold_from_distances,
                           smooth_threshold, encode_threshold, nose_cutouts, cheek_triangle_cutouts, apply_lit_cutout)
    single = profile['sdf']['format'] == 'threshold_lr'
    tile = profile['sdf'].get('tile_size', 512)
    gutter = profile['sdf'].get('gutter', 2)
    size = tile-2*gutter
    polygons = [p for p in obj.data.polygons if obj.data.materials[p.material_index] == material]
    nose_settings = profile['sdf'].get('nose_cutout',{}) if single else {}
    triangle_settings = profile['sdf'].get('cheek_triangle',{}) if single else {}
    triangle_right=triangle_left=None; triangle_info={'status':'disabled'}
    if nose_settings.get('enabled',False) or triangle_settings.get('enabled',False):
        coverage,transform,positions = face_uv_coverage(obj.data,polygons,size,surface_positions=True)
        world = np.array(obj.matrix_world,dtype=np.float32)
        positions = positions@world[:3,:3].T+world[:3,3]
        try:
            nose_right,nose_left,nose_info = nose_cutouts(positions,coverage)
        except ValueError as exc:
            nose_right=nose_left=None
            nose_info={'status':'needs_review','reason':str(exc)}
        if triangle_settings.get('enabled',False):
            try:
                triangle_right,triangle_left,triangle_info = cheek_triangle_cutouts(positions,coverage,nose_info)
            except ValueError as exc:
                triangle_info={'status':'needs_review','reason':str(exc)}
        if not nose_settings.get('enabled',False):
            nose_right=nose_left=None; nose_info={'status':'disabled'}
    else:
        coverage, transform = face_uv_coverage(obj.data, polygons, size)
        nose_right=nose_left=None; nose_info={'status':'disabled'}
    source_hash = fingerprint({'generation_version': 3, 'profile': profile, 'vertices': [v.co[:] for v in obj.data.vertices],
                               'normals': [v.normal[:] for v in obj.data.vertices],
                               'transform': [list(row) for row in obj.matrix_world],
                               'faces': [list(p.vertices) for p in polygons],
                               'uv': [obj.data.uv_layers.active.data[i].uv[:] for p in polygons for i in p.loop_indices]})
    folder = worker.bundle / 'FaceSDF' / (record['id'][:16] + '_' + source_hash[:12])
    folder.mkdir(parents=True, exist_ok=True)
    receipt = folder / 'receipt.json'
    if receipt.is_file():
        cached = json.loads(receipt.read_text(encoding='utf-8'))
        if cached.get('source_hash') == source_hash and all(
                Path(cached[k]['path']).is_file() and file_hash(cached[k]['path']) == cached[k]['sha256']
                for k in (('threshold',) if single else ('atlas', 'coverage'))) and all(
                Path(mask['path']).is_file() and file_hash(mask['path']) == mask['sha256']
                for mask in cached.get('masks',[])):
            return cached
    scene = bpy.data.scenes.new('MMDBridge_FaceBake')
    mesh = obj.data.copy()
    clone = bpy.data.objects.new('MMDBridge_FaceSkin', mesh)
    scene.collection.objects.link(clone)
    clone.matrix_world = obj.matrix_world.copy()
    bm = bmesh.new(); bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()
    selected_indices = {p.index for p in polygons}
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if f.index not in selected_indices], context='FACES')
    bm.to_mesh(mesh); bm.free()
    for point in mesh.uv_layers.active.data:
        point.uv = (point.uv[0]*transform[0]+transform[2], point.uv[1]*transform[1]+transform[3])
    mat = bpy.data.materials.new('MMDBridge_FaceBakeWhite'); mat.use_nodes = True
    nodes = mat.node_tree.nodes; nodes.clear()
    output = nodes.new('ShaderNodeOutputMaterial'); diffuse = nodes.new('ShaderNodeBsdfDiffuse')
    diffuse.inputs['Color'].default_value = (1, 1, 1, 1)
    mat.node_tree.links.new(diffuse.outputs[0], output.inputs['Surface'])
    target = bpy.data.images.new('MMDBridge_FaceBakeTarget', size, size, float_buffer=True)
    target.colorspace_settings.name = 'Non-Color'
    sample = nodes.new('ShaderNodeTexImage'); sample.image = target; nodes.active = sample
    mesh.materials.clear(); mesh.materials.append(mat)
    for poly in mesh.polygons:
        poly.material_index = 0
    sun_data = bpy.data.lights.new('MMDBridge_FaceBakeSun', 'SUN'); sun_data.angle = 0
    sun = bpy.data.objects.new('MMDBridge_FaceBakeSun', sun_data); scene.collection.objects.link(sun)
    scene.render.engine = 'CYCLES'; scene.cycles.samples = profile['sdf'].get('samples', 64)
    scene.cycles.seed = 0; scene.world = None
    scene.render.bake.use_pass_color = False
    scene.render.bake.use_pass_direct = True
    scene.render.bake.use_pass_indirect = False
    scene.render.bake.margin = 4
    atlas = None if single else np.empty((9*tile, 16*tile), dtype=np.float32)
    fields = {}
    masks = []
    window = bpy.context.window
    previous_scene = window.scene if window else None
    try:
        if window:
            window.scene = scene
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0],
                                       active_object=clone, object=clone,
                                       selected_objects=[clone], selected_editable_objects=[clone]):
            clone.select_set(True); scene.view_layers[0].objects.active = clone
            grid = [(column,4,column*math.tau/16,0.) for column in range(16)] if single else direction_grid()
            for index, (column, row, yaw, pitch) in enumerate(grid):
                from .core import check_stop
                check_stop(worker.job)
                command_path = worker.job.folder / 'command.json'
                if command_path.is_file() and json.loads(command_path.read_text(encoding='utf-8')).get('name') == 'stop':
                    raise RuntimeError('用户停止 SDF 生成')
                worker.job.status('character_sdf', f"面部方向烘焙 {index+1}/{len(grid)}: {record['name']}")
                mask_path = folder / f'mask_{row:02d}_{column:02d}.png'
                if mask_path.is_file():
                    saved = bpy.data.images.load(str(mask_path), check_existing=False)
                    try:
                        saved.colorspace_settings.name = 'Non-Color'
                        array = np.empty(size*size*4, np.float32); saved.pixels.foreach_get(array)
                        lit = array.reshape(size, size, 4)[..., 0] > .5
                    finally:
                        bpy.data.images.remove(saved)
                else:
                    sun.rotation_euler = (-Vector(light_vector(yaw, pitch))).to_track_quat('-Z', 'Y').to_euler()
                    bpy.ops.object.bake(type='DIFFUSE')
                    array = np.empty(size*size*4, np.float32); target.pixels.foreach_get(array)
                    gray = array.reshape(size, size, 4)[..., :3].mean(axis=2)
                    if not np.isfinite(gray).all():
                        raise ValueError('面部烘焙含非有限数据')
                    lit = gray > profile['sdf'].get('threshold', .05)
                    rgba = np.repeat(lit[..., None], 4, axis=2).astype(np.float32); rgba[..., 3] = 1
                    save_data_image(mask_path, rgba, size, size)
                field = signed_distance(lit)
                if single:
                    fields[column] = field
                else:
                    padded = np.pad(field, gutter, mode='edge')
                    columns = range(16) if row in (0, 8) else (column,)
                    for c in columns:
                        atlas[row*tile:(row+1)*tile, c*tile:(c+1)*tile] = padded
                masks.append({'row': row, 'column': column, 'path': str(mask_path), 'sha256': file_hash(mask_path)})
        if single:
            angles = np.linspace(0,1,9,dtype=np.float32)
            interpolation = profile['sdf'].get('interpolation','pchip')
            right, right_relit = threshold_from_distances([fields[i] for i in range(9)],angles,interpolation)
            left, left_relit = threshold_from_distances([fields[(-i)%16] for i in range(9)],angles,interpolation)
            sigma = profile['sdf'].get('smooth_sigma',0)
            contrast = profile['sdf'].get('contrast',0)
            right = smooth_threshold(right,coverage,sigma)
            left = smooth_threshold(left,coverage,sigma)
            if triangle_right is not None:
                end_angle = triangle_settings.get('end_angle',98)/180
                right = apply_lit_cutout(right,triangle_right,end_angle)
                left = apply_lit_cutout(left,triangle_left,end_angle)
                cutout = np.stack((triangle_right,triangle_left,np.zeros_like(right),np.ones_like(right)),axis=2)
                save_data_image(folder/'cheek_triangle.png',cutout,size,size)
                triangle_info['end_angle'] = end_angle*180
            if nose_right is not None:
                end_angle = nose_settings.get('end_angle',135)/180
                right = apply_lit_cutout(right,nose_right,end_angle)
                left = apply_lit_cutout(left,nose_left,end_angle)
                cutout = np.stack((nose_right,nose_left,np.zeros_like(right),np.ones_like(right)),axis=2)
                save_data_image(folder/'nose_cutout.png',cutout,size,size)
                nose_info['end_angle'] = end_angle*180
            right,left = encode_threshold(right,contrast),encode_threshold(left,contrast)
            rgba = np.stack((right,left,coverage.astype(np.float32),np.ones_like(right)),axis=2)
            resolution = profile['sdf'].get('resolution',1024)
            texture = save_data_image(folder/'face_sdf.exr',rgba,size,size,True,resolution)
            texture['sha256'] = file_hash(texture['path'])
            # A grayscale preview is for inspection; the shader reads the R/G EXR channels.
            preview = np.repeat(np.where(coverage,right,0)[...,None],4,axis=2); preview[...,3] = 1
            save_data_image(folder/'face_sdf_R.png',preview,size,size,resolution=resolution)
            relit = (right_relit|left_relit)&coverage
            review = np.repeat(relit[...,None],4,axis=2).astype(np.float32); review[...,3] = 1
            save_data_image(folder/'non_monotonic.png',review,size,size)
            result = {'source_hash':source_hash,'format':'threshold_lr','threshold':texture,
                      'uv_transform':transform,'head_bone':head_bone,'resolution':resolution,
                      'channels':{'r':'positive_yaw_threshold','g':'negative_yaw_threshold','b':'coverage','a':'one'},
                      'directions':16,'interpolation':interpolation,'contrast':contrast,'smooth_sigma':sigma,
                      'nose_cutout':nose_info,
                      'cheek_triangle':triangle_info,
                      'masks':masks,'non_monotonic_fraction':float(relit.sum()/coverage.sum()),
                      'requires_visual_review':True}
            atomic_json(receipt,result)
            return result
        rgba = np.zeros((9*tile, 16*tile, 4), dtype=np.float32); rgba[..., 0] = atlas; rgba[..., 3] = 1
        atlas_spec = save_data_image(folder/'atlas.exr', rgba, 16*tile, 9*tile, True)
        atlas_spec['half_float'] = True
        cov = np.repeat(coverage[..., None], 4, axis=2).astype(np.float32); cov[..., 3] = 1
        coverage_spec = save_data_image(folder/'coverage.png', cov, size, size)
        for spec in (atlas_spec, coverage_spec):
            spec['sha256'] = file_hash(spec['path'])
        result = {'source_hash': source_hash, 'format': 'direction_atlas', 'atlas': atlas_spec,
                  'coverage': coverage_spec, 'uv_transform': transform, 'head_bone': head_bone,
                  'tile_size': tile, 'grid': [16, 9], 'directions': 114, 'masks': masks,
                  'requires_visual_review': True}
        atomic_json(receipt, result)
        return result
    finally:
        if window:
            window.scene = previous_scene
        bpy.data.objects.remove(clone, do_unlink=True); bpy.data.objects.remove(sun, do_unlink=True)
        bpy.data.scenes.remove(scene); bpy.data.meshes.remove(mesh)
        bpy.data.materials.remove(mat); bpy.data.lights.remove(sun_data); bpy.data.images.remove(target)


def prepare(worker):
    import bpy
    profiles = load_profiles(resource_root(), worker.cfg.get('character_material_profiles_dir', ''))
    heads = [b['exported'] for b in worker.manifest['bones']
             if b['original'].lower() in ('頭', '头', 'head', 'bip001-head', '頭部')]
    head = heads[0] if len(heads) == 1 else None
    for record in worker.materials.records.values():
        if record.get('pmx_index') is None:
            continue
        is_eye = is_eye_material(record)
        parent = requested_character_parent(worker.cfg,record,profiles)
        profile = resolve_profile(profiles, parent, 'auto' if is_eye else worker.cfg.get('character_material_profile', 'auto'))
        if not profile:
            continue
        note = record.setdefault('character_material', {})
        note.update(profile=profile['id'], profile_hash=fingerprint(profile), role=material_role(record, profile),
                    sources={k: {'kind': 'pmx_index', 'path': v['path']} for k, v in record.get('textures', {}).items()})
        import numpy as np
        base_spec = record.get('textures',{}).get('base_color')
        note['alpha_mode'] = 'opaque'
        if record.get('mmd',{}).get('diffuse_alpha',1)<=0:
            note['alpha_mode']='masked'
        elif record.get('mmd',{}).get('diffuse_alpha',1)<.999:
            note['alpha_mode'] = 'translucent'
        elif base_spec:
            image = bpy.data.images.load(base_spec['path'],check_existing=False)
            try:
                pixels = np.empty(image.size[0]*image.size[1]*4,np.float32)
                image.pixels.foreach_get(pixels)
                alpha = pixels[3::4]
                if (alpha<.999).any():
                    try:
                        alpha=material_alpha_samples(worker,record,image,alpha)
                        note['alpha_source']='material_uv'
                    except ValueError as exc:
                        note['alpha_source']='whole_texture_fallback'
                        note['alpha_review_reason']=str(exc)
                        record['requires_visual_review']=True
                    if (alpha<.999).any():
                        note['alpha_mode'] = 'translucent' if ((alpha>.01)&(alpha<.99)).mean()>.01 else 'masked'
            finally:
                bpy.data.images.remove(image)
        bound, candidates = discover_sources(worker.data['settings'].get('model') or worker.cfg['model'], record, profile)
        note['source_candidates'] = candidates
        for semantic, path in bound.items():
            image = bpy.data.images.load(path, check_existing=False)
            try:
                record.setdefault('textures', {})[semantic] = worker.materials.image(image)
                note['sources'][semantic] = {'kind': 'profile_rule', 'path': path}
            finally:
                bpy.data.images.remove(image)
        generated = worker.bundle / 'CharacterMaterials'
        for semantic, recipe in profile.get('packed_textures', {}).items():
            if semantic not in record.get('textures', {}):
                record.setdefault('textures', {})[semantic] = pack_texture(generated, semantic, recipe, record, profile)
                note['sources'][semantic] = {'kind': 'generated', 'recipe': recipe}
        if profile.get('sdf', {}).get('format', 'none') == 'none':
            note['sdf'] = {'format':'none','status':'disabled'}
            continue
        if note['role'] != 'face':
            continue
        try:
            if not head:
                raise ValueError('没有唯一可识别的头骨')
            matches = [(obj, mat) for obj in worker.meshes for mat in obj.data.materials
                       if mat and mat.name in (record['source_name'], record['asset_name'])]
            if len(matches) != 1:
                raise ValueError('面部材质对应多个网格，需明确面部选择')
            sdf = bake_face(worker, *matches[0], record, profile, head)
            note['sdf'] = sdf
            if sdf['format'] == 'threshold_lr':
                record['textures']['face_sdf'] = sdf['threshold']
            else:
                record['textures']['sdf_atlas'] = sdf['atlas']; record['textures']['sdf_coverage'] = sdf['coverage']
            record['requires_visual_review'] = True
        except Cancelled:
            raise
        except Exception as exc:
            if '用户停止' in str(exc):
                raise
            note['sdf'] = {'format': 'none', 'status': 'needs_review', 'reason': str(exc)}
            record['requires_visual_review'] = True
            record.setdefault('warnings', []).append('面部 SDF 回退普通光照: ' + str(exc))
