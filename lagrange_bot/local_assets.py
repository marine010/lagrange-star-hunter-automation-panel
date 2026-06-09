from __future__ import annotations

import mmap
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_GAME_DIR_NAME = "\u65e0\u5c3d\u7684\u62c9\u683c\u6717\u65e5"
NPK_NAMES = ["script.npk", "res_1.npk", "res_2.npk", "res_3.npk", "res_4.npk", "res_5.npk"]
TAIL_SCAN_BYTES = 24 * 1024 * 1024

ASCII_RUN_RE = re.compile(rb"[\x20-\x7e]{6,}")
RESOURCE_EXTENSIONS = (
    ".png",
    ".dds",
    ".jpg",
    ".jpeg",
    ".webp",
    ".json",
    ".lua",
    ".csb",
    ".plist",
    ".atlas",
    ".bytes",
    ".txt",
    ".gim",
    ".mesh",
    ".mtg",
    ".lod",
    ".npse",
)
RESOURCE_HINTS = (
    "cocosui\\",
    "cocosui/",
    "model\\",
    "model/",
    "asset_cfg\\",
    "asset_cfg/",
    "scene\\",
    "scene/",
    "nxparticle_cache\\",
    "nxparticle_cache/",
    "wwise",
)

DEFAULT_KEYWORDS = [
    "CAS066",
    "FG300",
    "\u5361\u5229\u83b1\u6069",
    "\u523a\u6c34\u6bcd",
    "\u82d4\u539f",
    "\u96f7\u91cc\u4e9a\u7279",
    "\u96e8\u6d77",
    "\u960b\u795e\u661f",
    "\u4f24\u5bb3\u63d0\u5347",
    "\u63a9\u62a4\u627f\u4f24",
    "\u591a\u76ee\u6807\u5c04\u51fb",
    "\u9632\u5fa1\u60c5\u62a5\u540c\u6b65",
    "\u6218\u573a\u589e\u76ca",
]
DEFAULT_PATH_FILTERS = [
    "ship_blueprint",
    "img_blueprint",
    "blueprint_weaponinfo",
    "icon_tactics",
    "icon_amplifier",
    "shield",
    "target",
]
KEYWORD_ENCODINGS = ("utf-8", "gbk", "utf-16le")


@dataclass(frozen=True)
class GameRootProbe:
    path: Path
    source: str


def find_game_root(explicit_path: str | None = None) -> GameRootProbe | None:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if path.exists():
            return GameRootProbe(path=path, source="explicit")
        return None

    for drive in ("D:/", "C:/", "E:/", "F:/"):
        root = Path(drive)
        if not root.exists():
            continue
        direct = root / DEFAULT_GAME_DIR_NAME
        if direct.exists():
            return GameRootProbe(path=direct.resolve(), source=f"{drive}{DEFAULT_GAME_DIR_NAME}")
        for child in root.iterdir():
            if child.is_dir() and child.name == DEFAULT_GAME_DIR_NAME:
                return GameRootProbe(path=child.resolve(), source=f"{drive} scan")
    return None


def inspect_local_game(
    explicit_path: str | None = None,
    keywords: list[str] | None = None,
    path_filters: list[str] | None = None,
    max_paths_per_package: int = 80,
    max_keyword_hits: int = 8,
    scan_keywords: bool = False,
) -> dict[str, Any]:
    probe = find_game_root(explicit_path)
    if not probe:
        return {
            "found": False,
            "game_root": explicit_path,
            "message": "Game directory was not found.",
        }

    root = probe.path
    npk_paths = [root / name for name in NPK_NAMES]
    keywords = keywords if keywords is not None else DEFAULT_KEYWORDS
    path_filters = path_filters if path_filters is not None else DEFAULT_PATH_FILTERS

    packages = []
    resource_path_hits: list[dict[str, Any]] = []
    keyword_hits: dict[str, list[dict[str, Any]]] = {keyword: [] for keyword in keywords}

    for path in npk_paths:
        if not path.exists():
            packages.append({"name": path.name, "exists": False})
            continue

        header = _read_header(path)
        packages.append(
            {
                "name": path.name,
                "exists": True,
                "size": path.stat().st_size,
                "header_ascii": _ascii_preview(header),
                "header_hex": header.hex(" ").upper(),
            }
        )

        resource_path_hits.extend(
            _scan_resource_paths(
                path,
                filters=path_filters,
                max_hits=max_paths_per_package,
            )
        )
        if scan_keywords:
            _merge_keyword_hits(
                keyword_hits,
                _scan_keywords(path, keywords=keywords, max_hits_per_keyword=max_keyword_hits),
                max_hits=max_keyword_hits,
            )

    return {
        "found": True,
        "game_root": str(root),
        "root_source": probe.source,
        "read_only": True,
        "packages": packages,
        "client_files": _summarize_client_files(root),
        "resource_path_filters": path_filters,
        "resource_path_hits": resource_path_hits,
        "keyword_hits": keyword_hits,
        "keyword_scan_enabled": scan_keywords,
        "notes": [
            "NXPK packages are readable as files, but extracting original image bytes still needs an index/decompression parser.",
            "This report only performs read-only directory, header, string, and path-table scans.",
            "Use --scan-keywords when you want a slower full-package scan for Chinese card/skill names.",
        ],
    }


def _read_header(path: Path, size: int = 16) -> bytes:
    with path.open("rb") as handle:
        return handle.read(size)


def _ascii_preview(data: bytes) -> str:
    return "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in data)


def _summarize_client_files(root: Path) -> dict[str, Any]:
    client = root / "client"
    interesting = [
        "infinite_lagrange_cn.exe",
        "npk.dll",
        "npk_zstd.dll",
        "nxfilesystem.dll",
        "DirectXTex.dll",
        "python311.dll",
        "neox.npyd.dll",
    ]
    files = []
    for name in interesting:
        path = client / name
        files.append({"name": name, "exists": path.exists(), "size": path.stat().st_size if path.exists() else None})
    return {"path": str(client), "exists": client.exists(), "interesting_files": files}


def _scan_resource_paths(path: Path, filters: list[str], max_hits: int) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    lowered_filters = [item.lower() for item in filters if item]

    with path.open("rb") as handle:
        mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            start = _path_table_start(mm)
            region = mm[start:]
            for match in ASCII_RUN_RE.finditer(region):
                text = match.group().decode("ascii", errors="ignore")
                if not _looks_like_resource_path(text):
                    continue
                lower = text.lower()
                matched = [item for item in lowered_filters if item in lower]
                if lowered_filters and not matched:
                    continue
                hits.append(
                    {
                        "package": path.name,
                        "offset": start + match.start(),
                        "path": text,
                        "matched_filters": matched,
                    }
                )
                if len(hits) >= max_hits:
                    break
        finally:
            mm.close()

    return hits


def _path_table_start(mm: mmap.mmap) -> int:
    tail_start = max(0, len(mm) - TAIL_SCAN_BYTES)
    marker = mm.rfind(b"NXFN", tail_start)
    return marker if marker >= 0 else tail_start


def _looks_like_resource_path(text: str) -> bool:
    if len(text) > 260:
        return False
    lower = text.lower()
    if not any(hint in lower for hint in RESOURCE_HINTS):
        return False
    if not any(extension in lower for extension in RESOURCE_EXTENSIONS):
        return False
    return "\\" in text or "/" in text


def _scan_keywords(path: Path, keywords: list[str], max_hits_per_keyword: int) -> dict[str, list[dict[str, Any]]]:
    hits: dict[str, list[dict[str, Any]]] = {keyword: [] for keyword in keywords}
    if not keywords:
        return hits

    with path.open("rb") as handle:
        mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for keyword in keywords:
                for encoding in KEYWORD_ENCODINGS:
                    needle = keyword.encode(encoding, errors="ignore")
                    if not needle:
                        continue
                    pos = mm.find(needle)
                    while pos >= 0 and len(hits[keyword]) < max_hits_per_keyword:
                        hits[keyword].append(
                            {
                                "package": path.name,
                                "offset": pos,
                                "encoding": encoding,
                                "context": _decode_context(mm, pos, len(needle)),
                            }
                        )
                        pos = mm.find(needle, pos + 1)
                    if len(hits[keyword]) >= max_hits_per_keyword:
                        break
        finally:
            mm.close()

    return hits


def _decode_context(mm: mmap.mmap, offset: int, needle_length: int, padding: int = 120) -> str:
    start = max(0, offset - padding)
    end = min(len(mm), offset + needle_length + padding)
    data = mm[start:end]
    best = ""
    for encoding in KEYWORD_ENCODINGS:
        text = data.decode(encoding, errors="ignore")
        printable = "".join(ch if ch.isprintable() else " " for ch in text)
        printable = " ".join(printable.split())
        if len(printable) > len(best):
            best = printable
    return best[:320]


def _merge_keyword_hits(
    base: dict[str, list[dict[str, Any]]],
    incoming: dict[str, list[dict[str, Any]]],
    max_hits: int,
) -> None:
    for keyword, hits in incoming.items():
        target = base.setdefault(keyword, [])
        remaining = max_hits - len(target)
        if remaining > 0:
            target.extend(hits[:remaining])
