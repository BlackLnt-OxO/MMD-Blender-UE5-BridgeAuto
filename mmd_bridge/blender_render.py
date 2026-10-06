"""Consume a genuine render dependency graph without sampling an image."""
import bpy


def with_render_depsgraph(scene, consume):
    """Evaluated RNA is valid only in the callback; retain copied data instead."""
    outcome = {}

    class GeometryRender(bpy.types.RenderEngine):
        bl_idname = 'MMDBRIDGE_GEOMETRY_RENDER'
        bl_label = 'MMDBridge render geometry'
        bl_use_shading_nodes_custom = False

        def render(self, depsgraph):
            try:
                if depsgraph.mode != 'RENDER':
                    raise RuntimeError('场景导出未取得渲染求值图')
                outcome['value'] = consume(depsgraph)
            except Exception as error:
                outcome['error'] = error
            result = self.begin_result(0, 0, 1, 1)
            result.layers[0].passes['Combined'].rect = [(0, 0, 0, 1)]
            self.end_result(result)

    render = scene.render
    saved = {name: getattr(render, name) for name in (
        'engine',
        'use_sequencer', 'use_compositing', 'use_border')}
    camera = None
    bpy.utils.register_class(GeometryRender)
    try:
        if scene.camera is None:
            camera = bpy.data.objects.new('MMDBridge_GeometryCamera', bpy.data.cameras.new('MMDBridge_GeometryCamera'))
            scene.collection.objects.link(camera)
            scene.camera = camera
        render.engine = GeometryRender.bl_idname
        render.use_sequencer = render.use_compositing = render.use_border = False
        bpy.ops.render.render(scene=scene.name)
    finally:
        for name, value in saved.items():
            setattr(render, name, value)
        if camera:
            scene.camera = None
            data = camera.data
            bpy.data.objects.remove(camera, do_unlink=True)
            bpy.data.cameras.remove(data)
        bpy.utils.unregister_class(GeometryRender)
    if 'error' in outcome:
        raise outcome['error']
    if 'value' not in outcome:
        raise RuntimeError('Blender 未执行渲染几何求值，拒绝回退到视口结果')
    return outcome['value']
