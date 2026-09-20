from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mmd_bridge.app import main
if __name__ == "__main__":
    # Diagnostic entry point uses exactly the same child launcher as the GUI.
    if '--run-blender-stage' in sys.argv:
        from mmd_bridge.controller import Controller
        from mmd_bridge.core import Job
        index = sys.argv.index('--run-blender-stage')
        controller = Controller()
        controller.job = Job(Path(sys.argv[index + 1]))
        mode = sys.argv[index + 2]
        if mode not in ('character', 'scene'):
            raise ValueError('Expected character or scene')
        controller.launch_blender(mode)
        code = controller.processes[-1][1].wait()
        for handle in controller.log_handles:
            handle.close()
        raise SystemExit(code)
    raise SystemExit(main())
