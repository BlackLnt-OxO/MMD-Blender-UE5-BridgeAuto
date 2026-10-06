"""Reference frames and comparisons; use bundled Blender and the GUI's existing Qt."""
from pathlib import Path
from .core import Job, atomic_json, check_stop, read_json, file_hash


def render_blender(folder):
    import bpy
    from .blender_appearance import capabilities, check_resources
    job = Job(folder)
    cfg = job.data['settings']
    try:
        capabilities()
        job.verify_inputs()
        check_resources(job, True)
        bpy.ops.wm.open_mainfile(filepath=cfg['scene'], load_ui=False, use_scripts=False)
        scene = bpy.context.scene
        scene.frame_set(cfg.get('scene_frame', 1))
        if not scene.camera:
            raise ValueError('源场景没有活动相机，无法渲染参考帧')
        hidden = []
        for name in cfg.get('reference_hidden_objects', []):
            obj = scene.objects.get(name)
            if obj is None:
                raise ValueError(f'参考帧排除物体不存在: {name}')
            obj.hide_render = True
            hidden.append(name)
        out = job.folder / 'visual'
        out.mkdir(exist_ok=True)
        if hasattr(scene.render.image_settings, 'media_type'):
            scene.render.image_settings.media_type = 'IMAGE'
        scene.render.image_settings.file_format = 'PNG'
        scene.render.image_settings.color_mode = 'RGBA'
        scene.render.image_settings.color_depth = '8'
        scene.render.filepath = str(out / 'blender.png')
        scene.render.resolution_percentage = 100
        if scene.render.engine == 'CYCLES':
            scene.cycles.device = 'CPU'
            scene.cycles.samples = 32
            scene.cycles.use_denoising = True
        check_resources(job, True)
        job.status('reference_render', '正在用源活动相机渲染 Blender 参考帧（32 采样）')
        bpy.ops.render.render(write_still=True)
        check_stop(job)
        atomic_json(out / 'reference.json', {'camera': scene.camera.name,
                    'frame': scene.frame_current, 'renderer': scene.render.engine,
                    'resolution': [scene.render.resolution_x, scene.render.resolution_y],
                    'pixel_aspect': [scene.render.pixel_aspect_x, scene.render.pixel_aspect_y],
                    'hidden_objects': hidden, 'image_sha256': file_hash(out / 'blender.png')})
        job.status('needs_review', 'Blender 参考帧已生成；可继续渲染 UE 并生成对照图')
    except Exception as exc:
        from .core import Cancelled
        job.status('stopped' if isinstance(exc, Cancelled) else 'failed', str(exc))
        raise


def compare(folder):
    from collections import Counter
    from PySide6 import QtCore as C, QtGui as G
    out = Path(folder) / 'visual'
    images = [G.QImage(str(out / (name + '.png'))) for name in ('blender', 'ue')]
    if any(im.isNull() for im in images):
        raise ValueError('请先生成 Blender 和 UE 参考帧')
    if images[0].size() != images[1].size():
        raise ValueError('参考帧画幅不同，不能生成验收对照')
    receipt = read_json(out / 'ue_render.json') if (out / 'ue_render.json').is_file() else {}
    hashes = {name:file_hash(out/(name+'.png')) for name in ('blender','ue')}
    if receipt.get('complete') is False or (receipt.get('image_sha256') and receipt['image_sha256'] != hashes['ue']):
        raise ValueError('UE 参考帧与成功回执不匹配，请先重新渲染 UE 参考帧')
    a, b = [im.convertToFormat(G.QImage.Format_RGBA8888) for im in images]
    pixels = bytearray(a.constBits())
    target = b.constBits()
    counts = Counter()
    for i in range(0, len(pixels), 4):
        for c in range(3):
            error = abs(pixels[i+c] - target[i+c])
            counts[error] += 1
            pixels[i+c] = min(255, error * 4)
        pixels[i+3] = 255
    heat = G.QImage(pixels, a.width(), a.height(), a.bytesPerLine(), G.QImage.Format_RGBA8888).copy()
    heat.save(str(out / 'difference.png'))
    width, height = 960, round(960 * a.height() / a.width())
    panel = G.QImage(width * 3, height + 32, G.QImage.Format_RGB32)
    panel.fill(G.QColor('#20242a'))
    painter = G.QPainter(panel)
    painter.setPen(G.QColor('white'))
    renderer = receipt.get('renderer', {})
    ue_label = {'lumen': 'UE / Lumen', 'path_tracing': 'UE / Path Tracing'}.get(renderer.get('mode'), 'UE')
    for i, (name, im) in enumerate(zip(('Blender', ue_label, 'Absolute difference x4'), (*images, heat))):
        painter.drawText(i * width + 12, 22, name)
        painter.drawImage(i * width, 32, im.scaled(width, height, C.Qt.IgnoreAspectRatio, C.Qt.SmoothTransformation))
    painter.end()
    panel.save(str(out / 'comparison.png'))
    total, cumulative, p95 = sum(counts.values()), 0, 0
    for error in sorted(counts):
        cumulative += counts[error]
        if cumulative >= total * .95:
            p95 = error / 255
            break
    for name, im in zip(('blender', 'ue'), images):
        im.scaled(1280, 720, C.Qt.KeepAspectRatio, C.Qt.SmoothTransformation).save(str(out / (name + '_preview.png')))
    atomic_json(out / 'difference.json', {'resolution': [a.width(), a.height()],
                'mean_absolute_display_difference': sum(k*v for k,v in counts.items()) / total / 255,
                'p95_absolute_display_difference': p95, 'visual_status': 'needs_review',
                'renderer': renderer,
                'image_hashes': hashes,
                'note': '像素指标不判定还原通过，跨渲染器与特殊合成差异需人工复核'})
    return out / 'comparison.png'
