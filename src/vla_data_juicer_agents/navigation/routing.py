from __future__ import annotations

import re


_VAGUE_MESSAGES = {
    "继续",
    "继续吧",
    "下一步",
    "开始",
    "go on",
    "continue",
    "next",
}

_EXPLICIT_NAVIGATION_PATTERNS = [
    r"\bros\s*bag\b",
    r"\brosbag\b",
    r"\bdb3\b",
    r"\bodom(?:etry)?\b",
    r"\bgrid\s*map\b",
    r"\bgridmap\b",
    r"\bsync[_\s-]*data\b",
    r"\bfinish[_\s-]*data\b",
    r"\bgen[_\s-]*box(?:\.py)?\b",
    r"\btrajectory\b",
    r"\bnavigation\s+data\b",
    r"\bnav\s+data\b",
    r"(?:\bnav(?:igation)?\b|\bros\s*bag\b|\brosbag\b|\bdb3\b|\btrajectory\b).{0,40}"
    r"(?:\btracking\b|\bprojection\b|\bannotation\b|\bannotat(?:e|ion|ing)\b)",
    r"(?:\btracking\b|\bprojection\b|\bannotation\b|\bannotat(?:e|ion|ing)\b).{0,40}"
    r"(?:\bnav(?:igation)?\b|\bros\s*bag\b|\brosbag\b|\bdb3\b|\btrajectory\b)",
    r"导航数据",
    r"导航.*(?:处理|同步|标注|轨迹|投影|跟踪|生成|提取|解析)",
    r"(?:处理|同步|标注|轨迹|投影|跟踪|生成|提取|解析).*导航",
    r"里程计",
    r"栅格地图",
    r"轨迹.*(?:跟踪|投影|标注|生成|处理)",
    r"(?:跟踪|投影|标注|生成|处理).*轨迹",
    r"标注.*(?:导航|轨迹|数据)",
]

_DATE_WITH_NAVIGATION_CONTEXT = re.compile(
    r"(?:\b\d{4}[-_/]?\d{2}[-_/]?\d{2}\b|\b\d{8}\b).{0,40}"
    r"(?:nav|navigation|ros|bag|db3|sync|tracking|projection|导航|同步|跟踪|投影)"
    r"|(?:nav|navigation|ros|bag|db3|sync|tracking|projection|导航|同步|跟踪|投影).{0,40}"
    r"(?:\b\d{4}[-_/]?\d{2}[-_/]?\d{2}\b|\b\d{8}\b)",
    re.IGNORECASE,
)

_EXPLICIT_NAVIGATION_REGEXES = [
    re.compile(pattern, re.IGNORECASE) for pattern in _EXPLICIT_NAVIGATION_PATTERNS
]


def is_high_confidence_navigation_request(message: str) -> bool:
    text = message.strip()
    if not text:
        return False

    normalized = re.sub(r"\s+", " ", text.casefold())
    if normalized in _VAGUE_MESSAGES:
        return False

    return any(pattern.search(text) for pattern in _EXPLICIT_NAVIGATION_REGEXES) or bool(
        _DATE_WITH_NAVIGATION_CONTEXT.search(text)
    )
