"""Click the real Qt scene controls, dispatch to deployed UE, and verify its receipt."""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6 import QtCore as C, QtWidgets as W
from mmd_bridge.app import Window, configure_app
from mmd_bridge.core import atomic_json, read_json

app = W.QApplication([])
configure_app(app)
window = Window()
folder = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / 'work/scene_appearance_acceptance/jobs/Scene_street_01'
kind = sys.argv[2] if len(sys.argv) > 2 else 'ue'
window.timer.stop()
window.resize(1200, 950)
window.show()
state = {'started': False, 'ticks': 0}

def tick():
    if window.workers:
        return
    state['ticks'] += 1
    if not state['started']:
        window.update_scene_actions()
        W.QFileDialog.getOpenFileName = lambda *a, **k: (str(folder / 'job.json'), '')
        window.scene_result_button.click()
        assert window.controller.job.folder == folder
        if len(sys.argv) > 3:
            window.inputs['scene_render_pipeline'].setCurrentIndex(window.inputs['scene_render_pipeline'].findData(sys.argv[3]))
        state['pipeline'] = window.inputs['scene_render_pipeline'].currentData()
        state['source_bytes'] = [(path, path.read_bytes()) for path in (folder / 'job.json', folder / 'manifest.json')]
        window.tabs.setCurrentIndex(window.scene_appearance_tab_index)
        assert (window.ue_reference_button if kind == 'ue' else window.blender_reference_button).isEnabled()
        window.grab().save(str(ROOT / 'work/gui_scene_appearance.png'))
        (window.ue_reference_button if kind == 'ue' else window.blender_reference_button).click()
        assert window.controller.reference_busy()
        state['started'] = True
        return
    status = read_json(folder / 'status.json')
    if status['state'] in ('needs_review', 'failed', 'stopped'):
        window.poll()
        receipt = read_json(folder / ('visual/ue_render.json' if kind == 'ue' else 'visual/reference.json'))
        assert status['state'] == 'needs_review' and (receipt['complete'] if kind == 'ue' else (folder / 'visual/blender.png').is_file()), (status, receipt)
        assert all(path.read_bytes() == content for path, content in state['source_bytes'])
        if kind == 'ue':
            assert receipt['renderer']['mode'] == state['pipeline']
            assert receipt['renderer']['pass'] == ('MoviePipelineDeferredPassBase' if state['pipeline'] == 'lumen' else 'MoviePipelineDeferredPass_PathTracer')
            from mmd_bridge.core import file_hash
            assert receipt['image_sha256'] == file_hash(folder / 'visual/ue.png')
            if state['pipeline'] == 'lumen':
                assert receipt['renderer']['ShowFlag.PathTracing'] == 0
                assert receipt['renderer']['path_tracing_force_off_observed']
                assert receipt['renderer']['actual_passes'] == ['MoviePipelineDeferredPassBase']
                assert receipt['appearance']['console_variables']['r.Lumen.TranslucencyVolume.Enable'] == 1
                assert receipt['appearance']['console_variables']['r.HeterogeneousVolumes.MaxStepCount'] == 1024
            assert receipt['source_camera'] and not receipt.get('camera_error')
        if kind == 'blender':
            atomic_json(ROOT / 'work/gui_blender_reference_native.json', {'complete': True, 'job': str(folder), 'receipt': receipt})
            window.controller.job = None
            window.close()
            app.quit()
            return
        window.comparison_button.click()
        # The comparison Worker writes before publishing its Qt result.
        for worker in list(window.workers):
            worker.wait(30000)
        app.processEvents()
        assert (folder / 'visual/comparison.png').is_file()
        assert read_json(folder / 'visual/difference.json')['visual_status'] == 'needs_review'
        window.tabs.setCurrentIndex(window.scene_appearance_tab_index)
        window.grab().save(str(ROOT / 'work/gui_scene_appearance_complete.png'))
        atomic_json(ROOT / 'work/gui_scene_native.json', {'complete': True, 'job': str(folder),
                    'buttons': ['选择已转换场景任务', '渲染 UE 参考帧', '生成对照与差分', '查看对照图'], 'receipt': receipt})
        window.controller.job = None
        window.close()
        app.quit()
    elif state['ticks'] > 1200:
        raise RuntimeError('UE 参考帧 20 分钟仍未完成，请查看任务日志')

timer = C.QTimer()
def checked_tick():
    try:
        tick()
    except Exception:
        import traceback
        timer.stop()
        atomic_json(ROOT / 'work/gui_scene_native_error.json', {'error': traceback.format_exc()})
        app.exit(1)

timer.timeout.connect(checked_tick)
timer.start(1000)
app.exec()
