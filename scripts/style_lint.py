#!/usr/bin/env python3
"""Style lint: fail on any spec 8.5 banned token in templates/ or static/.

The AI-coded look is grep-able, so grep for it. Exit 1 with a list of
hits; exit 0 clean. Run from anywhere:

    python scripts/style_lint.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("templates", "static")
TEXT_EXT = {".html", ".css", ".js", ".svg", ".txt", ".jinja", ".jinja2"}

# (name, compiled regex, applies_to: 'all'|'templates')
PATTERNS = [
    ("css gradient", re.compile(r"gradient\(", re.I), "all"),
    ("backdrop-filter", re.compile(r"backdrop-filter", re.I), "all"),
    # CSS blur effect; a leading dot is the DOM focus method, not a filter
    ("blur()", re.compile(r"(?<![.\w])blur\(", re.I), "all"),
    ("box shadow", re.compile(r"box-" + "shadow", re.I), "all"),
    ("@keyframes", re.compile(r"@keyframes", re.I), "all"),
    ("banned color word", re.compile(r"\b(purple|violet|indigo)\b", re.I), "all"),
    ("banned font", re.compile(r"\b(Inter|Roboto|Poppins|Montserrat)\b"), "all"),
    ("banned font", re.compile(r"Space\s+Grotesk", re.I), "all"),
    ("loader/effect word", re.compile(r"\b(shimmer|skeleton|confetti)\b", re.I), "all"),
    ("marketing badge", re.compile(r"powered\s+by\s+ai", re.I), "all"),
    ("banned copy word", re.compile(r"\b(seamless|supercharge|unleash)\b", re.I), "all"),
]

ANIMATION = re.compile(r"\banimation\s*:\s*([^;}\"']*)", re.I)
BORDER_RADIUS = re.compile(r"border-radius\s*:\s*([^;}\"']*)", re.I)
RADIUS_VALUE = re.compile(r"(\d+(?:\.\d+)?)\s*(px|rem|em|%)?")

# Emoji / pictograph codepoint ranges (templates must contain none).
EMOJI_RANGES = [
    (0x1F000, 0x1FAFF), (0x1FB00, 0x1FBFF),
    (0x2600, 0x27BF), (0x2B00, 0x2BFF),
    (0xFE00, 0xFE0F), (0x2190, 0x21FF),  # variation selectors, arrows
    (0x1F1E6, 0x1F1FF),
]


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in EMOJI_RANGES)


def _strip_non_copy(text: str) -> str:
    """Remove the parts of a template that are not rendered prose, keeping
    line structure so hit line numbers stay right."""
    def blank(m):
        return re.sub(r"[^\n]", " ", m.group(0))
    text = re.sub(r"<script\b.*?</script>", blank, text, flags=re.S | re.I)
    text = re.sub(r"<style\b.*?</style>", blank, text, flags=re.S | re.I)
    text = re.sub(r"{%.*?%}", blank, text, flags=re.S)
    text = re.sub(r"{{.*?}}", blank, text, flags=re.S)
    text = re.sub(r"<\?xml.*?\?>", blank, text, flags=re.S)
    text = re.sub(r"<[^>]*?-->", blank, text, flags=re.S)   # comment tails
    text = re.sub(r"<!--.*?-->", blank, text, flags=re.S)
    text = text.replace("!=", "  ")
    text = re.sub(r"<!doctype", "         ", text, flags=re.I)
    text = re.sub(r"!important", " " * 10, text, flags=re.I)
    return text


def lint_file(path: Path, hits: list) -> None:
    rel = path.relative_to(ROOT)
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    lines = text.splitlines()
    is_template = rel.parts[0] == "templates"

    def line_no(pos: int) -> int:
        return text.count("\n", 0, pos) + 1

    for name, rx, scope in PATTERNS:
        if scope == "templates" and not is_template:
            continue
        for m in rx.finditer(text):
            hits.append("{0}:{1}: {2}: {3}".format(
                rel, line_no(m.start()), name, m.group(0).strip()))

    for m in ANIMATION.finditer(text):
        value = m.group(1).strip()
        if value and not value.startswith("none"):
            hits.append("{0}:{1}: animation: {2}".format(rel, line_no(m.start()), value))

    for m in BORDER_RADIUS.finditer(text):
        for vm in RADIUS_VALUE.finditer(m.group(1)):
            n, unit = float(vm.group(1)), (vm.group(2) or "px")
            px = n * 15.0 if unit in ("rem", "em") else n
            if unit != "%" and px > 4:
                hits.append("{0}:{1}: border-radius over 4px: {2}".format(
                    rel, line_no(m.start()), m.group(1).strip()))
            if unit == "%" and n > 0:
                hits.append("{0}:{1}: percent border-radius: {2}".format(
                    rel, line_no(m.start()), m.group(1).strip()))

    if is_template:
        for i, line in enumerate(text.splitlines(), 1):
            for ch in line:
                if _is_emoji(ch):
                    hits.append("{0}:{1}: emoji in template: U+{2:04X}".format(rel, i, ord(ch)))
        copy = _strip_non_copy(text)
        for i, line in enumerate(copy.splitlines(), 1):
            if "!" in line:
                orig = lines[i - 1].strip() if i - 1 < len(lines) else ""
                hits.append("{0}:{1}: exclamation mark in template copy: {2}".format(
                    rel, i, orig[:80]))


def main() -> int:
    hits = []
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in TEXT_EXT:
                lint_file(path, hits)
    if hits:
        print("style lint: {0} hit(s)".format(len(hits)))
        for h in hits:
            print("  " + h)
        return 1
    print("style lint: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
