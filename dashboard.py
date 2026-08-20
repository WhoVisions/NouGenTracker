#!/usr/bin/env python3
"""Render a fleet usage summary as one self-contained HTML page.

A terminal table is a poor instrument for "which box is burning what". This
turns `relay`'s fleet summary into a page: a hero cost number, per-machine
magnitude, a daily trend, and — given equal billing — the doubts that belong
next to the numbers rather than in a footnote (measurement confidence, stale
peers, machine-days that look like they counted the same calls).

No external assets, no CDN, no fonts, no JS libraries. The page is one file
that renders offline and can be opened, mailed, or published as-is.

Colour follows the reference categorical palette, slots 1-3 — the three that
validate on the all-pairs list in both light and dark modes. A fourth machine
folds into "other" rather than minting a hue that has not been checked.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

# Categorical slots 1-3 (light, dark). Fixed order, assigned by machine name,
# never cycled and never re-assigned when the machine list changes — colour
# follows the entity, not its rank.
SERIES: Tuple[Tuple[str, str], ...] = (
    ("#2a78d6", "#3987e5"),   # blue
    ("#eb6834", "#d95926"),   # orange
    ("#1baf7a", "#199e70"),   # aqua
)
OTHER = ("#6b6a63", "#8a8981")

CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f2f2ef;
--line:#dcdbd4;--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#78776f;
--good:#1a7f4b;--warn:#b06b00;--crit:#c0392b;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s0:#6b6a63}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{
color-scheme:dark;--surface-1:#1a1a19;--surface-2:#232322;--line:#3a3a37;
--text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#918f85;
--good:#4cba7d;--warn:#d99b2b;--crit:#e66767;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s0:#8a8981}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;
--surface-2:#232322;--line:#3a3a37;--text-primary:#fff;--text-secondary:#c3c2b7;
--text-muted:#918f85;--good:#4cba7d;--warn:#d99b2b;--crit:#e66767;
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s0:#8a8981}

.viz-root{background:var(--surface-1);color:var(--text-primary);
font:15px/1.5 ui-sans-serif,-apple-system,"Segoe UI",system-ui,sans-serif;
padding:32px 24px 56px;max-width:960px;margin:0 auto}
.viz-root h1{font-size:15px;font-weight:600;letter-spacing:.08em;
text-transform:uppercase;color:var(--text-secondary);margin:0 0 4px}
.sub{color:var(--text-muted);font-size:13px;margin:0 0 28px}
.hero{font-size:clamp(44px,9vw,76px);font-weight:650;letter-spacing:-.03em;
line-height:1;margin:0}
.hero-note{color:var(--text-secondary);font-size:14px;margin:8px 0 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;
overflow:hidden;margin:28px 0}
.tile{background:var(--surface-1);padding:14px 16px}
.tile .k{font-size:11px;letter-spacing:.07em;text-transform:uppercase;
color:var(--text-muted)}
.tile .v{font-size:24px;font-weight:600;letter-spacing:-.01em;margin-top:3px}
.tile .n{font-size:12px;color:var(--text-muted);margin-top:2px}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;
color:var(--text-muted);margin:34px 0 12px;font-weight:600}
.bars{display:flex;flex-direction:column;gap:10px}
.bar-row{display:grid;grid-template-columns:110px 1fr auto;gap:12px;
align-items:center}
.bar-name{font-size:13px;color:var(--text-secondary);white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.bar-track{background:var(--surface-2);border-radius:4px;height:16px;
position:relative;overflow:hidden}
.bar-fill{height:100%;border-radius:0 4px 4px 0;min-width:2px}
.bar-val{font-variant-numeric:tabular-nums;font-size:13px;
color:var(--text-primary);white-space:nowrap}
svg{display:block;max-width:100%;overflow:visible}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin:10px 0 0;font-size:13px;
color:var(--text-secondary)}
.swatch{width:10px;height:10px;border-radius:2px;display:inline-block;
margin-right:6px;vertical-align:-1px}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:520px}
th{text-align:left;font-weight:600;color:var(--text-muted);font-size:11px;
letter-spacing:.06em;text-transform:uppercase;padding:10px 14px;
border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:10px 14px;border-bottom:1px solid var(--line);
color:var(--text-secondary);white-space:nowrap}
tr:last-child td{border-bottom:none}
td.num{font-variant-numeric:tabular-nums;color:var(--text-primary)}
.flag{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;
border-radius:8px;margin:10px 0;font-size:14px;line-height:1.45}
.flag.ok{background:color-mix(in srgb,var(--good) 12%,transparent);
color:var(--text-primary)}
.flag.warn{background:color-mix(in srgb,var(--warn) 15%,transparent);
color:var(--text-primary)}
.flag.crit{background:color-mix(in srgb,var(--crit) 15%,transparent);
color:var(--text-primary)}
.flag b{font-weight:650}
.icon{font-size:15px;line-height:1.35}
footer{color:var(--text-muted);font-size:12px;margin-top:38px;
border-top:1px solid var(--line);padding-top:14px}
"""


def _fmt_usd(value: float) -> str:
    if value >= 100:
        return f"${value:,.0f}"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.3f}"


def _fmt_tokens(value: int) -> str:
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= limit:
            return f"{value / limit:.1f}{suffix}"
    return str(value)


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _colors(machines: Sequence[str]) -> Dict[str, Tuple[str, str]]:
    """Fixed assignment by sorted machine name: adding a box never repaints
    the others."""
    out: Dict[str, Tuple[str, str]] = {}
    for index, machine in enumerate(sorted(machines)):
        out[machine] = SERIES[index] if index < len(SERIES) else OTHER
    return out


def _var(machine: str, order: Sequence[str]) -> str:
    index = sorted(order).index(machine)
    return f"var(--s{index + 1})" if index < len(SERIES) else "var(--s0)"


def _cost_bars(summary: Any) -> str:
    """Magnitude by identity — a plain ranked bar, the right form for 1-4 rows."""
    if not summary.machines:
        return '<p class="sub">No machine has published yet.</p>'
    order = list(summary.machines)
    ranked = sorted(summary.machines.items(), key=lambda kv: -kv[1]["cost"])
    peak = max(data["cost"] for _, data in ranked) or 1.0
    rows = []
    for machine, data in ranked:
        width = max(data["cost"] / peak * 100, 0.6)
        label = f"{machine} (this box)" if machine == summary.local else machine
        rows.append(
            f'<div class="bar-row"><span class="bar-name" title="{_esc(label)}">'
            f'{_esc(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:'
            f'{width:.1f}%;background:{_var(machine, order)}" '
            f'title="{_esc(machine)}: {_fmt_usd(data["cost"])} across '
            f'{data["calls"]:,} calls"></span></span>'
            f'<span class="bar-val">{_fmt_usd(data["cost"])}</span></div>')
    return '<div class="bars">' + "".join(rows) + "</div>"


def _daily_chart(summary: Any) -> str:
    """Change over time: one stacked column per day, one segment per machine.

    Stacked rather than grouped because the question is "what did the FLEET
    spend that day", with attribution second. A 2px surface gap separates
    segments so adjacent machines never blur into one block.
    """
    days = summary.days
    if len(days) < 2:
        return ""
    order = list(summary.machines)
    totals = {
        day: sum(data["days"].get(day, 0.0) for data in summary.machines.values())
        for day in days
    }
    peak = max(totals.values()) or 1.0
    width, height = 640, 190
    pad_l, pad_b = 46, 26
    plot_w = width - pad_l - 8
    plot_h = height - pad_b - 12
    slot = plot_w / len(days)
    bar_w = min(max(slot * 0.62, 2.0), 46)
    # A label every column collides once the columns get thin: at 40 days each
    # slot is ~15px and "06-20" needs ~30. Thin to at most ~8 labels, always
    # keeping the first and last so the span stays readable.
    label_every = max(1, -(-len(days) // 8))

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Fleet cost per day">']
    # Recessive gridlines + axis labels at 0 / mid / peak.
    for frac in (0.0, 0.5, 1.0):
        y = 12 + plot_h - frac * plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - 8}" y2="{y:.1f}" '
            f'stroke="var(--line)" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="10" fill="var(--text-muted)">'
            f'{_fmt_usd(peak * frac)}</text>')
    for index, day in enumerate(days):
        x = pad_l + index * slot + (slot - bar_w) / 2
        cursor = 12 + plot_h
        # The 2px spacer separates STACKED segments. With a single machine
        # contributing there is no stack, and subtracting it anyway would
        # shorten every bar — a spacer must never distort a magnitude.
        stacked = sum(1 for m in summary.machines
                      if summary.machines[m]["days"].get(day, 0.0) > 0) > 1
        for machine in sorted(summary.machines):
            cost = summary.machines[machine]["days"].get(day, 0.0)
            if cost <= 0:
                continue
            seg = cost / peak * plot_h
            cursor -= seg
            parts.append(
                f'<rect x="{x:.1f}" y="{cursor:.1f}" width="{bar_w:.1f}" '
                f'height="{max(seg - 2, 1) if stacked else max(seg, 1):.1f}" '
                f'rx="{min(3, bar_w / 3):.1f}" '
                f'fill="{_var(machine, order)}">'
                f'<title>{_esc(day)} — {_esc(machine)}: {_fmt_usd(cost)}</title>'
                f'</rect>')
        if index % label_every == 0 or index == len(days) - 1:
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 8}" '
                f'text-anchor="middle" font-size="10" fill="var(--text-muted)">'
                f'{_esc(day[5:])}</text>')
    parts.append("</svg>")

    legend = "".join(
        f'<span><i class="swatch" style="background:{_var(m, order)}"></i>'
        f'{_esc(m)}</span>' for m in sorted(summary.machines))
    return ("".join(parts) + f'<div class="legend">{legend}</div>')


def _flags(summary: Any, threshold: float) -> str:
    """The doubts, given the same billing as the numbers."""
    out: List[str] = []
    if threshold and summary.total_cost >= threshold:
        out.append(
            f'<div class="flag warn"><span class="icon">▲</span><span>'
            f'<b>{_fmt_usd(summary.total_cost)}</b> is past the '
            f'{_fmt_usd(threshold)} signal line. Reported as a fact, not a '
            f'verdict — the question this fleet asks of spend is what it '
            f'bought, not whether it was large.</span></div>')
    for overlap in summary.overlaps:
        span = (overlap.day_a if overlap.day_a == overlap.day_b
                else f"{overlap.day_a}/{overlap.day_b}")
        out.append(
            f'<div class="flag crit"><span class="icon">■</span><span>'
            f'<b>Double counted.</b> {_esc(overlap.machine_a)} and '
            f'{_esc(overlap.machine_b)} report the same calls on '
            f'{_esc(span)} (Jaccard {overlap.similarity:.3f}). Totals above '
            f'are inflated by the shared portion and are NOT corrected — '
            f'guessing which copy to drop would be the worse error.'
            f'</span></div>')
    stale = [m for m, _ts, _age, is_stale in summary.freshness if is_stale]
    if stale:
        out.append(
            f'<div class="flag warn"><span class="icon">▲</span><span>'
            f'<b>{_esc(", ".join(stale))}</b> stopped publishing. Those '
            f'machines are missing from every number on this page — a quiet '
            f'peer looks exactly like a cheap one.</span></div>')
    if summary.confidence is not None and summary.confidence < 0.999:
        out.append(
            f'<div class="flag warn"><span class="icon">▲</span><span>'
            f'Only <b>{summary.confidence:.1%}</b> of billable tokens were '
            f'measured; the rest are inferred from text length'
            + (f' by {_esc(", ".join(summary.inferred))}' if summary.inferred
               else '') + '.</span></div>')
    if not out:
        out.append(
            '<div class="flag ok"><span class="icon">●</span><span>'
            'No overlap, no stale peer, nothing inferred. Every number on '
            'this page came from a provider that counted it.</span></div>')
    return "".join(out)


def _table(summary: Any) -> str:
    fresh = {m: (ts, age, stale) for m, ts, age, stale in summary.freshness}
    rows = []
    for machine, data in sorted(summary.machines.items(),
                                key=lambda kv: -kv[1]["cost"]):
        ts, age, stale = fresh.get(machine, (None, 0.0, False))
        if machine == summary.local:
            state, when = "this box", "live"
        elif ts is None:
            state, when = "unknown", "never"
        else:
            state = "STALE" if stale else "ok"
            when = f"{age:.1f}h ago"
        top = max(data["models"].items(), key=lambda kv: kv[1])[0] \
            if data["models"] else "-"
        rows.append(
            f"<tr><td>{_esc(machine)}</td><td class='num'>"
            f"{_fmt_usd(data['cost'])}</td><td class='num'>"
            f"{_fmt_tokens(data['tokens'])}</td><td class='num'>"
            f"{data['calls']:,}</td><td>{_esc(top)}</td>"
            f"<td>{_esc(when)}</td><td>{_esc(state)}</td></tr>")
    return (
        '<div class="tblwrap"><table><thead><tr><th>machine</th><th>cost</th>'
        '<th>tokens</th><th>calls</th><th>top model</th><th>last rollup</th>'
        '<th>state</th></tr></thead><tbody>' + "".join(rows) +
        "</tbody></table></div>")


def render(summary: Any, threshold: float = 0.0,
           title: str = "Fleet usage") -> str:
    """The whole page, as one string."""
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    span = (f"{summary.days[0]} to {summary.days[-1]}" if summary.days
            else "no data yet")
    machines = len(summary.machines)
    busiest = summary.busiest_day
    conf = ("unknown" if summary.confidence is None
            else ("100%" if summary.confidence >= 1
                  else f"{summary.confidence:.1%}"))
    return f"""<style>{CSS}</style>
<div class="viz-root">
<h1>{_esc(title)}</h1>
<p class="sub">{_esc(span)} &middot; {machines} machine{'s' if machines != 1 else ''}
reporting &middot; generated {_esc(generated)}</p>

<p class="hero">{_fmt_usd(summary.total_cost)}</p>
<p class="hero-note">API-equivalent cost across the fleet, cache-reads priced as
cache-reads. {_fmt_tokens(summary.total_tokens)} tokens moved to earn it.</p>

<div class="tiles">
  <div class="tile"><div class="k">cache-read share</div>
    <div class="v">{summary.cache_share:.0%}</div>
    <div class="n">{_fmt_tokens(summary.cache_read)} re-read</div></div>
  <div class="tile"><div class="k">measured</div><div class="v">{conf}</div>
    <div class="n">of billable tokens</div></div>
  <div class="tile"><div class="k">busiest day</div>
    <div class="v">{_esc(busiest[0][5:]) if busiest else '-'}</div>
    <div class="n">{_fmt_usd(busiest[1]) if busiest else 'no data'}</div></div>
  <div class="tile"><div class="k">machines</div><div class="v">{machines}</div>
    <div class="n">{_esc(summary.local)} is local</div></div>
</div>

{_flags(summary, threshold)}

<h2>Cost by machine</h2>
{_cost_bars(summary)}

{'<h2>Cost per day</h2>' + _daily_chart(summary) if len(summary.days) > 1 else ''}

<h2>Every machine</h2>
{_table(summary)}

<footer>Generated by NouGenTracker relay. Rollups are aggregate-only: day,
source, model and five token buckets — no session ids, no paths, no usernames.
Cost is an API-equivalent shadow bill, not an invoice.</footer>
</div>
"""


def write(summary: Any, path: Path, threshold: float = 0.0,
          title: str = "Fleet usage") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(summary, threshold, title), encoding="utf-8")
    return path
