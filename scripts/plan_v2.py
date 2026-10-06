"""File-only Plan V2 entry point; MMD writes baked.vmd between these commands."""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mmd_bridge.plan_v2_prep import (PreparedCharacterV2,
                                     finalize_character_v2, prepare_character_v2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--pmx", required=True)
    prepare.add_argument("--motion", required=True)
    prepare.add_argument("--lipsync")
    prepare.add_argument("--job", required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--job", required=True)
    finalize.add_argument("--baked", required=True)
    args = parser.parse_args(argv)
    job = Path(args.job).resolve()
    manifest = job / "prepared.json"
    if args.command == "prepare":
        if manifest.exists():
            raise ValueError("任务已准备；请使用新工作目录")
        prepared = prepare_character_v2(args.pmx, args.motion, args.lipsync, job)
        temporary = manifest.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(prepared), default=str,
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, manifest)
        print(manifest)
    else:
        values = json.loads(manifest.read_text(encoding="utf-8"))
        for field in ("source_model", "source_motion", "model", "motion", "name_map"):
            values[field] = Path(values[field])
        values["lipsync"] = Path(values["lipsync"]) if values["lipsync"] else None
        values["frame_range"] = tuple(values["frame_range"])
        values["expected_bones"] = tuple(values["expected_bones"])
        values.setdefault("renamed_bone_count", 0)
        values.setdefault("vertex_weight_counts", {})
        print(finalize_character_v2(PreparedCharacterV2(**values), args.baked, job))


if __name__ == "__main__":
    main()
