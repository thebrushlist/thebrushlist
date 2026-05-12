"""
The Brush List — site generator.

Reads the artworks/ folder (filenames = paintings) and the connected
Instagram account, joins them by mentions in post captions, and writes
out a static index.html with a leaderboard of views per model per artwork.

Caption convention:
    "Day 4 of Night Tide. The lighting finally came together. Using: Sora 2"

The script extracts:
- Which artwork: any artwork name appearing anywhere in the caption.
- Which model: whatever follows the word "using" up to the next period
  or newline. Colon is optional.

Run locally:
    IG_ACCESS_TOKEN="IGAA..." python3 generate.py

Or via GitHub Actions (see .github/workflows/rebuild.yml).
"""

import os
import re
import json
import html
import datetime as dt
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError


# ---------- config ----------
IG_USER_ID   = "17841426968416033"        # not secret
ACCESS_TOKEN = os.environ.get("IG_ACCESS_TOKEN")

API_BASE    = "https://graph.instagram.com"
API_VERSION = "v21.0"

ARTWORKS_DIR = Path("artworks")
OUTPUT_PATH  = Path("index.html")

# Canonical tool names + aliases for normalization.
# Aliases are matched after normalize() (lowercase, strip non-alphanumerics),
# so "Sora 2", "sora-2", "SORA2" all collapse to the same canonical row.
KNOWN_TOOLS = {
    "Sora 2":               ["sora", "sora2"],
    "Midjourney v7":        ["midjourney", "mj", "midjourneyv7", "mjv7"],
    "Flux 1.1 Pro":         ["flux", "fluxpro", "flux11", "flux11pro"],
    "DALL·E 4":             ["dalle", "dalle4", "dalle-4"],
    "Stable Diffusion 3.5": ["sd", "sd35", "stablediffusion", "stablediffusion35", "sdxl"],
    "Veo 3":                ["veo", "veo3"],
    "Runway Gen-3":         ["runway", "runwaygen3", "gen3"],
}


# ---------- helpers ----------
def normalize(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def title_from_filename(name):
    """`night-tide.jpg` → `Night Tide`."""
    stem = Path(name).stem
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", stem))


def slug_from_filename(name):
    """`night-tide.jpg` → `nighttide` (used for caption matching)."""
    return normalize(Path(name).stem)


def fmt(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "—"


# ---------- IG API ----------
def api_get(path_or_url, **params):
    if path_or_url.startswith("http"):
        url = path_or_url
    else:
        params["access_token"] = ACCESS_TOKEN
        url = f"{API_BASE}/{API_VERSION}/{path_or_url.lstrip('/')}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "brushlist-generator/1.0"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read()), None
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body
        return None, f"HTTP {e.code}: {msg}"


def fetch_all_media():
    items, next_url = [], None
    while True:
        if next_url:
            data, err = api_get(next_url)
        else:
            data, err = api_get(
                f"{IG_USER_ID}/media",
                fields="id,caption,permalink,timestamp,like_count,comments_count",
                limit=50,
            )
        if err:
            print(f"!! media fetch error: {err}")
            return items
        items.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return items


def fetch_views(media_id):
    for metric in ("views", "impressions"):
        data, err = api_get(f"{media_id}/insights", metric=metric)
        if err:
            continue
        for row in data.get("data", []):
            if row.get("name") == metric:
                tv = row.get("total_value", {}).get("value")
                if tv is not None:
                    return tv
                vals = row.get("values", [])
                if vals:
                    return vals[0].get("value")
    return 0


# ---------- parsing ----------
USING_RE = re.compile(r"\busing\b\s*:?\s*([^.\n]+)", re.IGNORECASE)


def canonicalize_tool(raw):
    """Map a raw 'using: X' capture to a canonical tool name, or echo it."""
    if not raw:
        return None
    norm = normalize(raw)
    if not norm:
        return None
    for canonical, aliases in KNOWN_TOOLS.items():
        if normalize(canonical) == norm:
            return canonical
        for alias in aliases:
            if normalize(alias) == norm:
                return canonical
    return raw.strip()


def detect_tool(caption):
    m = USING_RE.search(caption or "")
    if not m:
        return None
    raw = m.group(1).strip(" .,:;-—")
    return canonicalize_tool(raw)


def detect_artwork(caption, artworks):
    """Return the slug whose normalized name appears in caption, longest first."""
    norm_caption = normalize(caption or "")
    for art in sorted(artworks, key=lambda a: -len(a["slug"])):
        if art["slug"] and art["slug"] in norm_caption:
            return art["slug"]
    return None


# ---------- main ----------
def main():
    if not ACCESS_TOKEN:
        raise SystemExit("Missing env var IG_ACCESS_TOKEN.")

    # discover artworks from the folder
    artworks = []
    if ARTWORKS_DIR.exists():
        for path in sorted(ARTWORKS_DIR.iterdir()):
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                artworks.append({
                    "slug": slug_from_filename(path.name),
                    "title": title_from_filename(path.name),
                    "image": path.as_posix(),
                })
    print(f"Found {len(artworks)} artwork(s).")

    # pull posts + views
    media = fetch_all_media()
    print(f"Fetched {len(media)} post(s) from Instagram.")

    posts = []
    for m in media:
        caption = m.get("caption") or ""
        views = fetch_views(m["id"]) or 0
        posts.append({
            "id":        m["id"],
            "timestamp": m.get("timestamp", ""),
            "permalink": m.get("permalink", ""),
            "caption":   caption,
            "views":     views,
            "likes":     m.get("like_count") or 0,
            "artwork":   detect_artwork(caption, artworks),
            "tool":      detect_tool(caption),
        })

    # aggregate: per-artwork → per-tool → total views
    by_artwork = {a["slug"]: {"art": a, "tools": {}, "first_seen": None, "total": 0}
                  for a in artworks}
    global_tools = {}
    grand_total = 0
    unmatched = []

    for p in posts:
        slug = p["artwork"]
        tool = p["tool"]
        views = p["views"]
        grand_total += views

        if tool:
            global_tools.setdefault(tool, {"views": 0, "posts": 0})
            global_tools[tool]["views"] += views
            global_tools[tool]["posts"] += 1

        if slug and slug in by_artwork:
            bucket = by_artwork[slug]
            bucket["total"] += views
            ts = p["timestamp"]
            if ts and (bucket["first_seen"] is None or ts < bucket["first_seen"]):
                bucket["first_seen"] = ts
            if tool:
                bucket["tools"].setdefault(tool, {"views": 0, "posts": 0})
                bucket["tools"][tool]["views"] += views
                bucket["tools"][tool]["posts"] += 1
        else:
            unmatched.append(p)

    # ---------- render HTML ----------
    out = render(artworks, by_artwork, global_tools, posts, unmatched, grand_total)
    OUTPUT_PATH.write_text(out, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}  ({len(out):,} bytes)")


# ---------- HTML rendering ----------
def render(artworks, by_artwork, global_tools, posts, unmatched, grand_total):
    e = html.escape
    now = dt.datetime.now(dt.timezone.utc)
    issue_date = now.strftime("%-d %b %Y") if os.name != "nt" else now.strftime("%d %b %Y")

    # champion = tool with most total views globally
    if global_tools:
        champ_name, champ = max(global_tools.items(), key=lambda kv: kv[1]["views"])
    else:
        champ_name, champ = None, None

    # series cards — only artworks that have at least one post
    active_artworks = [
        slug for slug, b in by_artwork.items() if b["tools"]
    ]
    active_artworks.sort(key=lambda s: -by_artwork[s]["total"])

    series_html = ""
    if not active_artworks:
        series_html = """
          <p style="font-family:'JetBrains Mono',monospace;font-size:12px;
                    color:var(--ink-soft);padding:36px 0;">
            No artworks have been mentioned in posts yet. Upload images to /artworks
            and reference them by name in IG captions.
          </p>"""
    else:
        for slug in active_artworks:
            b = by_artwork[slug]
            art = b["art"]
            first_seen = (b["first_seen"] or "")[:10]
            ranked = sorted(b["tools"].items(), key=lambda kv: -kv[1]["views"])
            top_views = ranked[0][1]["views"] if ranked else 1
            rows = ""
            for i, (tool, stats) in enumerate(ranked, 1):
                win_cls = " win" if i == 1 else ""
                bar_pct = int(stats["views"] / top_views * 100) if top_views else 0
                rows += f"""
                  <div class="post{win_cls}">
                    <div class="post-rank">{i}</div>
                    <div class="post-meta">
                      <p class="name">{e(tool)}</p>
                      <span class="post-sub">{stats['posts']} post{'s' if stats['posts'] != 1 else ''}</span>
                    </div>
                    <div class="post-views">{fmt(stats['views'])}<span class="label">views</span></div>
                    <div class="bar-wrap"><div class="bar" style="width:{bar_pct}%"></div></div>
                  </div>"""

            series_html += f"""
              <article class="series">
                <div class="artwork">
                  <div class="artwork-frame"><img src="{e(art['image'])}" alt="{e(art['title'])}"></div>
                  <div class="artwork-no">{('Since ' + first_seen) if first_seen else 'Unposted'}</div>
                  <h3 class="artwork-title">{e(art['title'])}</h3>
                  <div class="artwork-medium">{fmt(b['total'])} views · {len(ranked)} model{'s' if len(ranked) != 1 else ''}</div>
                </div>
                <div class="series-board">{rows}</div>
              </article>"""

    # champion card
    if champ_name:
        champion_html = f"""
          <section class="champion">
            <div class="champ-meta">
              <div class="laurel">Champion</div>
              <h2 class="champ-name">{e(champ_name)}</h2>
              <div class="champ-maker">{champ['posts']} post{'s' if champ['posts'] != 1 else ''} · across {sum(1 for b in by_artwork.values() if champ_name in b['tools'])} work{'s' if sum(1 for b in by_artwork.values() if champ_name in b['tools']) != 1 else ''}</div>
            </div>
            <div class="champ-stat">
              <div class="big">{fmt(champ['views'])}</div>
              <div class="small">organic views</div>
            </div>
          </section>"""
    else:
        champion_html = """
          <section class="champion">
            <div class="champ-meta">
              <div class="laurel">No champion yet</div>
              <h2 class="champ-name">—</h2>
              <div class="champ-maker">Post on Instagram with "Using: [model name]" to start the count</div>
            </div>
          </section>"""

    return TEMPLATE.format(
        issue_date=e(issue_date),
        champion=champion_html,
        series=series_html,
        artwork_count=len(active_artworks),
        post_count=len(posts),
        grand_total=fmt(grand_total),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>The Brush List</title>
<meta property="og:title" content="The Brush List">
<meta property="og:description" content="Ranking AI tools by the organic views they earn for real paintings.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght,SOFT,WONK@0,9..144,300..900,0..100,0..1;1,9..144,300..900,0..100,0..1&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper: #f3eee5;
    --paper-shade: #ebe4d6;
    --paper-deep: #e2d9c6;
    --ink: #131310;
    --ink-soft: #5a554c;
    --accent: #c1342e;
    --gold: #b8893b;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; }}
  body {{
    background: var(--paper); color: var(--ink);
    font-family: "Fraunces", Georgia, serif;
    font-feature-settings: "ss01", "ss02", "liga", "onum";
    min-height: 100vh;
    background-image: radial-gradient(rgba(20,18,15,0.035) 1px, transparent 1px);
    background-size: 3px 3px;
  }}
  .wrap {{ max-width: 1040px; margin: 0 auto; padding: 56px 32px 96px; }}

  .masthead {{
    display: flex; justify-content: space-between; align-items: baseline;
    padding-bottom: 12px; border-bottom: 2px solid var(--ink);
  }}
  .brand {{
    font-variation-settings: "opsz" 144, "WONK" 1;
    font-weight: 500; font-size: 28px; letter-spacing: -0.01em;
  }}
  .brand em {{ font-style: italic; color: var(--accent); }}
  .meta {{
    font-family: "JetBrains Mono", monospace; font-size: 11px;
    color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.12em;
  }}

  .hero {{ padding: 36px 0 24px; }}
  .eyebrow {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.22em;
    color: var(--accent); margin-bottom: 14px;
  }}
  h1 {{
    font-variation-settings: "opsz" 144, "WONK" 1, "SOFT" 50;
    font-weight: 300; font-size: clamp(40px, 6.5vw, 72px);
    line-height: 0.95; letter-spacing: -0.025em; margin: 0;
  }}
  h1 em {{ font-style: italic; color: var(--accent); }}

  .champion {{
    margin: 40px 0 60px; background: var(--ink); color: var(--paper);
    border-radius: 4px; padding: 40px 48px;
    display: grid; grid-template-columns: 1fr auto;
    gap: 40px; align-items: center;
    position: relative; overflow: hidden;
  }}
  .champion::before {{
    content: ""; position: absolute; inset: 0;
    background:
      radial-gradient(circle at 20% 100%, rgba(193,52,46,0.25), transparent 50%),
      radial-gradient(circle at 80% 0%, rgba(184,137,59,0.2), transparent 50%);
    pointer-events: none;
  }}
  .champion > * {{ position: relative; z-index: 1; }}
  .laurel {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.25em; color: var(--gold);
    margin-bottom: 8px; display: flex; align-items: center; gap: 8px;
  }}
  .laurel::before, .laurel::after {{
    content: ""; width: 24px; height: 1px; background: var(--gold);
  }}
  .champ-name {{
    font-variation-settings: "opsz" 144, "WONK" 1;
    font-style: italic; font-weight: 400; font-size: 64px;
    line-height: 1; letter-spacing: -0.03em; margin: 6px 0 4px;
  }}
  .champ-maker {{
    font-family: "JetBrains Mono", monospace; font-size: 11px;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: rgba(243,238,229,0.6);
  }}
  .champ-stat {{
    text-align: right; font-family: "JetBrains Mono", monospace;
    font-variant-numeric: tabular-nums;
  }}
  .champ-stat .big {{
    font-family: "Fraunces", serif; font-variation-settings: "opsz" 144;
    font-weight: 300; font-size: 56px; line-height: 1;
    letter-spacing: -0.02em; color: var(--paper);
  }}
  .champ-stat .small {{
    font-size: 10px; text-transform: uppercase; letter-spacing: 0.18em;
    color: rgba(243,238,229,0.55); margin-top: 6px;
  }}

  .section-head {{
    display: flex; align-items: baseline; justify-content: space-between;
    margin: 56px 0 16px; padding-bottom: 10px; border-bottom: 2px solid var(--ink);
  }}
  .section-head h2 {{
    font-variation-settings: "opsz" 144, "WONK" 1;
    font-weight: 400; font-size: 32px; margin: 0; letter-spacing: -0.015em;
  }}
  .section-head h2 em {{ font-style: italic; color: var(--accent); }}
  .section-head .note {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.18em; color: var(--ink-soft);
  }}

  .series {{
    display: grid; grid-template-columns: 220px 1fr;
    gap: 36px; padding: 36px 0;
    border-bottom: 1px solid rgba(20,18,15,0.15);
  }}
  .artwork {{ display: flex; flex-direction: column; gap: 12px; }}
  .artwork-frame {{
    width: 100%; aspect-ratio: 4/5; border-radius: 3px;
    box-shadow: 0 1px 0 rgba(0,0,0,0.04), 0 10px 28px -10px rgba(20,18,15,0.35);
    overflow: hidden; background: var(--paper-shade);
  }}
  .artwork-frame img {{
    width: 100%; height: 100%; object-fit: cover; display: block;
  }}
  .artwork-no {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.18em; color: var(--ink-soft);
  }}
  .artwork-title {{
    font-variation-settings: "opsz" 144, "WONK" 1;
    font-style: italic; font-weight: 400; font-size: 22px;
    line-height: 1.1; letter-spacing: -0.01em; margin: 0;
  }}
  .artwork-medium {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.15em; color: var(--ink-soft);
  }}

  .series-board {{ display: flex; flex-direction: column; gap: 2px; }}
  .post {{
    display: grid; grid-template-columns: 36px 1fr auto 110px;
    gap: 16px; padding: 16px 14px; align-items: center;
    background: var(--paper-shade); border-radius: 3px;
  }}
  .post.win {{
    background: linear-gradient(90deg, rgba(193,52,46,0.12), rgba(193,52,46,0.04));
    border-left: 2px solid var(--accent);
  }}
  .post-rank {{
    font-variation-settings: "opsz" 144; font-weight: 400; font-size: 24px;
    color: var(--ink-soft); line-height: 1;
  }}
  .post.win .post-rank {{
    color: var(--accent); font-style: italic; font-weight: 500;
  }}
  .post-meta .name {{
    font-size: 17px; font-weight: 500; letter-spacing: -0.01em; margin: 0;
  }}
  .post-sub {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.13em; color: var(--ink-soft);
  }}
  .post-views {{
    font-family: "JetBrains Mono", monospace;
    font-variant-numeric: tabular-nums; font-size: 18px;
    text-align: right; letter-spacing: -0.01em;
  }}
  .post-views .label {{
    display: block; font-size: 9px; text-transform: uppercase;
    letter-spacing: 0.15em; color: var(--ink-soft); margin-top: 2px;
  }}
  .bar-wrap {{
    width: 100px; height: 6px; background: rgba(20,18,15,0.08);
    border-radius: 3px; overflow: hidden;
  }}
  .bar {{ height: 100%; background: var(--ink); border-radius: 3px; }}
  .post.win .bar {{ background: var(--accent); }}

  .colophon {{
    margin-top: 56px; padding-top: 20px;
    border-top: 1px solid rgba(20,18,15,0.18);
    display: flex; justify-content: space-between;
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.15em; color: var(--ink-soft);
  }}
  .colophon em {{
    font-family: "Fraunces", serif; font-style: italic;
    text-transform: none; letter-spacing: 0; font-size: 13px; color: var(--ink);
  }}

  @media (max-width: 760px) {{
    .champion {{ grid-template-columns: 1fr; text-align: center; padding: 32px 24px; }}
    .champ-stat {{ text-align: center; }}
    .series {{ grid-template-columns: 1fr; }}
    .artwork {{ flex-direction: row; align-items: flex-start; gap: 16px; }}
    .artwork-frame {{ width: 120px; flex-shrink: 0; }}
    .post {{ grid-template-columns: 28px 1fr auto; }}
    .bar-wrap {{ display: none; }}
  }}
</style>
</head>
<body>

<div class="wrap">

  <header class="masthead">
    <div class="brand">The Brush <em>List</em></div>
    <div class="meta">{issue_date}</div>
  </header>

  <section class="hero">
    <div class="eyebrow">Organic Views · By Model · By Artwork</div>
    <h1>Which model <em>painted</em><br>its way to the top?</h1>
  </section>

  {champion}

  <div class="section-head">
    <h2>By <em>artwork</em></h2>
    <span class="note">{artwork_count} work · {post_count} posts · {grand_total} views</span>
  </div>

  {series}

  <footer class="colophon">
    <span>Source · <em>Instagram Graph API</em></span>
    <span>Rebuilds hourly</span>
  </footer>

</div>

</body>
</html>
"""


if __name__ == "__main__":
    main()
