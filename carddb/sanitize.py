"""Markup HTML sanitizer. Spec §3.3.

Card markup HTML may only contain: h1 h2 h3 h4 p u strong em mark span br,
and the only attribute that survives is class (used for <span class="min">).
Everything else is stripped but its text content kept.
"""
from __future__ import annotations

import html
from html.parser import HTMLParser

ALLOWED = {"h1", "h2", "h3", "h4", "p", "u", "strong", "em", "mark", "span", "br"}
VOID = {"br"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open_stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            return
        if tag in VOID:
            self.out.append(f"<{tag}>")
            return
        cls = next((v for k, v in attrs if k == "class" and v), None)
        if cls:
            safe_cls = html.escape(cls, quote=True)
            self.out.append(f'<{tag} class="{safe_cls}">')
        else:
            self.out.append(f"<{tag}>")
        self.open_stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in ALLOWED or tag in VOID:
            return
        if tag in self.open_stack:
            # close any unclosed inner tags first so output stays well-formed
            while self.open_stack:
                t = self.open_stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        self.out.append(html.escape(data))

    def result(self) -> str:
        while self.open_stack:
            self.out.append(f"</{self.open_stack.pop()}>")
        return "".join(self.out)


def sanitize_markup(raw_html: str) -> str:
    if not raw_html:
        return ""
    p = _Sanitizer()
    p.feed(raw_html)
    p.close()
    return p.result()
