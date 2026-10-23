from pathlib import Path
from mmd_bridge.core import Job,atomic_json


def test_module_status_shows_independent_failure_progress_and_output(tmp_path,monkeypatch):
    from PySide6 import QtWidgets as W
    from mmd_bridge.app import Window
    monkeypatch.setenv('MMDBRIDGE_HOME',str(tmp_path/'home'))
    monkeypatch.setattr(Window,'scan',lambda _:None)
    app=W.QApplication.instance() or W.QApplication([])
    window=Window();window.timer.stop()
    job=Job(tmp_path/'parent');result=tmp_path/'scene-result';result.mkdir()
    atomic_json(job.path,{'module_jobs':[
        {'id':'scene','state':'failed','job':str(result),'message':'USD import fatal'},
        {'id':'character_0001','state':'running','job':'','message':''},
        {'id':'camera','state':'complete','job':'','message':'Camera imported'}]})
    window.controller.job=job
    window.update_module_status({'module':'character_0001','module_state':'mmd_render','message':'645/6450'})
    assert not window.module_tree.isHidden()
    assert window.module_tree.topLevelItemCount()==3
    assert window.module_tree.topLevelItem(0).text(1)=='失败'
    assert window.module_tree.topLevelItem(1).text(2)=='645/6450'
    assert window.module_tree.topLevelItem(2).text(1)=='完成'
    opened=[];monkeypatch.setattr(window,'reveal',opened.append)
    window.open_module_result(window.module_tree.topLevelItem(0))
    assert opened==[result]
    window.controller.job=None;window.update_module_status({})
    assert window.module_tree.isHidden()
    window.close()
