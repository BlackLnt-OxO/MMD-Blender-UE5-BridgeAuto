"""Fix PMX bone names for VMD's 15-byte CP932 slots, keyed by bone index."""
from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .pmx_model import PMXModel

MAX_NAME_BYTES = 15
VMD_DUPLICATE_SOURCE_BONE_AMBIGUOUS = "VMD_DUPLICATE_SOURCE_BONE_AMBIGUOUS"


class BoneNameError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class BoneNameMapping:
    bone_index: int
    original: str
    fixed: str


@lru_cache(maxsize=1)
def _converter():
    from vendor.python.opencc import OpenCC

    mapping_file = (Path(__file__).resolve().parents[1] / "vendor" / "python" /
                    "opencc" / "dictionary" / "JPVariants.txt")
    japanese = dict(line.split("\t", 1) for line in
                    mapping_file.read_text(encoding="utf-8").splitlines() if "\t" in line)
    return OpenCC("s2t"), str.maketrans(japanese)


def _compatible(text: str) -> str:
    chinese, japanese = _converter()
    converted = unicodedata.normalize("NFKC", chinese.convert(text)).translate(japanese)
    return "".join(char for char in converted if _encodable(char)) or "bone"


def _encodable(text: str) -> bool:
    try:
        text.encode("cp932")
    except UnicodeEncodeError:
        return False
    return True


def _truncate(text: str, limit: int) -> str:
    result = ""
    for char in text:
        if len((result + char).encode("cp932")) > limit:
            break
        result += char
    return result


def fix_bone_names(model: PMXModel, source_track_names=()) -> list[BoneNameMapping]:
    source_track_names = set(source_track_names)
    counts = Counter(bone.name_jp for bone in model.bones)
    ambiguous = set(source_track_names) & {name for name, count in counts.items() if count > 1}
    if ambiguous:
        raise BoneNameError(VMD_DUPLICATE_SOURCE_BONE_AMBIGUOUS,
                            f"源 VMD 按名称索引，重复 PMX 骨骼名无法定位 Bone Index: {sorted(ambiguous)!r}")

    used: set[str] = set()
    mappings = []
    for bone in model.bones:
        original = bone.name_jp
        if original and _encodable(original) and len(original.encode("cp932")) <= MAX_NAME_BYTES and original not in used:
            fixed = original
        else:
            base = _compatible(original)
            suffix = 1
            while True:
                tail = "" if suffix == 1 else str(suffix)
                fixed = _truncate(base, MAX_NAME_BYTES - len(tail)) + tail
                if fixed not in used and (fixed not in source_track_names or fixed == original):
                    break
                suffix += 1
        used.add(fixed)
        mappings.append(BoneNameMapping(bone.index, original, fixed))
    return mappings
