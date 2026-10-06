"""Render an existing MRQ preset without opening the editor. Default: plan only."""
import argparse
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mmd_bridge.core import Job, read_json
from mmd_bridge.controller import Controller
from mmd_bridge.standalone_render import make_plan

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('job', type=Path)
parser.add_argument('--preset', help='现有 MRQ 资产路径；默认使用已保存的管线预设或 SourceLook')
parser.add_argument('--profile', choices=('balanced', 'low', 'quality', 'compatible'), help='使用已有显存策略预设')
parser.add_argument('--frame', type=int, help='只渲染指定帧；省略时沿用预设范围')
parser.add_argument('--output', default='', help='输出父目录，每次创建新的子文件夹')
parser.add_argument('--run', action='store_true')
args = parser.parse_args()
folder = args.job.resolve()
if folder.is_file(): folder = folder.parent
job = Job(folder)
cfg = job.data['settings']
assets = read_json(folder / 'ue_report.json')['assets']
preset = args.preset
if not preset and args.profile:
    preset = assets.get('memory_presets', {}).get(args.profile)
    if not preset: parser.error('任务没有保存该显存策略预设，请指定 --preset')
pipeline = cfg.get('scene_render_pipeline', 'lumen')
preset = preset or assets.get('render_presets', {}).get(pipeline) or assets.get('render_preset')
if not preset: parser.error('任务没有 MRQ 预设，请指定 --preset')
if not args.run:
    run, request, command = make_plan(job, preset, args.output, args.frame, pipeline)
    print(subprocess.list2cmdline(command))
    print('Request: ' + str(run / 'request.json'))
    print('未启动渲染。加 --run 使用此工具的后台执行入口。')
    raise SystemExit(0)
controller = Controller()
controller.load_scene_results(folder)
try:
    run = controller.render_background(preset, args.output, args.frame, pipeline)
    print('后台任务：' + str(run), flush=True)
    previous = None
    while controller.standalone_render['active']:
        status = controller.poll()
        if status.get('message') != previous:
            print(status['message'], flush=True)
            previous = status['message']
        time.sleep(1)
except KeyboardInterrupt:
    controller.stop_background()
    raise SystemExit(130)
except ValueError as exc:
    parser.error(str(exc))
result = controller.standalone_render['result']
print('输出：' + result['output'])
print('回执：' + str(run / 'result.json'))
raise SystemExit(0 if result['complete'] else 2)
