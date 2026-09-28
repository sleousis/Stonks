"""Shared HTML fragments for the static report and the tear sheet.

The escaping rule: every value that came from data goes through :func:`e`
(``html.escape`` with quotes) where it is placed into markup; builders only
concatenate already-escaped fragments.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from html import escape
from typing import Any


def e(value: Any) -> str:
    return escape(str(value), quote=True)


def pct(x: float | None) -> str:
    return "-" if x is None else f"{x:+.2%}"


def money(x: float | None) -> str:
    return "-" if x is None else f"{x:,.2f}"


def num(x: Any) -> str:
    if isinstance(x, float):
        return f"{x:,.4g}"
    return str(x)


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], empty: str) -> str:
    """``rows`` cells must already be escaped HTML fragments."""
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    if not body:
        return f'<p class="muted">{e(empty)}</p>'
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def badge(ok: bool, yes: str = "PASS", no: str = "FAIL") -> str:
    return f'<span class="badge {"ok" if ok else "bad"}">{e(yes if ok else no)}</span>'


def tile(label: str, value: str) -> str:
    return f'<div class="tile"><div class="label">{e(label)}</div><div class="value">{e(value)}</div></div>'


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--fg:#1d1f23;--muted:#61656d;--border:#dfe1e5;
--s1:#2563eb;--s2:#d97706;--neg:#dc2626;--ok:#15803d;--bad:#b91c1c;--grid:#e8e9ec}
@media (prefers-color-scheme: dark){:root{--bg:#121417;--panel:#1b1e23;--fg:#e6e7ea;
--muted:#9aa0aa;--border:#2c3038;--s1:#60a5fa;--s2:#fbbf24;--neg:#f87171;--ok:#4ade80;
--bad:#f87171;--grid:#2a2e35}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header,main{max-width:1000px;margin:0 auto;padding:16px}
h1{margin:8px 0 0;font-size:24px}h2{font-size:18px;margin:0 0 8px}
h2.group{margin:24px 0 8px}h3{font-size:14px;margin:16px 0 6px;color:var(--muted)}
section{background:var(--panel);border:1px solid var(--border);border-radius:8px;
padding:16px;margin:12px 0}
.muted{color:var(--muted)}.warn{color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px}
.tile{border:1px solid var(--border);border-radius:6px;padding:8px}
.tile .label{color:var(--muted);font-size:12px}.tile .value{font-size:18px;
font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--border);
vertical-align:top;overflow-wrap:anywhere}
th{color:var(--muted);font-weight:600}
.badge{display:inline-block;padding:0 6px;border-radius:4px;font-size:12px;font-weight:600;
border:1px solid currentColor}
.badge.ok{color:var(--ok)}.badge.bad{color:var(--bad)}.badge.status-active{color:var(--ok)}
.badge.status-shadow{color:var(--s2)}.badge.status-retired{color:var(--muted)}
svg.chart{width:100%;height:220px;display:block}
.chart .grid{stroke:var(--grid);stroke-width:1}
.chart .axis{fill:var(--muted);font-size:11px}
.chart .line{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}
.chart .line.s1{stroke:var(--s1)}.chart .line.s2{stroke:var(--s2);stroke-dasharray:6 4}
.chart .line.neg{stroke:var(--neg)}.chart .area.neg{fill:var(--neg);opacity:.15;stroke:none}
.chart .dot.s1{fill:var(--s1)}.chart .dot.s2{fill:var(--s2)}.chart .dot.neg{fill:var(--neg)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--muted);margin-top:4px}
.legend .key::before{content:"";display:inline-block;width:12px;height:3px;margin-right:6px;
vertical-align:middle;background:var(--s1)}
.legend .key.s2::before{background:var(--s2)}
.chart .area.s2{fill:var(--s2);opacity:.12;stroke:none}
.legend .key.neg::before{background:var(--neg)}
.up{color:var(--ok)}.down{color:var(--bad)}
table.grid td,table.grid th{text-align:right;white-space:nowrap}
table.grid td:first-child,table.grid th:first-child{text-align:left}
.nodata{color:var(--muted);padding:24px;text-align:center;border:1px dashed var(--border);
border-radius:6px}
@page{size:A4;margin:14mm 12mm}
@media print{:root{--bg:#fff;--panel:#fff;--fg:#000;--muted:#444;--border:#bbb;--grid:#ddd;
--s1:#1d4ed8;--s2:#b45309;--neg:#b91c1c;--ok:#166534;--bad:#991b1b}
body{font-size:11px;-webkit-print-color-adjust:exact;print-color-adjust:exact}
header,main{max-width:none;padding:0}
section{border:0;border-top:1px solid var(--border);border-radius:0;padding:8px 0;margin:8px 0;
break-inside:avoid}
h2,h3{break-after:avoid}tr,svg.chart,.tile{break-inside:avoid}
.scroll{overflow:visible}svg.chart{height:180px}}
"""
