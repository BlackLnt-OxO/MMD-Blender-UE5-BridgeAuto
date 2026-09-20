import unreal as u,json
from pathlib import Path
root=Path('D:/Codex_Projects/MMD_Blender_UE5_Autowork')
folder=Path((root/'work/current_job.txt').read_text().strip())
report=json.loads((folder/'ue_report.json').read_text(encoding='utf-8'))
seq=u.load_asset(report['assets']['sequence'])
out=[]
for binding in seq.get_bindings():
    row={'name':binding.get_name(),'tracks':[]}
    for track in binding.get_tracks():
        tr={'class':track.get_class().get_name(),'sections':[]}
        for section in track.get_sections():
            channels=[]
            for ch in section.get_all_channels():
                if hasattr(ch,'get_keys'):
                    keys=ch.get_keys()
                    channels.append({'name':str(ch.channel_name),'keys':[(k.get_time().frame_number.value,k.get_value()) for k in keys[:2]],'count':len(keys),'default':str(ch.get_default()) if ch.has_default() else None})
            tr['sections'].append(channels)
        row['tracks'].append(tr)
    out.append(row)
(folder/'camera_tracks.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
