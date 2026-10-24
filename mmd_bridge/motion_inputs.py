"""Ordered supplementary VMD inputs and their task-local merged copy."""
from fractions import Fraction
from pathlib import Path

from .core import atomic_json, file_hash, read_json
from .vmd_writer import merge_vmds


def supplementary_paths(cfg):
    """A nonempty new list is authoritative; lipsync remains the legacy input."""
    return list(cfg.get('supplementary_motions') or [])


def supplementary_offset(cfg):
    offset = Fraction((cfg.get('lipsync_offset', 0) - cfg.get('motion_offset', 0)) * 30,
                      cfg.get('fps', 30))
    if offset.denominator != 1:
        raise ValueError('补充动作与主动作的偏移差必须对应整数 VMD 帧（原始 30fps）')
    return int(offset)


def prepare_motion_inputs(cfg, folder):
    """Old tasks retain two-input behavior; new lists are merged before import."""
    supplements = supplementary_paths(cfg)
    if not supplements:
        return cfg['motion'], cfg.get('lipsync', ''), None
    folder = Path(folder) / 'motion_inputs'
    paths = [cfg['motion'], *supplements]
    offsets = [0, *([supplementary_offset(cfg)] * len(supplements))]
    sources = [{'path': str(Path(path).resolve()), 'sha256': file_hash(path), 'offset': offset}
               for path, offset in zip(paths, offsets)]
    output = folder / 'merged.vmd'
    marker = folder / 'merge.json'
    if marker.is_file():
        report = read_json(marker)
        if (report.get('version') != 1 or report.get('policy') != 'later_track_wins' or
                report.get('sources') != sources or report.get('output') != str(output.resolve()) or
                not output.is_file() or report.get('sha256') != file_hash(output)):
            raise ValueError('补充动作合并记录或输入已改变；请新建任务')
    else:
        report = merge_vmds(paths, output, offsets=offsets)
        report.update(sources=sources, output=str(output.resolve()), sha256=file_hash(output))
        atomic_json(marker, report)
    return str(output), '', report
