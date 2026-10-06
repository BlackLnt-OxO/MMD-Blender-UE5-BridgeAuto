"""PMX binary primitives: variable-width signed indices and UTF-16LE strings.

PMX stores every index in a width declared by the file header (1, 2 or 4 bytes),
and index-like references may be -1 to mean "none".  Reading them as int32 would
silently corrupt any model that uses the compact encodings, so the width must be
carried around explicitly (see SPEC §17 / 任务书 §17).
"""
from __future__ import annotations

import struct

INDEX_SIZES = (1, 2, 4)

_SIGNED = {1: "<b", 2: "<h", 4: "<i"}
_UNSIGNED = {1: "<B", 2: "<H", 4: "<I"}


class PMXFormatError(ValueError):
    """Raised when the byte stream does not match the PMX specification."""


def _check_size(size: int, what: str) -> None:
    if size not in INDEX_SIZES:
        raise PMXFormatError(f"{what} index size 非法: {size}（只允许 1/2/4）")


class ByteReader:
    """Sequential reader that tracks its own position for section slicing."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.text_encoding = "utf-16-le"

    # -- low level -------------------------------------------------------
    def _take(self, count: int) -> bytes:
        end = self.pos + count
        if end > len(self.data):
            raise PMXFormatError(
                f"读取越界: 需要在 {self.pos} 处读取 {count} 字节，文件仅 {len(self.data)} 字节")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def read_int(self, size: int, signed: bool = False) -> int:
        _check_size(size, "整数")
        fmt = (_SIGNED if signed else _UNSIGNED)[size]
        return struct.unpack(fmt, self._take(size))[0]

    def read_float(self) -> float:
        return struct.unpack("<f", self._take(4))[0]

    def read_index(self, size: int) -> int:
        """Index width comes from the header; always signed because -1 is legal."""
        return self.read_int(size, signed=True)

    def read_text(self) -> str:
        length = self.read_int(4, signed=True)
        if length < 0:
            raise PMXFormatError(f"字符串长度非法: {length}")
        return self._take(length).decode(self.text_encoding, errors="replace")

    def skip(self, count: int) -> None:
        self._take(count)


class ByteWriter:
    """Counterpart of ByteReader; mirrors the source file's index widths."""

    def __init__(self, text_encoding: str = "utf-16-le"):
        self.chunks: list[bytes] = []
        self.text_encoding = text_encoding

    def write_raw(self, data: bytes) -> None:
        self.chunks.append(data)

    def write_int(self, value: int, size: int, signed: bool = False) -> None:
        _check_size(size, "整数")
        if size == 1 and not signed:
            value = value & 0xFF
        elif size == 2 and not signed:
            value = value & 0xFFFF
        fmt = (_SIGNED if signed else _UNSIGNED)[size]
        self.chunks.append(struct.pack(fmt, value))

    def write_index(self, value: int, size: int) -> None:
        self.write_int(value, size, signed=True)

    def write_float(self, value: float) -> None:
        self.chunks.append(struct.pack("<f", value))

    def write_text(self, text: str) -> None:
        encoded = text.encode(self.text_encoding)
        self.write_int(len(encoded), 4, signed=True)
        self.chunks.append(encoded)

    def write_vec(self, values) -> None:
        for value in values:
            self.write_float(value)

    def build(self) -> bytes:
        return b"".join(self.chunks)
