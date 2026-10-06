import os,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['MMDBRIDGE_HOME']=str(root/'work/gui')
from PySide6 import QtCore as C,QtWidgets as W,QtGui as G
from mmd_bridge.app import Window,configure_app
from mmd_bridge.core import atomic_json
app=W.QApplication([])
configure_app(app)
window=Window()
window.resize(1200,900)
job=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else None
output=Path(sys.argv[2]).resolve() if len(sys.argv)>2 else root/'work/gui.png'
if not job:
    window.sample()
window.show()
def capture():
    if window.workers:
        C.QTimer.singleShot(500,capture)
        return
    window.timer.stop()
    if job:
        browse=W.QFileDialog.getOpenFileName
        try:
            W.QFileDialog.getOpenFileName=lambda *a,**kw:(str(job/'job.json'),'')
            window.scene_result_button.click()
        finally:
            W.QFileDialog.getOpenFileName=browse
        assert window.controller.job.folder==job
        if len(sys.argv)>3:
            combo=window.inputs['scene_render_pipeline']
            combo.setCurrentIndex(combo.findData(sys.argv[3]))
        window.tabs.setCurrentIndex(window.scene_appearance_tab_index)
    output.parent.mkdir(parents=True,exist_ok=True)
    buttons=[]
    for button in window.findChildren(W.QPushButton):
        tab=next((window.tabs.tabText(i) for i in range(window.tabs.count())
                  if window.tabs.widget(i).isAncestorOf(button)),'主入口')
        buttons.append({'tab':tab,'button':button.text(),'input_key':button.property('input_key'),
                        'signal_connected':bool(button.receivers(C.SIGNAL('clicked(bool)'))),
                        'enabled':button.isEnabled()})
    assert all(button['signal_connected'] for button in buttons)
    assert window.grab().save(str(output))
    if job:
        scroll=window.tabs.currentWidget()
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        app.processEvents()
        assert window.grab().save(str(output.with_name(output.stem+'_bottom.png')))
        for button in (window.show_blender_button,window.show_ue_button,window.show_comparison_button):
            if button.isEnabled():
                before=set(window.findChildren(W.QDialog))
                button.click()
                dialogs=set(window.findChildren(W.QDialog))-before
                assert len(dialogs)==1
                dialog=dialogs.pop()
                assert any(label.pixmap() and not label.pixmap().isNull() for label in dialog.findChildren(W.QLabel))
                dialog.close()
    atomic_json(output.with_suffix('.buttons.json'),{'count':len(buttons),'all_connected':True,'buttons':buttons,
                'image_viewers_checked':bool(job),'note':'信号连接与真实图像窗口检查；行为测试另见 buttons.junit.xml'})
    print({'buttons':len(buttons),'screenshot':str(output),'job':str(job) if job else None})
    window.controller.job=None
    window.close()
    app.quit()
C.QTimer.singleShot(2000,capture)
app.exec()
