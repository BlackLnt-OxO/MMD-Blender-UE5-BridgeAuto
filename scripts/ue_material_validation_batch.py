"""Sequential native checks in one process, restricted by each script's guard."""
import runpy
from pathlib import Path
root=Path(__file__).resolve().parent
for name in ('ue_material_functions_regression.py','ue_generated_reload_validation.py','ue_shader_data_gpu_validation.py'):
    runpy.run_path(str(root/name),run_name='__main__')
