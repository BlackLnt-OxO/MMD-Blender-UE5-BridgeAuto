import os,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
os.environ['QT_QPA_PLATFORM']='offscreen'
os.environ['MMDBRIDGE_HOME']=str(root/'work/gui')
from PySide6 import QtCore as C,QtWidgets as W,QtGui as G
from mmd_bridge.app import Window,configure_app
app=W.QApplication([])
configure_app(app)
window=Window()
window.resize(1200,900)
window.sample()
window.show()
def capture():
    if window.workers:
        C.QTimer.singleShot(500,capture)
        return
    window.grab().save(str(root/'work/gui.png'))
    print(window.settings())
    window.close()
    app.quit()
C.QTimer.singleShot(2000,capture)
app.exec()
