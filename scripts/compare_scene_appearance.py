"""Same-frame evidence using the GUI's existing Qt dependency."""
import os, sys
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase
from mmd_bridge.scene_reference import compare
app = QApplication.instance() or QApplication([])
if os.name == 'nt':
    font = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/msyh.ttc'
    if font.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 11))
print(compare(Path(sys.argv[1]).resolve()))
