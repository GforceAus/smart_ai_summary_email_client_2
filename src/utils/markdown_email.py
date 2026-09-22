"""
src/utils/markdown_email.py
---------------------------
Render the generator's Markdown output as HTML suitable for an email body.

The generator emits a small, known subset of Markdown (## / ### headings,
bullet lists, **bold**, plain paragraphs), so this converts that subset
directly rather than pulling in a Markdown dependency.

Styles are inlined because mail clients strip <style> blocks, and the layout
is kept to plain block elements — Outlook renders through Word and drops
flex/grid entirely.
"""
import html
import re

# ── Inline styles ──────────────────────────────────────────────────────────

FONT = ("-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif")

S_BODY    = f"margin:0;padding:0;background:#f4f4f5;font-family:{FONT};"
S_WRAP    = ("max-width:720px;margin:0 auto;padding:24px 28px;background:#ffffff;"
             "color:#1f2933;font-size:14px;line-height:1.55;")
S_H2      = ("margin:26px 0 10px;padding-bottom:6px;font-size:17px;font-weight:600;"
             "color:#10243e;border-bottom:2px solid #e4e7eb;")
S_H2_FIRST = S_H2.replace("margin:26px", "margin:0px")
S_H3      = ("margin:18px 0 8px;font-size:14px;font-weight:600;color:#334e68;"
             "text-transform:uppercase;letter-spacing:.04em;")
S_P       = "margin:0 0 12px;"
S_UL      = "margin:0 0 14px;padding-left:20px;"
S_LI      = "margin:0 0 6px;"


def _inline(text: str) -> str:
    """Escape HTML, then apply **bold** and `code`."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", out)
    return out


def markdown_to_html(md: str) -> str:
    """Convert the generator's Markdown subset to an HTML fragment."""
    lines = md.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    bullets: list[str] = []
    para: list[str] = []
    seen_heading = False

    def flush_bullets():
        if bullets:
            items = "".join(f'<li style="{S_LI}">{b}</li>' for b in bullets)
            parts.append(f'<ul style="{S_UL}">{items}</ul>')
            bullets.clear()

    def flush_para():
        if para:
            parts.append(f'<p style="{S_P}">{" ".join(para)}</p>')
            para.clear()

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            flush_bullets()
            flush_para()
            continue

        m3 = re.match(r"^###\s+(.*)$", stripped)
        m2 = re.match(r"^##\s+(.*)$", stripped)
        mb = re.match(r"^[\*\-]\s+(.*)$", stripped)

        if m3:
            flush_bullets(); flush_para()
            parts.append(f'<h3 style="{S_H3}">{_inline(m3.group(1))}</h3>')
        elif m2:
            flush_bullets(); flush_para()
            style = S_H2 if seen_heading else S_H2_FIRST
            seen_heading = True
            parts.append(f'<h2 style="{style}">{_inline(m2.group(1))}</h2>')
        elif mb:
            flush_para()
            bullets.append(_inline(mb.group(1)))
        else:
            flush_bullets()
            para.append(_inline(stripped))

    flush_bullets()
    flush_para()
    return "\n".join(parts)


def render_email_html(md: str, footer: str | None = None) -> str:
    """Wrap the converted Markdown in a full HTML document for sending."""
    body = markdown_to_html(md)
    foot = ""
    if footer:
        foot = (
            f'<p style="margin:22px 0 0;padding-top:14px;border-top:1px solid #e4e7eb;'
            f'color:#7b8794;font-size:12px;">{html.escape(footer, quote=False)}</p>'
        )
    return (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="{S_BODY}"><div style="{S_WRAP}">{body}{foot}</div></body></html>'
    )
