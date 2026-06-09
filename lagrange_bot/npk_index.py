from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


INDEX_RECORD_SIZE = 28
HEADER_SIZE = 32
FILENAME_TABLE_MAGIC = b"NXFN"


@dataclass(frozen=True)
class NpkEntry:
    package: str
    index: int
    path: str | None
    file_id: int
    offset: int
    packed_size: int
    unpacked_size: int
    packed_hash: int
    unpacked_hash: int
    flags: int

    @property
    def is_stored(self) -> bool:
        return self.flags == 0 and self.packed_size == self.unpacked_size


@dataclass(frozen=True)
class NpkPackageIndex:
    path: Path
    count: int
    index_offset: int
    filename_table_offset: int | None
    filename_table_size: int | None
    entries: list[NpkEntry]


def read_npk_index(path: Path) -> NpkPackageIndex:
    with path.open("rb") as handle:
        mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            if mm[:4] != b"NXPK":
                raise ValueError(f"{path} is not an NXPK package")
            count = struct.unpack_from("<I", mm, 4)[0]
            index_offset = struct.unpack_from("<I", mm, 20)[0]
            index_end = index_offset + count * INDEX_RECORD_SIZE
            filename_table_offset = index_end if mm[index_end:index_end + 4] == FILENAME_TABLE_MAGIC else None
            names: list[str | None] = []
            filename_table_size: int | None = None
            if filename_table_offset is not None:
                filename_table_size = struct.unpack_from("<I", mm, filename_table_offset + 8)[0]
                names = _read_filename_table(mm, filename_table_offset, count)

            entries = []
            for index in range(count):
                record_offset = index_offset + index * INDEX_RECORD_SIZE
                values = struct.unpack_from("<IIIIIII", mm, record_offset)
                path_text = names[index] if index < len(names) else None
                entries.append(
                    NpkEntry(
                        package=path.name,
                        index=index,
                        path=path_text,
                        file_id=values[0],
                        offset=values[1],
                        packed_size=values[2],
                        unpacked_size=values[3],
                        packed_hash=values[4],
                        unpacked_hash=values[5],
                        flags=values[6],
                    )
                )
        finally:
            mm.close()

    return NpkPackageIndex(
        path=path,
        count=count,
        index_offset=index_offset,
        filename_table_offset=filename_table_offset,
        filename_table_size=filename_table_size,
        entries=entries,
    )


def extract_stored_entry(package_path: Path, entry: NpkEntry) -> bytes:
    if not entry.is_stored:
        raise ValueError(f"{entry.path or entry.index} is compressed or encrypted; flags={entry.flags}")
    with package_path.open("rb") as handle:
        handle.seek(entry.offset)
        return handle.read(entry.packed_size)


def iter_matching_entries(
    indexes: Iterable[NpkPackageIndex],
    include: Iterable[str],
) -> Iterable[NpkEntry]:
    needles = [item.lower() for item in include if item]
    for package in indexes:
        for entry in package.entries:
            if not entry.path:
                continue
            lower = entry.path.lower()
            if all(needle in lower for needle in needles):
                yield entry


def _read_filename_table(mm: mmap.mmap, offset: int, count: int) -> list[str | None]:
    table_size = struct.unpack_from("<I", mm, offset + 8)[0]
    start = offset + 16
    end = start + table_size
    raw = bytes(mm[start:end])

    names: list[str | None] = []
    for part in raw.split(b"\0"):
        if not part:
            continue
        names.append(part.decode("utf-8", errors="replace"))
        if len(names) >= count:
            break

    while len(names) < count:
        names.append(None)
    return names
