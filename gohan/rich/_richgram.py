"""Compatibility shim over `richgram <https://github.com/Badmunda05/richgram>`_.

``richgram`` (by `BadmundaXd <https://t.me/BadmundaXd>`_, MIT) provides the
HTML builders and the Kurigram/Pyrogram senders for Bot API 10.3 rich messages.
It is the preferred backend and is used whenever it is importable.

If it is *not* installed (aiogram-only deployment, `MTPROTO_MODE=off`), this
module falls back to small local implementations of the same builders so that
:mod:`gohan.rich.ui` and every handler keep working unchanged.
"""

from __future__ import annotations

import html as _html
from typing import Any

try:  # pragma: no cover - depends on the environment
    import richgram as _rg
except Exception:  # pragma: no cover
    _rg = None

#: True when the real richgram package is in use.
HAS_RICHGRAM: bool = _rg is not None

__all__ = [
    "HAS_RICHGRAM",
    "rich_anchor",
    "rich_button",
    "rich_button_row",
    "rich_code",
    "rich_details",
    "rich_esc",
    "rich_footer",
    "rich_heading",
    "rich_hr",
    "rich_list",
    "rich_math",
    "rich_note",
    "rich_pre",
    "rich_pull_quote",
    "rich_table",
    "rich_to_plain",
]


if HAS_RICHGRAM:  # pragma: no cover - exercised whenever richgram is installed
    from richgram import (  # type: ignore[import-not-found]
        rich_anchor,
        rich_button,
        rich_button_row,
        rich_code,
        rich_details,
        rich_esc,
        rich_footer,
        rich_heading,
        rich_hr,
        rich_list,
        rich_math,
        rich_note,
        rich_pre,
        rich_pull_quote,
        rich_table,
        rich_to_plain,
    )

    RICH_AVAILABLE = bool(getattr(_rg, "RICH_AVAILABLE", True))
    VERSION = getattr(_rg, "__version__", "unknown")

else:  # pragma: no cover - minimal local fallbacks
    RICH_AVAILABLE = False
    VERSION = "builtin"

    def rich_esc(value: Any) -> str:  # type: ignore[misc]
        return _html.escape(str(value if value is not None else ""), quote=False)

    def rich_heading(text: str, level: int = 1) -> str:  # type: ignore[misc]
        level = max(1, min(6, int(level)))
        return f"<h{level}>{rich_esc(text)}</h{level}>"

    def rich_note(text: str, expandable: bool = False) -> str:  # type: ignore[misc]
        attr = " expandable" if expandable else ""
        return f"<blockquote{attr}>{text}</blockquote>"

    def rich_hr() -> str:  # type: ignore[misc]
        return "<hr/>"

    def rich_footer(text: str) -> str:  # type: ignore[misc]
        return f"<footer>{text}</footer>"

    def rich_pull_quote(text: str) -> str:  # type: ignore[misc]
        return f"<aside>{text}</aside>"

    def rich_code(value: Any) -> str:  # type: ignore[misc]
        return f"<code>{rich_esc(value)}</code>"

    def rich_pre(value: Any, language: str | None = None) -> str:  # type: ignore[misc]
        body = rich_esc(value)
        if language:
            return f'<pre><code class="language-{rich_esc(language)}">{body}</code></pre>'
        return f"<pre>{body}</pre>"

    def rich_list(items, ordered: bool = False) -> str:  # type: ignore[misc]
        tag = "ol" if ordered else "ul"
        body = "".join(f"<li>{item}</li>" for item in items)
        return f"<{tag}>{body}</{tag}>"

    def rich_math(expression: str) -> str:  # type: ignore[misc]
        return f"<tg-math-block>{rich_esc(expression)}</tg-math-block>"

    def rich_anchor(name: str) -> str:  # type: ignore[misc]
        return f'<a name="{rich_esc(name)}"></a>'

    def rich_details(summary: str, body: str, open: bool = False) -> str:  # type: ignore[misc]
        attr = " open" if open else ""
        return f"<details{attr}><summary>{summary}</summary>{body}</details>"

    def rich_button(  # type: ignore[misc]
        text: str,
        url: str | None = None,
        callback_data: str | None = None,
        *,
        style: str | None = None,
        data: str | None = None,
        **_: Any,
    ) -> str:
        style_attr = f' style="{style}"' if style else ""
        label = rich_esc(text)
        if callback_data or data:
            payload = rich_esc(callback_data or data or "")
            return f'<tg-button type="callback_data"{style_attr} data="{payload}">{label}</tg-button>'
        return f'<tg-button type="url"{style_attr} url="{rich_esc(url)}">{label}</tg-button>'

    def rich_button_row(*buttons: str, align: str | None = None) -> str:  # type: ignore[misc]
        attr = f' align="{align}"' if align else ""
        return f"<tg-button-row{attr}>{''.join(buttons)}</tg-button-row>"

    def rich_table(headers, rows, border: int = 1) -> str:  # type: ignore[misc]
        parts = [f'<table border="{int(bool(border))}">']
        if headers:
            parts.append("<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>")
        for row in rows:
            parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
        parts.append("</table>")
        return "".join(parts)

    def rich_to_plain(html_text: str) -> str:  # type: ignore[misc]
        import re

        text = re.sub(r"<[^>]+>", "", str(html_text or ""))
        return _html.unescape(text).strip()
