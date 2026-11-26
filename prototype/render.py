#!/usr/bin/env python3
"""Prototype renderer: report JSON + narrative markdown -> one Hazel St Forecast page.

Layout follows docs/product/report-page.md: report, then forecast, then accumulation.
Stdlib only. Usage: python3 prototype/render.py prototype/out/2026-11-25.json
"""
import html
import json
import re
import sys
from pathlib import Path

SHADES = ["#0f3a5a", "#1a5c88", "#2f86b4", "#7fb7d6", "#12806a"]
TYPE_STYLE = {  # precip type at summit -> (label, background, text)
    "snow": ("Snow", "#1a5c88", "#fff"), "ice": ("Ice", "#7c3aed", "#fff"),
    "rain": ("Rain", "#12806a", "#fff"), "mix": ("Mix", "#c4b5fd", "#132534"),
    "dry": ("—", "#eef4f8", "#5b7183"),
}


def precip_type(day):
    p, s, tags = day["precip_in"], day["snow_in"], day["tags"]
    if p < 0.05 and s < 0.2:
        return "dry"
    if s >= 5 * p:            # most of the liquid fell as snow (~10:1 ratio, half or more)
        return "snow"
    if "rain_summit" in tags:
        return "rain"
    if "freezing_rain" in tags:
        return "ice"
    return "mix"


def md_to_html(md):
    out = []
    for block in re.split(r"\n\s*\n", md.strip()):
        b = html.escape(" ".join(block.split()))
        b = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", b)
        if b.startswith("## "):
            out.append(f"<h2>{b[3:]}</h2>")
        else:
            out.append(f"<p>{b}</p>")
    return "\n".join(out)


def front_matter(text):
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    meta, body = m.group(1), m.group(2)
    fm = {}
    for line in meta.splitlines():
        k, _, v = line.partition(":")
        fm[k.strip()] = v.strip().strip('"')
    bb = dict(re.findall(r'(\w+): "([^"]*)"', fm.get("best_bet", "")))
    return fm, bb, body


def forecast_chart(ms, days):
    W, H, pl, pb, pt = 340, 210, 26, 66, 12
    rows = sorted(ms, key=lambda m: -m["forecast"]["windows"]["5d"]["snow_in"])
    bw = (W - pl) / len(rows)
    mx = max(6, max(sum(d["snow_in"] for d in m["forecast"]["days"]) for m in rows))
    sc = (H - pb - pt) / mx
    s = []
    for v in range(0, int(mx) + 1, 2 if mx <= 8 else 5):
        y = H - pb - v * sc
        s.append(f'<line x1="{pl}" x2="{W}" y1="{y:.1f}" y2="{y:.1f}" stroke="#eef4f8"/>'
                 f'<text x="{pl-4}" y="{y+3:.1f}" font-size="9" text-anchor="end" fill="#5b7183">{v}″</text>')
    for i, m in enumerate(rows):
        x, y = pl + i * bw + bw * .18, H - pb
        for d, day in enumerate(m["forecast"]["days"]):
            h = day["snow_in"] * sc
            y -= h
            s.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw*.64:.1f}" height="{h:.1f}" fill="{SHADES[d]}"/>')
        tot = m["forecast"]["windows"]["5d"]["snow_in"]
        s.append(f'<text x="{x+bw*.32:.1f}" y="{y-3:.1f}" font-size="9" text-anchor="middle" font-weight="600" fill="#0f3a5a">{tot:g}</text>')
        s.append(f'<text transform="translate({x+bw*.32+3:.1f},{H-pb+8}) rotate(-40)" font-size="9" text-anchor="end" fill="#132534">{html.escape(m["name"])}</text>')
    for j, d in enumerate(days):
        s.append(f'<rect x="{pl+j*62}" y="{H-12}" width="8" height="8" fill="{SHADES[j]}"/>'
                 f'<text x="{pl+j*62+11}" y="{H-5}" font-size="9" fill="#5b7183">{d}</text>')
    return f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Forecast new snow by mountain">{"".join(s)}</svg>'


def type_grid(ms, days):
    head = "".join(f"<th>{d.replace(' ', '<br>')}</th>" for d in days)
    rows = []
    for m in ms:
        cells = []
        for day in m["forecast"]["days"]:
            t = day.get("precip_type") or precip_type(day)
            label, bg, fg = TYPE_STYLE[t]
            amt = "" if t == "dry" else f'<small>{day["precip_in"]:.2f}″</small>'
            wind = ' <i title="gusts">💨</i>' if any(x.startswith("wind_hold") for x in day["tags"]) else ""
            cells.append(f'<td style="background:{bg};color:{fg}">{label}{wind}{amt}</td>')
        rows.append(f'<tr><th>{html.escape(m["name"])}</th>{"".join(cells)}</tr>')
    return f'<table class="types"><thead><tr><th></th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


def window_table(ms):
    rows = []
    for m in sorted(ms, key=lambda m: -m["forecast"]["windows"]["5d"]["snow_in"]):
        tds = []
        for k in ("1d", "3d", "5d"):
            w = m["forecast"]["windows"][k]
            lo, hi = w["range"]
            rng = f'<small>{lo:g}–{hi:g}</small>' if hi > lo else ""
            nws = f'<small>NWS {w["nws_in"]:g}</small>' if w["nws_in"] is not None else ""
            tds.append(f'<td><b>{w["snow_in"]:g}″</b>{rng}{nws}</td>')
        rows.append(f'<tr><td>{html.escape(m["name"]).replace(" ", "&nbsp;")}</td>{"".join(tds)}</tr>')
    return ('<table id="fct"><thead><tr><th>Mountain</th><th>Next<br>24 h</th><th>Next<br>3 days</th>'
            f'<th>Next<br>5 days</th></tr></thead><tbody>{"".join(rows)}</tbody></table>')


def accumulation(ms, past_days):
    totals = [m["accumulation"]["last7_total_in"] for m in ms]
    if max(totals) < 0.5:
        return ('<div class="empty">No measurable natural snow at any mountain in the last 7 days '
                f'({past_days[0]} – {past_days[-1]}, modeled). Base depths and open terrain come from '
                'resort reports, which are not connected in this sample.</div>')
    W, pl, pr, rh = 340, 84, 60, 26
    mx = max(totals)
    sc = (W - pl - pr) / mx
    s = []
    for i, m in enumerate(sorted(ms, key=lambda m: -m["accumulation"]["last7_total_in"])):
        y, t = 6 + i * rh, m["accumulation"]["last7_total_in"]
        s.append(f'<text x="{pl-8}" y="{y+14}" font-size="11" text-anchor="end" fill="#132534">{html.escape(m["name"])}</text>'
                 f'<rect x="{pl}" y="{y+4}" width="{t*sc:.1f}" height="14" rx="2" fill="#1a5c88"/>'
                 f'<text x="{pl+t*sc+4:.1f}" y="{y+15}" font-size="10" font-weight="600" fill="#0f3a5a">{t:g}″</text>')
    return f'<figure><svg viewBox="0 0 {W} {6+len(ms)*rh+8}" width="100%">{"".join(s)}</svg></figure>'


CSS = (Path(__file__).resolve().parent / "page.css").read_text()


def render(json_path):
    r = json.loads(Path(json_path).read_text())
    fm, bb, body = front_matter(Path(json_path).with_suffix(".md").read_text())
    ms = r["mountains"]
    offices = ", ".join(d["office"] for d in r["discussions"])
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Hazel St Forecast · {r['date']}</title>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="band"><div class="k">Hazel St Forecast</div><h1>{html.escape(fm['headline'])}</h1>
<div class="when">{r['issued']} · sample run</div></div>
<main>
<div class="call"><small>Best bet</small><b>{html.escape(bb['mountain'])}, {html.escape(bb['day'])}</b><span>{html.escape(bb['why'])}</span></div>
{md_to_html(body)}
<div class="part">Forecast · what falls next</div>
<h2>Next 5 days</h2>
<p class="lead">From 6 AM {r['days'][0]}, at summit elevation. Average of GFS, ECMWF, and GEM; range is the spread between them.</p>
<figure>{forecast_chart(ms, r['days'])}<figcaption>Forecast new snow, stacked by day.</figcaption></figure>
<figure>{type_grid(ms, r['days'])}<figcaption>Precipitation type at the summit, liquid inches. 💨 = lift-hold wind.</figcaption></figure>
{window_table(ms)}
<div class="part">Accumulation · snow that has fallen</div>
<h2>Last 7 days</h2>
{accumulation(ms, r['past_days'])}
<div class="foot">Forecast: Open-Meteo (GFS, ECMWF, GEM) at summit and base elevation; NWS gridpoints and alerts; NWS forecast discussions ({offices}). Accumulation: Open-Meteo past 7 days (modeled). Verification: none yet — first run.</div>
</main></body></html>"""
    out = Path(json_path).with_suffix(".html")
    out.write_text(page)
    print(out)


if __name__ == "__main__":
    render(sys.argv[1])
