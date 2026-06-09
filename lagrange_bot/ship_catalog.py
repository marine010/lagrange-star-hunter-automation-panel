from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .local_assets import NPK_NAMES, find_game_root
from .npk_index import NpkEntry, read_npk_index


SHIP_ID_RE = re.compile(
    r"(?:model\\ships|model/ships|ship_blueprint|img_blueprint\\(?:adv\\)?model(?:_new|_new_bp)?|"
    r"img_blueprint/(?:adv/)?model(?:_new|_new_bp)?|blueprint_weaponinfo(?:\\adv)?|"
    r"blueprint_weaponinfo(?:/adv)?|costum_paint_cfg\\ship|costum_paint_cfg/ship)"
    r"[\\/](?:elements[\\/])?([a-z0-9]+(?:_[a-z0-9]+){2,})(?:_ad)?(?:[\\/_.]|$)",
    re.IGNORECASE,
)

KNOWN_DECK_SHIPS = {
    "cas066": {
        "name": "CAS066综合型",
        "hints": ["066", "cas066", "c_cruiser", "cruiser"],
    },
    "fg300_armor": {
        "name": "FG300装甲型",
        "hints": ["fg300", "frigate", "s_frigate_m_001", "30101"],
    },
    "carlilion": {
        "name": "卡利莱恩级",
        "hints": ["carlilion", "carlilion", "destroyer", "407", "410"],
    },
    "tundra_support": {
        "name": "苔原支援型",
        "hints": ["tundra", "support", "s_support_l_001", "70301"],
    },
    "stingray": {
        "name": "刺水母级",
        "hints": ["stingray", "torpedo", "f_torpedo_m_001", "21201"],
    },
    "reliat": {
        "name": "雷里亚特级",
        "hints": ["reliat", "b_frigate", "304"],
    },
    "rainsea_assault": {
        "name": "雨海突击型",
        "hints": ["rainsea", "rain", "a_cruiser", "504"],
    },
    "eris": {
        "name": "阋神星级",
        "hints": ["eris", "b_destroyer", "412", "s_destroyer"],
    },
}


def build_ship_catalog(
    game_root: str | None = None,
    max_examples_per_kind: int = 10,
) -> dict[str, Any]:
    probe = find_game_root(game_root)
    if not probe:
        return {"found": False, "game_root": game_root, "ships": []}

    root = probe.path
    packages = []
    grouped: dict[str, dict[str, Any]] = {}
    script_summary: dict[str, Any] | None = None

    for name in NPK_NAMES:
        path = root / name
        if not path.exists():
            continue
        index = read_npk_index(path)
        packages.append(
            {
                "name": name,
                "count": index.count,
                "has_filenames": index.filename_table_offset is not None,
                "stored_entries": sum(1 for entry in index.entries if entry.is_stored),
                "filename_table_offset": index.filename_table_offset,
                "filename_table_size": index.filename_table_size,
            }
        )
        if name == "script.npk":
            script_summary = _summarize_script(index.entries)
            continue

        for entry in index.entries:
            if not entry.path:
                continue
            ship_id = _extract_ship_id(entry.path)
            if not ship_id:
                continue
            item = grouped.setdefault(
                ship_id,
                {
                    "ship_id": ship_id,
                    "resource_count": 0,
                    "packages": set(),
                    "blueprint_images": [],
                    "model_paths": [],
                    "weaponinfo_paths": [],
                    "paint_config_paths": [],
                    "other_paths": [],
                    "stored_files": 0,
                    "compressed_files": 0,
                },
            )
            item["resource_count"] += 1
            item["packages"].add(entry.package)
            if entry.is_stored:
                item["stored_files"] += 1
            else:
                item["compressed_files"] += 1
            _append_path_by_kind(item, entry, max_examples_per_kind)

    ships = []
    for item in grouped.values():
        item["packages"] = sorted(item["packages"])
        item["likely_class"] = _ship_class_from_id(item["ship_id"])
        item["confirmed_detail_level"] = "resource_index"
        ships.append(item)
    ships.sort(key=lambda item: (-item["resource_count"], item["ship_id"]))

    return {
        "found": True,
        "game_root": str(root),
        "read_only": True,
        "packages": packages,
        "script_summary": script_summary,
        "ships": ships,
        "deck_ship_candidates": _match_deck_candidates(ships),
        "notes": [
            "This catalog is built from NXPK filename and index tables.",
            "Ship attributes such as HP, armor, damage, fire rate, range, and targeting AI are not decoded yet.",
            "script.npk contains named-less NXZ blocks; those are the most likely location for detailed numeric tables.",
        ],
    }


def write_ship_catalog_report(catalog: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    json_path = output.with_suffix(".json")
    json_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 舰船资源读取报告",
        "",
        f"- 游戏目录: `{catalog.get('game_root')}`",
        f"- 读取方式: {'只读' if catalog.get('read_only') else '未知'}",
        f"- 识别到舰船内部 ID 数量: {len(catalog.get('ships', []))}",
        "",
        "## 资源包概况",
        "",
    ]
    for package in catalog.get("packages", []):
        lines.append(
            f"- `{package['name']}`: {package['count']} 条目, "
            f"文件名表={'有' if package['has_filenames'] else '无'}, "
            f"原样存储={package['stored_entries']}"
        )

    script = catalog.get("script_summary") or {}
    lines.extend(
        [
            "",
            "## 属性表状态",
            "",
            f"- `script.npk` 条目数: {script.get('count', 0)}",
            f"- `NXZ` 块数量: {script.get('nxz_blocks', 0)}",
            "- `nxio3.NpkReader` 已验证可以打开包、列出文件名、读取未压缩未加密条目。",
            "- 大多数舰船图片/配置条目是 `decompress_type=3, encrypt_type=1`，还需要正确 key 才能读取正文。",
            "- 结论: 数值属性很可能在这些 NXZ/加密块中，但本轮尚未完成解压/解密。",
            "",
            "## 当前卡组候选映射",
            "",
        ]
    )
    for deck_id, data in catalog.get("deck_ship_candidates", {}).items():
        lines.append(f"### {data['name']} (`{deck_id}`)")
        candidates = data.get("candidates", [])
        if not candidates:
            lines.append("")
            lines.append("- 暂未从资源路径中稳定匹配到内部 ID。")
            lines.append("")
            continue
        for candidate in candidates[:8]:
            lines.append(
                f"- `{candidate['ship_id']}`: score={candidate['score']}, "
                f"resources={candidate['resource_count']}, class={candidate['likely_class']}"
            )
            for path in candidate.get("sample_paths", [])[:3]:
                lines.append(f"  - `{path}`")
        lines.append("")

    lines.extend(["## 舰船内部 ID 索引 Top 120", ""])
    for ship in catalog.get("ships", [])[:120]:
        lines.append(
            f"### `{ship['ship_id']}`"
        )
        lines.append(
            f"- 资源数: {ship['resource_count']} | 类型推断: {ship['likely_class']} | 包: {', '.join(ship['packages'])}"
        )
        sample_paths = (
            ship.get("blueprint_images", [])[:2]
            + ship.get("weaponinfo_paths", [])[:2]
            + ship.get("model_paths", [])[:2]
            + ship.get("paint_config_paths", [])[:1]
        )
        for path in sample_paths[:6]:
            lines.append(f"- `{path}`")
        lines.append("")

    lines.extend(
        [
            "## 说明",
            "",
            "- `ship_id` 是资源路径中提取出的内部舰船/舰载机/工程船 ID，不等于中文显示名。",
            "- `resource_index` 级别表示已经从本地包索引确认该资源存在，但还没有解出数值属性表。",
            "- 下一步需要通过游戏自带 Python 3.11 `nxio3.NpkReader` 或解析 `NXZ` 容器来读取详细属性。",
        ]
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _extract_ship_id(path: str) -> str | None:
    match = SHIP_ID_RE.search(path)
    if not match:
        return None
    ship_id = match.group(1).lower()
    ship_id = re.sub(r"_(?:ad|lantu|bridge|jet|lod\d+|lq|hq|mq)$", "", ship_id)
    return ship_id


def _append_path_by_kind(item: dict[str, Any], entry: NpkEntry, max_examples: int) -> None:
    path = entry.path or ""
    lower = path.lower()
    if "ship_blueprint" in lower or "img_blueprint" in lower:
        key = "blueprint_images"
    elif "blueprint_weaponinfo" in lower:
        key = "weaponinfo_paths"
    elif "model\\ships" in lower or "model/ships" in lower:
        key = "model_paths"
    elif "costum_paint_cfg" in lower:
        key = "paint_config_paths"
    else:
        key = "other_paths"
    if len(item[key]) < max_examples:
        item[key].append(path)


def _ship_class_from_id(ship_id: str) -> str:
    parts = ship_id.split("_")
    if len(parts) < 2:
        return "unknown"
    token = parts[1]
    mapping = {
        "frigate": "护卫舰",
        "destroyer": "驱逐舰",
        "cruiser": "巡洋舰",
        "battlecruiser": "战列巡洋舰",
        "battleship": "战列舰",
        "carrier": "航空母舰",
        "support": "支援舰",
        "corvette": "护航艇",
        "fighter": "战机",
        "bomber": "轰炸机",
        "attacker": "攻击机",
        "torpedo": "鱼雷艇/鱼雷机",
        "scout": "侦察舰/机",
        "ship": "通用舰船",
        "umv": "工程/通用单位",
    }
    return mapping.get(token, token)


def _summarize_script(entries: list[NpkEntry]) -> dict[str, Any]:
    return {
        "count": len(entries),
        "stored_entries": sum(1 for entry in entries if entry.is_stored),
        "nxz_blocks": len(entries),
        "sample_file_ids": [entry.file_id for entry in entries[:20]],
        "sample_sizes": [entry.packed_size for entry in entries[:20]],
    }


def _match_deck_candidates(ships: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for deck_id, info in KNOWN_DECK_SHIPS.items():
        scored = []
        for ship in ships:
            text = " ".join(
                [
                    ship["ship_id"],
                    *ship.get("blueprint_images", []),
                    *ship.get("weaponinfo_paths", []),
                    *ship.get("model_paths", []),
                    *ship.get("paint_config_paths", []),
                ]
            ).lower()
            score = 0
            for hint in info["hints"]:
                if hint.lower() in text:
                    score += 10
            if any(hint.lower() in ship["ship_id"] for hint in info["hints"]):
                score += 8
            if score:
                scored.append(
                    {
                        "ship_id": ship["ship_id"],
                        "score": score,
                        "resource_count": ship["resource_count"],
                        "likely_class": ship["likely_class"],
                        "sample_paths": (
                            ship.get("blueprint_images", [])[:2]
                            + ship.get("weaponinfo_paths", [])[:2]
                            + ship.get("model_paths", [])[:2]
                        ),
                    }
                )
        scored.sort(key=lambda item: (-item["score"], -item["resource_count"], item["ship_id"]))
        result[deck_id] = {"name": info["name"], "candidates": scored[:12]}
    return result
