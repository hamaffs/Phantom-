"""HTML dossier exporter. The big editorial layout — Instrument Serif +
IBM Plex stack, photo-match card, subject hero, account grid, theme
toggle.
"""
from __future__ import annotations

import html
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from confidence import TIER_IMPOSTOR, TIER_LIKELY, TIER_VERIFIED
from dedupe import _flatten
from disambiguation import LABEL_LOW, LABEL_PRIMARY, LABEL_SECONDARY
from models import CheckResult, _photo_to_data_uri


def _format_count(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if abs(n) >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B".rstrip("0").rstrip(".")
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M".rstrip("0").rstrip(".")
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K".rstrip("0").rstrip(".")
    return str(n)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en" data-theme="{theme}">
<head>
<script>(function(){{try{{var t=localStorage.getItem('phantom-theme');if(t)document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}}})();</script>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phantom — {raw_html}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:        #f5efe2;
    --paper:     #f5efe2;
    --paper-2:   #ebe1cd;
    --ink:       #1a1612;
    --muted:     #6b5f4d;
    --rule:      #c4b896;
    --border:    #d4c9b3;
    --btn-bg:    #1a1612;
    --btn-text:  #f5efe2;
    --btn-hover: #2e2620;
    --err:       #8a3a2e;
    --tier-verified: #3d6b4a;
  }}
  [data-theme="dark"] {{
    --bg:        #16140f;
    --paper:     #2a2620;
    --paper-2:   #23201a;
    --ink:       #f0e8d8;
    --muted:     #9a8d75;
    --rule:      #3a342a;
    --tier-verified: #6aaa7c;
    --border:    #3a342a;
    --btn-bg:    #f0e8d8;
    --btn-text:  #16140f;
    --btn-hover: #d8ccb8;
    --err:       #d4826f;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ background: var(--bg); }}
  body {{
    margin: 0; color: var(--ink);
    font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    font-size: 14px; line-height: 1.55;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    min-height: 100vh;
  }}
  a {{ color: var(--ink); text-decoration: none; }}
  .serif {{ font-family: 'Instrument Serif', Georgia, 'Times New Roman', serif; }}
  .mono {{
    font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-feature-settings: "ss01", "ss02";
  }}
  .ghost path {{ fill: var(--ink); }}
  .ghost circle {{ fill: var(--bg); }}

  /* -------- Theme toggle -------- */
  .header-right {{
    display: flex; align-items: flex-start; gap: 12px;
  }}
  .theme-toggle {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 13px;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 5px 9px;
    border-radius: 4px;
    cursor: pointer;
    line-height: 1;
    flex-shrink: 0;
    margin-top: 1px;
  }}
  .theme-toggle:hover {{
    border-color: var(--ink);
    color: var(--ink);
    background: var(--paper-2);
  }}

  .page {{
    max-width: 900px; margin: 0 auto;
    padding: 42px 36px;
  }}

  /* -------- Header -------- */
  header.top {{
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 24px;
    padding-bottom: 22px;
    border-bottom: 1px solid var(--ink);
  }}
  .brand {{
    display: inline-flex; align-items: center; gap: 9px;
    line-height: 1;
  }}
  .brand .ghost {{
    width: 22px; height: 22px; flex-shrink: 0;
  }}
  .brand .wordmark {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 30px; color: var(--ink);
    letter-spacing: -0.01em; line-height: 1;
  }}
  .file-meta {{
    text-align: right;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; color: var(--muted);
    line-height: 1.6;
    letter-spacing: 0.02em;
  }}
  .file-meta .num {{ color: var(--ink); font-weight: 500; }}

  /* -------- Section kicker -------- */
  .kicker {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.22em;
    color: var(--muted);
    margin-bottom: 14px;
  }}

  /* -------- Subject of inquiry -------- */
  section.subject {{
    margin-top: 30px;
  }}
  .subject-row {{
    display: flex; align-items: center; gap: 26px;
  }}
  .portrait {{
    width: 130px; height: 130px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper-2) center/cover no-repeat;
    border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  .portrait .letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 66px; color: var(--ink);
    line-height: 1; letter-spacing: -0.02em;
  }}
  .ident {{ flex: 1; min-width: 0; }}
  .ident .handle {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 96px; line-height: 1.04;
    letter-spacing: -0.03em; color: var(--ink);
    word-break: break-word;
  }}
  .ident .name-region {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-style: italic;
    font-size: 24px; line-height: 1.35;
    color: var(--muted);
    margin-top: 14px;
  }}

  /* -------- Stats row -------- */
  .stats {{
    margin-top: 30px;
    border-top: 1px solid var(--ink);
    border-bottom: 1px solid var(--ink);
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    align-items: center;
  }}
  .stat {{
    padding: 24px 18px;
    position: relative;
    text-align: left;
  }}
  .stat + .stat::before {{
    content: ""; position: absolute; left: 0; top: 14%; bottom: 14%;
    border-left: 1px dashed var(--rule);
  }}
  .stat .n {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 56px; line-height: 1.05;
    letter-spacing: -0.02em; color: var(--ink);
  }}
  .stat .l {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: var(--muted);
    margin-top: 6px;
  }}

  /* -------- Photo-match + Subject details combo row -------- */
  section.combo {{
    margin-top: 30px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
    gap: 28px;
    align-items: start;
  }}
  /* When the photo-match column is absent, let subject-details span. */
  section.combo > :only-child {{ grid-column: 1 / -1; }}

  .photo-match {{
    background: var(--paper-2);
    border-radius: 8px;
    padding: 22px;
    border: 1px solid var(--border);
    display: flex; flex-direction: column;
    min-width: 0;
  }}
  .pm-photos {{
    display: flex; align-items: center; justify-content: center;
    gap: 16px;
    margin-top: 4px;
    margin-bottom: 18px;
  }}
  .pm-thumb {{
    width: 84px; height: 84px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper) center/cover no-repeat;
    border: 1px solid var(--border);
  }}
  .pm-arrow {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 16px;
    color: var(--muted);
    line-height: 1;
  }}
  .pm-divider {{
    border-top: 1px dashed var(--rule);
    padding-top: 12px;
  }}
  .pm-meta {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    color: var(--muted);
    line-height: 1.55;
  }}
  .pm-meta + .pm-meta {{ margin-top: 4px; }}
  .pm-site {{ color: var(--ink); font-weight: 400; }}

  /* Right column flat list — no card background; just an offset to
     visually align its kicker with the card's interior kicker. */
  .subject-details {{
    padding-top: 22px;
    min-width: 0;
  }}
  .subject-details > .kicker {{ margin-bottom: 14px; }}

  /* -------- Detail rows -------- */
  .detail-row {{
    display: grid;
    grid-template-columns: 130px 1fr;
    gap: 20px;
    padding: 14px 0;
    border-bottom: 1px dashed var(--rule);
  }}
  .detail-row:first-child {{ padding-top: 0; }}
  .detail-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
  .detail-row .lbl {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: var(--muted);
    align-self: center;
  }}
  .detail-row .val {{
    font-size: 15px; color: var(--ink);
    line-height: 1.5; word-break: break-word;
    align-self: center;
  }}
  .detail-row .val em {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-style: italic; color: var(--muted); font-size: 13px;
    margin-left: 6px;
  }}
  .alias-tag {{
    display: inline-block;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; font-weight: 500;
    background: var(--paper-2);
    color: var(--ink);
    padding: 3px 8px;
    border-radius: 3px;
    margin: 2px 4px 2px 0;
    border: 1px solid var(--border);
  }}

  /* -------- Discovered accounts -------- */
  section.accounts {{ margin-top: 30px; }}

  /* -------- Identity cluster grouping -------- */
  .cluster-group {{ margin-bottom: 26px; }}
  .cluster-group:last-child {{ margin-bottom: 0; }}
  .cluster-header {{
    display: flex; align-items: baseline; gap: 10px;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }}
  .cluster-label {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    padding: 2px 7px; border-radius: 3px;
    border: 1px solid var(--border);
  }}
  .cluster-label-primary {{
    color: var(--tier-verified); border-color: var(--tier-verified);
  }}
  .cluster-label-secondary {{
    color: var(--muted); border-color: var(--border);
  }}
  .cluster-label-low {{
    color: var(--muted); border-color: var(--border); opacity: 0.6;
  }}
  .cluster-name {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 17px; color: var(--ink); flex: 1;
  }}
  .cluster-meta {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; color: var(--muted); white-space: nowrap;
  }}

  .accounts-grid {{
    display: grid;
    /* minmax(0, 1fr) — without it, `1fr` resolves to minmax(auto, 1fr),
       and `auto` is min-content. A long URL or display-name inside a
       card then forces its grid cell wider than 50%, blowing the whole
       grid past the 900px container. */
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 14px;
    width: 100%;
  }}

  /* -------- Account filter (inline, header of accounts section) -------- */
  .acct-filter-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    margin-bottom: 14px;
  }}
  .acct-filter-row .kicker {{ margin-bottom: 0; }}
  .acct-filter {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 12px;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--ink);
    padding: 6px 10px;
    border-radius: 4px;
    min-width: 180px;
    max-width: 280px;
    flex: 1 1 220px;
  }}
  .acct-filter::placeholder {{ color: var(--muted); }}
  .acct-filter:focus {{
    outline: none;
    border-color: var(--ink);
  }}
  .acct.is-filtered-out {{ display: none; }}
  .filter-empty {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    color: var(--muted);
    text-align: center;
    padding: 20px;
    display: none;
  }}
  .accounts.is-empty .filter-empty {{ display: block; }}
  .acct {{
    background: var(--paper-2);
    border-radius: 6px;
    padding: 20px 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    border: 1px solid var(--border);
    min-width: 0;
    overflow: hidden;
  }}
  .acct-head {{
    display: flex;
    gap: 18px;
    align-items: flex-start;
    min-width: 0;
  }}
  .acct-head-text {{
    flex: 1;
    min-width: 0;
  }}
  .acct .photo {{
    width: 64px; height: 64px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper) center/cover no-repeat;
    border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  .acct .photo .letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 30px; color: var(--ink);
    line-height: 1; letter-spacing: -0.02em;
  }}
  .acct .display-name {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 22px; line-height: 1.2;
    color: var(--ink); letter-spacing: -0.01em;
    word-break: break-word;
  }}
  .acct .handle {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 14px; color: var(--muted);
    margin-top: 2px;
    word-break: break-all;
  }}
  .acct .bio {{
    font-size: 15px; color: var(--muted);
    line-height: 1.6;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .acct-details {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 13px;
    color: var(--muted);
    line-height: 1.7;
    word-break: break-word;
    margin: 4px 0;
  }}
  .acct-details b.verified-tag {{
    font-weight: 500;
    color: var(--ink);
  }}
  .acct-meta-row {{
    display: flex; align-items: center; gap: 6px;
  }}
  .tier-dot {{
    width: 7px; height: 7px;
    border-radius: 50%; flex-shrink: 0;
    display: inline-block; vertical-align: middle;
  }}
  .tier-verified {{ background: var(--tier-verified); }}
  .score-chip {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; color: var(--muted);
    margin-left: auto;
    letter-spacing: 0.04em;
  }}
  .acct .platform-tag {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 12px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.10em;
    background: var(--paper);
    color: var(--ink);
    padding: 3px 8px;
    border-radius: 3px;
    border: 1px solid var(--border);
  }}
  .acct .open-btn {{
    display: block;
    width: 100%;
    text-align: center;
    box-sizing: border-box;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 13px; font-weight: 500;
    letter-spacing: 0.04em;
    background: var(--btn-bg);
    color: var(--btn-text);
    padding: 14px 16px;
    border-radius: 6px;
    border: 0;
    cursor: pointer;
    text-decoration: none;
    transition: background 0.15s ease;
    margin-top: auto;
  }}
  .acct .open-btn:hover {{ background: var(--btn-hover); }}
  .acct .open-btn .arrow {{ margin-left: 4px; }}

  /* -------- Auxiliary panels (emails / deep / unknowns) -------- */
  section.aux {{ margin-top: 30px; }}
  .aux-panel {{
    background: var(--paper-2);
    border-radius: 6px;
    border: 1px solid var(--border);
    padding: 16px 18px;
  }}
  .aux-table {{
    width: 100%; border-collapse: collapse;
    font-size: 12px;
  }}
  .aux-table th, .aux-table td {{
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px dashed var(--rule);
    vertical-align: top;
  }}
  .aux-table tr:last-child td {{ border-bottom: 0; }}
  .aux-table th {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.14em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }}
  .aux-table td {{ color: var(--ink); }}
  .aux-table .dim {{ color: var(--muted); }}
  .aux-table .err {{ color: var(--err); }}
  .aux-table .platform-tag {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.10em;
    background: var(--paper);
    color: var(--ink);
    padding: 2px 7px; border-radius: 3px;
    border: 1px solid var(--border);
  }}
  .aux-table a {{ color: var(--ink); text-decoration: underline; text-decoration-color: var(--rule); text-underline-offset: 3px; }}
  .aux-table a:hover {{ text-decoration-color: var(--ink); }}
  .aux-notes {{
    display: flex; flex-wrap: wrap; gap: 6px;
    margin-bottom: 12px;
  }}
  .aux-notes .chip {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    background: var(--paper);
    color: var(--muted);
    padding: 3px 8px; border-radius: 3px;
    border: 1px solid var(--border);
    letter-spacing: 0.04em;
  }}

  /* -------- Inconclusive collapsible -------- */
  details.unknown-fold {{ margin-top: 4px; }}
  details.unknown-fold > summary {{
    list-style: none; cursor: pointer; user-select: none;
    display: inline-flex; align-items: center; gap: 10px;
    padding: 9px 14px; border-radius: 4px;
    background: var(--paper-2);
    border: 1px solid var(--border);
    color: var(--ink); font-size: 12px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    text-transform: uppercase; letter-spacing: 0.10em;
    transition: background 0.15s, border-color 0.15s;
  }}
  details.unknown-fold > summary::-webkit-details-marker {{ display: none; }}
  details.unknown-fold > summary::before {{
    content: "›"; font-size: 14px; color: var(--muted);
    transition: transform 0.18s ease;
    line-height: 1;
  }}
  details.unknown-fold[open] > summary::before {{ transform: rotate(90deg); }}
  details.unknown-fold > summary:hover {{
    background: var(--paper); border-color: var(--ink);
  }}
  details.unknown-fold > .aux-panel {{ margin-top: 14px; }}

  /* -------- Footer -------- */
  footer.bottom {{
    margin-top: 38px;
    padding-top: 18px;
    border-top: 1px solid var(--ink);
    display: flex; justify-content: space-between; gap: 18px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: var(--muted);
  }}
  footer.bottom a {{ color: var(--muted); }}
  footer.bottom a:hover {{ color: var(--ink); }}

  @media (max-width: 760px) {{
    .page {{ padding: 28px 18px; }}
    header.top {{ flex-direction: column; gap: 14px; }}
    .header-right {{ justify-content: flex-end; }}
    .file-meta {{ text-align: left; }}
    .portrait {{ width: 96px; height: 96px; }}
    .portrait .letter {{ font-size: 50px; }}
    .ident .handle {{ font-size: 52px; letter-spacing: -0.025em; }}
    .ident .name-region {{ font-size: 18px; margin-top: 10px; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .stat + .stat::before {{ display: none; }}
    .stat:nth-child(odd) {{ border-right: 1px dashed var(--rule); }}
    .stat:nth-child(n+3) {{ border-top: 1px dashed var(--rule); }}
    .stat .n {{ font-size: 40px; }}
    section.combo {{ grid-template-columns: 1fr; gap: 18px; }}
    .subject-details {{ padding-top: 0; }}
    .detail-row {{ grid-template-columns: 1fr; gap: 6px; }}
    .accounts-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
<script>
document.addEventListener('DOMContentLoaded', function() {{
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  function applyTheme(t) {{
    document.documentElement.setAttribute('data-theme', t);
    btn.textContent = t === 'dark' ? '☾' : '☀';
  }}
  var init;
  try {{ init = localStorage.getItem('phantom-theme'); }} catch(e) {{}}
  if (!init) init = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(init);
  btn.addEventListener('click', function() {{
    var cur = document.documentElement.getAttribute('data-theme') || 'light';
    var next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try {{ localStorage.setItem('phantom-theme', next); }} catch(e) {{}}
  }});
}});
</script>
</head>
<body>
<div class="page">

<header class="top">
  <div class="brand">
    <svg class="ghost" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M12 2.4 C6.8 2.4 4 5.8 4 10.6 L4 21.4 L6.6 19.4 L9 21.4 L12 19.4 L15 21.4 L17.4 19.4 L20 21.4 L20 10.6 C20 5.8 17.2 2.4 12 2.4 Z" fill="#1a1612"/>
      <circle cx="9.5" cy="10.6" r="1.3" fill="#f5efe2"/>
      <circle cx="14.5" cy="10.6" r="1.3" fill="#f5efe2"/>
    </svg>
    <span class="wordmark">Phantom</span>
  </div>
  <div class="header-right">
    {toggle_button}
    <div class="file-meta">
      <div>File <span class="num">N° {file_number}</span></div>
      <div>{generated_date} · {generated_time} UTC</div>
      <div>Scan time {elapsed:.1f}s</div>
    </div>
  </div>
</header>

<section class="subject">
  <div class="kicker">Subject of inquiry</div>
  <div class="subject-row">
    {subject_portrait}
    <div class="ident">
      <div class="handle">@{subject_handle}</div>
      <div class="name-region">{subject_name_region}</div>
    </div>
  </div>
</section>

<section class="stats">
  <div class="stat">
    <div class="n">{n_found}</div>
    <div class="l">Confirmed</div>
  </div>
  <div class="stat">
    <div class="n">{n_identities}</div>
    <div class="l">Photo match</div>
  </div>
  <div class="stat">
    <div class="n">{n_variants}</div>
    <div class="l">Aliases tested</div>
  </div>
  <div class="stat">
    <div class="n">{n_sites}</div>
    <div class="l">Sites scanned</div>
  </div>
</section>

<section class="combo">
  {photo_match_block}
  <div class="subject-details">
    <div class="kicker">Subject details</div>
    {detail_rows}
  </div>
</section>

<section class="accounts">
  <div class="acct-filter-row">
    <div class="kicker">Confirmed presence — {n_found} accounts</div>
    <input class="acct-filter" type="search" placeholder="filter accounts…" aria-label="Filter accounts">
  </div>
  {found_section}
  <div class="filter-empty">No accounts match your filter.</div>
</section>

{emails_section}

<footer class="bottom">
  <div>Generated by Phantom</div>
  <div><a href="https://github.com/hamaffs/Phantom-" target="_blank" rel="noopener">github.com/hamaffs/Phantom-</a></div>
</footer>

<script>
(function() {{
  var input = document.querySelector('.acct-filter');
  if (!input) return;
  var section = document.querySelector('section.accounts');
  var cards = Array.prototype.slice.call(document.querySelectorAll('section.accounts .acct'));
  // Build a lowercase haystack per card once at load. Filter is substring,
  // not regex — fast and predictable.
  var index = cards.map(function(c) {{ return c.textContent.toLowerCase(); }});
  function apply() {{
    var q = input.value.trim().toLowerCase();
    var any = false;
    for (var i = 0; i < cards.length; i++) {{
      var match = !q || index[i].indexOf(q) !== -1;
      cards[i].classList.toggle('is-filtered-out', !match);
      if (match) any = true;
    }}
    section.classList.toggle('is-empty', !any && !!q);
  }}
  input.addEventListener('input', apply);
}})();
</script>

</div>
</body>
</html>
"""
def _format_joined(s) -> Optional[str]:
    """Try to render a joined/created date as 'Mar 2024'. Best-effort only."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.strftime("%b %Y")
    except Exception:
        try:
            d = datetime.strptime(str(s), "%a %b %d %H:%M:%S %z %Y")
            return d.strftime("%b %Y")
        except Exception:
            return None
def _format_profile_details(profile: dict) -> str:
    """Render the per-card details strip — followers / following / posts /
    hearts / joined / verified / public-private — bullet-separated.

    Each field is skipped silently if the platform didn't surface it.
    Returns the empty string when nothing's available so the card can
    omit the row entirely. Numeric counts go through `_format_count`
    (12345 → '12.3K'); joined dates go through `_format_joined`. The
    verified flag renders bolder than the rest of the strip; private/
    public is shown as plain text.
    """
    p = profile or {}
    bits: list[str] = []

    if p.get("followers") is not None:
        bits.append(f"{html.escape(_format_count(p['followers']))} followers")
    if p.get("following") is not None:
        bits.append(f"{html.escape(_format_count(p['following']))} following")
    if p.get("posts") is not None:
        bits.append(f"{html.escape(_format_count(p['posts']))} posts")
    if p.get("hearts") is not None:
        bits.append(f"{html.escape(_format_count(p['hearts']))} hearts")

    joined = _format_joined(p.get("joined")) or p.get("joined")
    if joined:
        bits.append(f"joined {html.escape(str(joined))}")

    if p.get("verified") is True:
        bits.append('<b class="verified-tag">verified ✓</b>')

    if p.get("private") is True:
        bits.append("private")
    elif p.get("private") is False:
        bits.append("public")

    if not bits:
        return ""
    return f'<div class="acct-details">{" · ".join(bits)}</div>'
def _html_card(r: CheckResult, photo_match: Optional[list] = None,
               tier: Optional[str] = None, photo_bytes_map: Optional[dict] = None) -> str:
    """Render one FOUND profile as an editorial dossier account card.

    Vertical flow:
      ┌────────────────────────────────────┐
      │ [photo]  display name              │  ← header row
      │          @handle                   │
      │  bio                               │
      │  details · followers · joined …    │
      │  [platform tag]  [●] [score N]     │
      │ ┌────────────────────────────────┐ │
      │ │       Open profile  ↗          │ │  ← full-width CTA
      │ └────────────────────────────────┘ │
      └────────────────────────────────────┘
    """
    p = r.profile or {}
    target = r.url

    photo_url = p.get("photo")
    initial = (p.get("display_name") or r.variant or r.site or "?")[:1].upper()
    if photo_url:
        src = _photo_to_data_uri(photo_url, photo_bytes_map) or html.escape(photo_url, quote=True)
        photo_html = (
            f'<div class="photo" style="background-image:url(\'{src}\')"></div>'
        )
    else:
        photo_html = (
            f'<div class="photo"><span class="letter">{html.escape(initial)}</span></div>'
        )

    display_name = p.get("display_name") or r.variant or r.site
    handle = f"@{r.variant}" if r.variant else r.site
    bio_html = (
        f'<div class="bio">{html.escape(p["bio"])}</div>' if p.get("bio") else ""
    )
    details_html = _format_profile_details(p)

    head = (
        '<div class="acct-head">'
        f'{photo_html}'
        '<div class="acct-head-text">'
        f'<div class="display-name">{html.escape(display_name)}</div>'
        f'<div class="handle">{html.escape(handle)}</div>'
        '</div>'
        '</div>'
    )

    # Tier dot: small green circle for verified identity.
    effective_tier = tier or r.tier
    dot_html = (
        '<span class="tier-dot tier-verified" title="Verified identity"></span>'
        if effective_tier == TIER_VERIFIED else ""
    )
    # Score chip.
    score_html = (
        f'<span class="score-chip">{r.score}</span>'
        if r.score is not None else ""
    )
    meta_row = (
        '<div class="acct-meta-row">'
        f'<span class="platform-tag">{html.escape(r.site)}</span>'
        f'{dot_html}{score_html}'
        '</div>'
    )

    button = (
        f'<a class="open-btn" href="{html.escape(target, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer" '
        f'title="{html.escape(target)}">'
        f'Open profile <span class="arrow">↗</span></a>'
    )

    return (
        '<div class="acct">'
        f'{head}{bio_html}{details_html}{meta_row}{button}'
        '</div>'
    )
def _html_emails_section(found: list, emails: dict) -> str:
    """Per-site Hunter.io results — restyled for the editorial dossier."""
    if not emails:
        return ""
    rows: list[str] = []
    n_emails = 0
    for r in found:
        info = emails.get(r.site)
        if not info:
            continue
        site_cell = (
            f'<span class="platform-tag">{html.escape(r.site)}</span>'
        )
        if info.get("email"):
            n_emails += 1
            email = html.escape(info["email"])
            href = html.escape(info["email"], quote=True)
            score = info.get("score")
            score_part = f' <span class="dim">({score})</span>' if score is not None else ""
            cell = f'<a href="mailto:{href}">{email}</a>{score_part}'
        elif info.get("low_confidence"):
            score = info.get("score")
            tail = f" (score {score})" if score is not None else ""
            cell = f'<span class="dim">low confidence{html.escape(tail)}</span>'
        elif info.get("error"):
            cell = f'<span class="err">error: {html.escape(info["error"])}</span>'
        elif info.get("skipped"):
            cell = f'<span class="dim">skipped: {html.escape(info["skipped"])}</span>'
        else:
            cell = '<span class="dim">no match</span>'
        rows.append(f'<tr><td>{site_cell}</td><td>{cell}</td></tr>')
    if not rows:
        return ""
    table = (
        '<table class="aux-table"><thead><tr>'
        '<th>Site</th><th>Email</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table>'
    )
    return (
        '<section class="aux">'
        f'<div class="kicker">Discovered emails — {n_emails}</div>'
        f'<div class="aux-panel">{table}</div>'
        '</section>'
    )



def _html_unknown_row(r: CheckResult) -> str:
    target = r.url
    return (
        '<tr>'
        f'<td><span class="platform-tag">{html.escape(r.site)}</span></td>'
        f'<td><a href="{html.escape(target, quote=True)}" target="_blank" rel="noopener">'
        f'{html.escape(target)}</a></td>'
        f'<td><span class="dim">{html.escape(r.reason or "unknown")}</span></td>'
        f'<td><span class="dim">{html.escape(r.variant or "")}</span></td>'
        '</tr>'
    )
# Sites whose users typically post real selfies as the avatar — break
# ties in favour of these when several photos contain a detected face.
_SELFIE_SITES = frozenset({
    "Behance", "Instagram", "Twitter", "Threads", "Facebook", "LinkedIn",
})
# Sites that commonly carry logos or stylised avatars rather than faces.
_LOGO_SITES = frozenset({
    "GitHub", "Pastebin", "Disqus", "Pinterest",
})


def _site_priority(site: str) -> int:
    """Lower = more preferred when several face photos tie on cluster size."""
    if site in _SELFIE_SITES:
        return 0
    if site in _LOGO_SITES:
        return 2
    return 1


def _pick_subject_photo(overall, clusters, found, face_map=None) -> Optional[str]:
    """Pick the dossier hero portrait with face-aware priority.

    Order (highest priority first):
      (a) Photos with a detected human face — sort by cluster size desc,
          then by site priority asc (Behance/IG/Twitter/Threads/FB/
          LinkedIn beat GitHub/Pastebin/Disqus/Pinterest beat neutral).
      (b) No face detected anywhere, BUT a selfie-site photo is
          available — Behance, Instagram, etc. are very likely real
          even when Haar misses the face (off-angle, small crop, hat,
          glasses, partial occlusion). Prefer the selfie-site photo
          over a logo-site or generic-avatar photo. Sort by cluster
          size desc, then site priority asc.
      (c) No selfie-site photo either → largest photo-matched cluster's
          representative photo (the user's chosen self-representation,
          logo or otherwise).
      (d) No clusters → first FOUND profile with any photo.
      (e) Nothing → None (caller renders the letter placeholder).

    Only drives the big hero portrait. Per-account 64×64 cards keep
    showing whatever each platform exposed. Logs every candidate +
    selection reason to stderr so future mismatches are debuggable.
    """
    face_map = face_map or {}

    # Cluster-coverage map: photo URL → max number of sites in any
    # cluster that includes that photo. Used by (a) and (b) sorts.
    coverage: dict[str, int] = {}
    for c in (clusters or []):
        size = len(getattr(c, "sites", None) or c.member_indexes)
        for url in (getattr(c, "photos", []) or []):
            if size > coverage.get(url, 0):
                coverage[url] = size

    # Build a candidate list from FOUND profiles' photos.
    candidates: list[dict] = []
    seen_urls: set[str] = set()
    for r in found:
        url = (r.profile or {}).get("photo")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append({
            "url": url,
            "site": r.site,
            "has_face": bool(face_map.get(url)),
            "cluster_size": coverage.get(url, 0),
            "is_selfie_site": r.site in _SELFIE_SITES,
            "is_logo_site": r.site in _LOGO_SITES,
        })

    # Debug logging: every candidate + their decision-relevant fields.
    if candidates:
        print(
            f"[portrait] evaluating {len(candidates)} candidate(s):",
            file=sys.stderr,
        )
        for c in candidates:
            url_short = c["url"] if len(c["url"]) <= 70 else c["url"][:67] + "…"
            tag = ""
            if c["is_selfie_site"]:
                tag = " (selfie-site)"
            elif c["is_logo_site"]:
                tag = " (logo-site)"
            print(
                f"[portrait]   {c['site']:13} face={'yes' if c['has_face'] else 'no ':<3} "
                f"cluster={c['cluster_size']}{tag} {url_short}",
                file=sys.stderr,
            )

    def _log(reason: str, url: str) -> str:
        url_short = url if len(url) <= 70 else url[:67] + "…"
        print(f"[portrait] selected ({reason}): {url_short}", file=sys.stderr)
        return url

    if not candidates:
        if overall and getattr(overall, "photos", None):
            return _log("overall.photos[0], no candidates", overall.photos[0])
        print("[portrait] selected: none (no photos available)", file=sys.stderr)
        return None

    # (a) face-bearing photos: cluster size desc, site priority asc.
    face_cands = [c for c in candidates if c["has_face"]]
    if face_cands:
        face_cands.sort(
            key=lambda c: (-c["cluster_size"], _site_priority(c["site"]))
        )
        return _log("face detected", face_cands[0]["url"])

    # (b) No face anywhere — but selfie-site photos are still very
    # likely real (Haar misses faces routinely on creative profile
    # angles). Prefer them over logo-leaning sites.
    selfie_cands = [c for c in candidates if c["is_selfie_site"]]
    if selfie_cands:
        selfie_cands.sort(
            key=lambda c: (-c["cluster_size"], _site_priority(c["site"]))
        )
        return _log("selfie-site (no face detected)", selfie_cands[0]["url"])

    # (c) largest photo-matched cluster's representative photo.
    multi = [c for c in (clusters or []) if len(c.member_indexes) > 1]
    if multi:
        biggest = max(multi, key=lambda c: len(c.member_indexes))
        if biggest.photos:
            return _log("largest cluster", biggest.photos[0])
    if overall and getattr(overall, "photos", None):
        return _log("overall.photos[0]", overall.photos[0])

    # (d) first FOUND profile with any photo.
    return _log("first candidate", candidates[0]["url"])
def _subject_handle(raw: str, found) -> str:
    """Pick a single @handle to display as the subject identifier.

    For single-token input that's the input itself. For name-mode input
    ('first last'), pick the variant that produced the most FOUND
    accounts — that's the canonical handle the subject actually uses.
    """
    raw = raw.strip()
    if raw and " " not in raw:
        return raw
    counts: dict[str, int] = {}
    for r in found:
        v = (r.variant or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return raw.replace(" ", "")
def _html_subject_portrait(photo_url: Optional[str], handle: str, photo_bytes_map: Optional[dict] = None) -> str:
    """100×100 portrait — image if available, otherwise solid block with
    the first letter of the handle in serif."""
    if photo_url:
        src = _photo_to_data_uri(photo_url, photo_bytes_map) or html.escape(photo_url, quote=True)
        return (
            f'<div class="portrait" '
            f'style="background-image:url(\'{src}\')">'
            f'</div>'
        )
    initial = (handle[:1] or "?").upper()
    return (
        f'<div class="portrait">'
        f'<span class="letter">{html.escape(initial)}</span>'
        f'</div>'
    )
def _format_footprint(overall) -> str:
    """Combine totals (followers / following / posts) into one line."""
    if overall is None:
        return "—"
    bits: list[str] = []
    if getattr(overall, "total_followers", None) is not None:
        bits.append(f"{_format_count(overall.total_followers)} followers")
    if getattr(overall, "total_following", None) is not None:
        bits.append(f"{_format_count(overall.total_following)} following")
    if getattr(overall, "total_posts", None) is not None:
        bits.append(f"{_format_count(overall.total_posts)} posts")
    return " · ".join(bits) if bits else "—"


def _format_region(overall) -> str:
    """Render region from locations + inferred geo hint."""
    if overall is None:
        return "—"
    parts: list[str] = []
    locs = list(getattr(overall, "locations", []) or [])
    if locs:
        parts.append(", ".join(locs))
    geo = getattr(overall, "geo_hint", None)
    if geo and getattr(geo, "region", None) and geo.region not in (locs or []):
        parts.append(
            f'<em>likely {html.escape(geo.region)} '
            f'({html.escape(getattr(geo, "confidence", "low"))})</em>'
        )
    return " · ".join(parts) if parts else "—"
def _build_detail_rows(overall, found, all_variants: list[str]) -> str:
    """Render the four detail rows: region, active since, footprint, aliases."""
    rows: list[tuple[str, str]] = []

    rows.append(("Region", _format_region(overall) or "—"))

    active_since = "—"
    if overall and getattr(overall, "joined_oldest", None):
        formatted = _format_joined(overall.joined_oldest)
        active_since = formatted or html.escape(str(overall.joined_oldest))
    rows.append(("Active since", active_since))

    rows.append(("Footprint", html.escape(_format_footprint(overall))))

    # Aliases: variants that actually surfaced a FOUND result, plus any
    # variants tested overall as light/dim chips. Confirmed first.
    confirmed = sorted({(r.variant or "").strip() for r in found if r.variant})
    confirmed = [v for v in confirmed if v]
    tags = "".join(
        f'<span class="alias-tag">{html.escape(v)}</span>'
        for v in confirmed
    )
    if not tags:
        tags = "—"
    rows.append(("Aliases", tags))

    return "".join(
        f'<div class="detail-row">'
        f'<div class="lbl">{html.escape(label)}</div>'
        f'<div class="val">{value}</div>'
        f'</div>'
        for label, value in rows
    )
def _html_photo_match_card(found, clusters, photo_bytes_map=None) -> str:
    """Render the editorial Photo Match card or empty string.

    Picks the highest-confidence photo-matched cluster (must have ≥ 2
    members) and surfaces two of its photos side by side with a ↔
    glyph between them, plus the cluster's hamming distance and
    confidence score below a dashed divider. When no cluster qualifies
    we return an empty string and CSS lets the right column span the
    full row.
    """
    multi = [c for c in (clusters or []) if len(c.member_indexes) > 1]
    if not multi:
        return ""
    best = max(multi, key=lambda c: getattr(c, "confidence", 0) or 0)

    # Walk the FOUND list once, keep the first 2 (site, photo) pairs
    # whose site is in this cluster's site set. Indexing the cluster
    # member dicts directly isn't possible from here — `member_indexes`
    # references the dedupped found_dicts list at correlation time.
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    cluster_sites = set(getattr(best, "sites", []) or [])
    for r in found:
        if r.site in cluster_sites and r.site not in seen:
            photo = (r.profile or {}).get("photo")
            if photo:
                pairs.append((r.site, photo))
                seen.add(r.site)
        if len(pairs) >= 2:
            break

    if len(pairs) < 2:
        return ""

    site1, photo1 = pairs[0]
    site2, photo2 = pairs[1]

    # Pull a hamming distance out of the cluster's rationale strings
    # (the phash matcher records "matching profile photo (hamming=N)").
    # If the merge came from DINO/Face++ instead, hamming is None and
    # we drop that part of the metadata line.
    hamming = None
    for note in (getattr(best, "rationale", None) or []):
        m = re.search(r"hamming=(\d+)", note)
        if m:
            hamming = int(m.group(1))
            break

    confidence = getattr(best, "confidence", 0) or 0
    meta_bits: list[str] = []
    if hamming is not None:
        meta_bits.append(f"Hamming distance: {hamming}")
    meta_bits.append(f"Confidence: {confidence:.2f}")
    meta_line = " · ".join(meta_bits)

    src1 = _photo_to_data_uri(photo1, photo_bytes_map) or html.escape(photo1, quote=True)
    src2 = _photo_to_data_uri(photo2, photo_bytes_map) or html.escape(photo2, quote=True)
    return (
        '<aside class="photo-match">'
        '<div class="kicker">Photo match</div>'
        '<div class="pm-photos">'
        f'<div class="pm-thumb" style="background-image:url(\'{src1}\')"></div>'
        '<div class="pm-arrow">↔</div>'
        f'<div class="pm-thumb" style="background-image:url(\'{src2}\')"></div>'
        '</div>'
        '<div class="pm-divider">'
        f'<div class="pm-meta">Same profile photo confirmed across '
        f'<span class="pm-site">{html.escape(site1)}</span> and '
        f'<span class="pm-site">{html.escape(site2)}</span></div>'
        f'<div class="pm-meta">{html.escape(meta_line)}</div>'
        '</div>'
        '</aside>'
    )
def _html_unknown_section(unknown: list) -> str:
    """Restyled inconclusive collapsible. Empty string when no unknowns."""
    if not unknown:
        return ""
    rows = "".join(_html_unknown_row(r) for r in unknown)
    table = (
        '<table class="aux-table"><thead><tr>'
        '<th>Site</th><th>URL</th><th>Reason</th><th>Variant</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table>'
    )
    n = len(unknown)
    plural = "s" if n != 1 else ""
    return (
        '<section class="aux">'
        f'<div class="kicker">Inconclusive — {n}</div>'
        '<details class="unknown-fold">'
        f'<summary>Show {n} inconclusive result{plural}</summary>'
        f'<div class="aux-panel">{table}</div>'
        '</details>'
        '</section>'
    )
def _html_cluster_section(found: list, dis_clusters: list, photo_bytes_map: Optional[dict] = None) -> str:
    """Build the found_section HTML grouped by identity cluster."""
    parts: list[str] = []

    LABEL_MAP = {
        LABEL_PRIMARY:   ("cluster-label-primary",   "Primary Identity"),
        LABEL_SECONDARY: ("cluster-label-secondary", "Secondary Cluster"),
        LABEL_LOW:       ("cluster-label-low",        "Unrelated Matches"),
    }

    primary_and_secondary = [c for c in dis_clusters
                             if c.label in (LABEL_PRIMARY, LABEL_SECONDARY)]
    low_clusters = [c for c in dis_clusters if c.label == LABEL_LOW]

    for c in primary_and_secondary:
        css_cls, label_text = LABEL_MAP.get(c.label, ("cluster-label-low", c.label))
        meta_parts = [f"{c.size} account{'s' if c.size != 1 else ''}"]
        if c.location:
            meta_parts.append(html.escape(c.location))
        name_html = (
            f'<span class="cluster-name">{html.escape(c.display_name)}</span>'
            if c.display_name else ""
        )
        header = (
            f'<div class="cluster-header">'
            f'<span class="cluster-label {css_cls}">{label_text}</span>'
            f'{name_html}'
            f'<span class="cluster-meta">{html.escape(" · ".join(meta_parts))}</span>'
            f'</div>'
        )
        members = [found[i] for i in c.member_indices if i < len(found)]
        # Sort: verified-tier first, then by score desc
        members.sort(key=lambda r: (
            0 if r.tier == TIER_VERIFIED else 1,
            -(r.score or 0),
        ))
        cards_html = "".join(
            _html_card(r, tier=TIER_VERIFIED if r.tier == TIER_VERIFIED else None,
                       photo_bytes_map=photo_bytes_map)
            for r in members
        )
        parts.append(
            f'<div class="cluster-group">'
            f'{header}'
            f'<div class="accounts-grid">{cards_html}</div>'
            f'</div>'
        )

    # Low-confidence clusters go in a collapsible
    if low_clusters:
        total_low = sum(c.size for c in low_clusters)
        inner_html = ""
        for c in low_clusters:
            members = [found[i] for i in c.member_indices if i < len(found)]
            inner_html += "".join(_html_card(r, photo_bytes_map=photo_bytes_map) for r in members)
        parts.append(
            f'<details class="unknown-fold" style="margin-top:18px">'
            f'<summary>Unrelated matches ({total_low})</summary>'
            f'<div class="aux-panel" style="margin-top:14px">'
            f'<div class="accounts-grid">{inner_html}</div>'
            f'</div></details>'
        )

    return "\n".join(parts) if parts else (
        '<div class="accounts-grid">'
        '<div class="acct" style="grid-column:1/-1;justify-content:center">'
        '<div class="body"><div class="bio">No confirmed accounts.</div></div>'
        '</div></div>'
    )
def export_html(grouped, raw, elapsed, path: Path, overall=None, clusters=None, emails=None, deep_evidence=None, face_map=None, dark=False, include_toggle=True, dis_clusters=None, photo_bytes_map=None) -> None:
    found, _, missing_count = _flatten(grouped)
    clusters = clusters or []
    multi = [c for c in clusters if len(c.member_indexes) > 1]

    # When disambiguation has run, use primary-cluster members for the subject
    # overview so stats reflect the real person, not all matched accounts.
    primary_cluster = None
    if dis_clusters:
        primary_cluster = next(
            (c for c in dis_clusters if c.label == LABEL_PRIMARY), None
        )
    if primary_cluster:
        primary_found = [found[i] for i in primary_cluster.member_indices if i < len(found)]
    else:
        primary_found = found

    # --- Subject hero ---
    subject_handle = _subject_handle(raw, primary_found)
    portrait_url = _pick_subject_photo(overall, clusters, primary_found, face_map)
    subject_portrait_html = _html_subject_portrait(portrait_url, subject_handle, photo_bytes_map)

    if primary_cluster and primary_cluster.display_name:
        subject_name_region = html.escape(primary_cluster.display_name)
    elif overall and getattr(overall, "display_name", None):
        subject_name_region = html.escape(overall.display_name)
    else:
        subject_name_region = "&nbsp;"

    # --- Stats counts ---
    n_variants = len(grouped)
    n_sites = len(grouped[0][1]) if grouped and grouped[0][1] else 0

    # --- Combo section: photo-match card + subject details (primary only) ---
    photo_match_block = _html_photo_match_card(primary_found, clusters, photo_bytes_map)
    detail_rows_html = _build_detail_rows(
        overall, primary_found, [v for v, _ in grouped]
    )

    # --- Account cards: cluster-grouped or tier-grouped ---
    if found:
        if dis_clusters:
            found_section_html = _html_cluster_section(found, dis_clusters, photo_bytes_map)
        else:
            scored_html = any(r.tier is not None for r in found)
            if scored_html:
                v_cards = [r for r in found if r.tier == TIER_VERIFIED]
                l_cards = [r for r in found if r.tier == TIER_LIKELY]
                i_cards = [r for r in found if r.tier == TIER_IMPOSTOR]
                l_cards += [r for r in found if r.tier is None]
            else:
                v_cards, l_cards, i_cards = [], found, []
            primary_html = (
                "".join(_html_card(r, tier=TIER_VERIFIED, photo_bytes_map=photo_bytes_map) for r in v_cards)
                + "".join(_html_card(r, photo_bytes_map=photo_bytes_map) for r in l_cards)
            )
            found_section_html = f'<div class="accounts-grid">{primary_html}</div>'
            if i_cards:
                impostor_inner = "".join(_html_card(r, photo_bytes_map=photo_bytes_map) for r in i_cards)
                found_section_html += (
                    '<details class="unknown-fold" style="margin-top:18px">'
                    f'<summary>Possible impostors ({len(i_cards)})</summary>'
                    '<div class="aux-panel" style="margin-top:14px">'
                    f'<div class="accounts-grid">{impostor_inner}</div>'
                    '</div></details>'
                )
    else:
        found_section_html = (
            '<div class="accounts-grid">'
            '<div class="acct" style="grid-column:1/-1;justify-content:center">'
            '<div class="body"><div class="bio">No confirmed accounts.</div></div>'
            '</div></div>'
        )

    # --- Auxiliary panels ---
    emails_section = _html_emails_section(found, emails) if emails else ""

    # --- Theme ---
    theme = "dark" if dark else "light"
    toggle_button = (
        '<button id="theme-toggle" class="theme-toggle" title="Toggle theme">☀</button>'
        if include_toggle else ""
    )

    # --- File metadata ---
    now = datetime.now(timezone.utc)
    file_number = f"{random.randint(1000, 9999)}"
    generated_date = now.strftime("%b %d, %Y")
    generated_time = now.strftime("%H:%M")

    page = _HTML_TEMPLATE.format(
        raw_html=html.escape(raw),
        file_number=file_number,
        generated_date=generated_date,
        generated_time=generated_time,
        elapsed=elapsed,
        subject_portrait=subject_portrait_html,
        subject_handle=html.escape(subject_handle),
        subject_name_region=subject_name_region,
        n_found=len(found),
        n_identities=len(multi),
        n_variants=n_variants,
        n_sites=n_sites,
        photo_match_block=photo_match_block,
        detail_rows=detail_rows_html,
        found_section=found_section_html,
        emails_section=emails_section,
        theme=theme,
        toggle_button=toggle_button,
    )
    path.write_text(page, encoding="utf-8")
