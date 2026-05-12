"""
The Brush List — site generator.

Reads the artworks/ folder (filenames = paintings) and the connected
Instagram account, joins them by mentions in post captions, and writes
out a static index.html with a leaderboard of views per model per artwork.

Also persists every run to a flat JSON history file (brushlist-history.json)
so we can compute a daily ledger of "which model gained the most views in
the last 24h" and stack those days into a rolling history.

The history file is compacted on every save: only the latest snapshot per
(post, UTC day) is kept, so file size grows with posts × days, not runs.

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
HISTORY_PATH = Path("brushlist-history.json")

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


def pretty_date(iso_date):
    """`2026-05-13` → `13 May`."""
    try:
        d = dt.datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return iso_date
    return d.strftime("%-d %b") if os.name != "nt" else d.strftime("%d %b")


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


# ---------- history (local JSON file) ----------
def load_history():
    """Read brushlist-history.json, or return an empty shell if missing/corrupt."""
    if HISTORY_PATH.exists():
        try:
            data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            data.setdefault("snapshots", [])
            return data
        except (json.JSONDecodeError, OSError) as exc:
            print(f"!! couldn't read {HISTORY_PATH} ({exc}); starting fresh")
    return {"snapshots": []}


def save_history(history):
    """Compact (keep latest per post per UTC hour) then write the file.

    Hour-level granularity is fine resolution for anchored 24h windows and
    caps file growth even if the cron runs more often than hourly.
    """
    by_key = {}
    for s in history.get("snapshots", []):
        key = (s["post_id"], s["taken_at"][:13])  # YYYY-MM-DDTHH
        existing = by_key.get(key)
        if existing is None or s["taken_at"] > existing["taken_at"]:
            by_key[key] = s
    history["snapshots"] = sorted(
        by_key.values(),
        key=lambda s: (s["taken_at"], s["post_id"]),
    )
    HISTORY_PATH.write_text(
        json.dumps(history, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def record_run(history, posts):
    """Append one snapshot row per post for this moment."""
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for p in posts:
        history["snapshots"].append({
            "taken_at":     now,
            "post_id":      p["id"],
            "artwork_slug": p["artwork"],
            "tool":         p["tool"],
            "views":        p["views"],
            "likes":        p["likes"],
        })


def compute_daily_champions(history):
    """Daily ledger using rolling 24h windows anchored to the first run.

    Day 1 = [first_run, first_run + 24h).  Day N = [first_run + (N-1)*24h, +N*24h).
    For each post, we take its latest snapshot at-or-before each window's end
    and diff against the prior window's end.  Windows that contain no fresh
    snapshot (script outage / data gap) are skipped, not faked.

    The latest window is flagged in_progress when last_ts < window_end so the
    UI can show "still counting".
    """
    snapshots = history.get("snapshots", [])
    if not snapshots:
        return []

    def parse_ts(s):
        return dt.datetime.fromisoformat(s)

    parsed = [(parse_ts(s["taken_at"]), s) for s in snapshots]
    parsed.sort(key=lambda x: x[0])

    anchor   = parsed[0][0]
    last_ts  = parsed[-1][0]
    n_days   = int((last_ts - anchor).total_seconds() // 86400) + 1

    # snapshots grouped by post, in time order, for fast lookups
    by_post = {}
    for ts, s in parsed:
        by_post.setdefault(s["post_id"], []).append((ts, s))

    def views_as_of(post_snaps, cutoff):
        """Latest (views, tool) for this post at-or-before cutoff."""
        best = None
        for ts, s in post_snaps:
            if ts <= cutoff:
                best = s
            else:
                break
        return (best["views"], best.get("tool")) if best else (None, None)

    champions = []
    for day_num in range(1, n_days + 1):
        w_start = anchor + dt.timedelta(days=day_num - 1)
        w_end   = anchor + dt.timedelta(days=day_num)

        # data gap: skip days with no fresh snapshot inside the window
        had_data = any(w_start <= ts < w_end for ts, _ in parsed)
        if not had_data:
            continue

        # for each post, delta = views_at_w_end - views_at_w_start
        # (day 1's "views_at_w_start" is treated as 0 → baseline = full views)
        tools = {}
        for post_id, snaps in by_post.items():
            v_end, tool_end = views_as_of(snaps, w_end)
            if v_end is None or not tool_end:
                continue
            if day_num == 1:
                delta = v_end
            else:
                v_prev, _ = views_as_of(snaps, w_start)
                delta = v_end if v_prev is None else max(0, v_end - v_prev)
            bucket = tools.setdefault(tool_end, {"views": 0, "posts": 0})
            bucket["views"] += delta
            bucket["posts"] += 1

        if not tools:
            continue

        ranked = sorted(tools.items(), key=lambda kv: -kv[1]["views"])
        winner_tool, winner_stats = ranked[0]
        champions.append({
            "day_num":      day_num,
            "window_start": w_start.isoformat(timespec="seconds"),
            "window_end":   w_end.isoformat(timespec="seconds"),
            "tool":         winner_tool,
            "views":        winner_stats["views"],
            "posts":        winner_stats["posts"],
            "is_baseline":  (day_num == 1),
            "in_progress":  (last_ts < w_end),
            "runners_up":   [
                {"tool": t, "views": s["views"]} for t, s in ranked[1:4]
            ],
        })
    return champions


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

    # persist this run, then compute the daily ledger
    history = load_history()
    record_run(history, posts)
    save_history(history)
    daily_champions = compute_daily_champions(history)
    print(f"History: {len(history['snapshots'])} snapshot(s) · ledger spans {len(daily_champions)} day(s).")

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
    out = render(artworks, by_artwork, global_tools, posts, unmatched,
                 grand_total, daily_champions)
    OUTPUT_PATH.write_text(out, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}  ({len(out):,} bytes)")


# ---------- HTML rendering ----------
def render(artworks, by_artwork, global_tools, posts, unmatched,
           grand_total, daily_champions):
    e = html.escape
    now = dt.datetime.now(dt.timezone.utc)
    issue_date = now.strftime("%-d %b %Y") if os.name != "nt" else now.strftime("%d %b %Y")

    # champion = tool with most total views globally
    if global_tools:
        champ_name, champ = max(global_tools.items(), key=lambda kv: kv[1]["views"])
    else:
        champ_name, champ = None, None

    # ---- all-time champion card ----
    if champ_name:
        appears_in = sum(1 for b in by_artwork.values() if champ_name in b["tools"])
        champion_html = f"""
          <section class="champion">
            <div class="champ-meta">
              <div class="laurel">Champion</div>
              <h2 class="champ-name">{e(champ_name)}</h2>
              <div class="champ-maker">{champ['posts']} post{'s' if champ['posts'] != 1 else ''} · across {appears_in} work{'s' if appears_in != 1 else ''}</div>
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

    # ---- daily ledger ----
    if daily_champions:
        # the anchor (start of Day 01) defines the rhythm — show it as a hint
        anchor_iso = daily_champions[0]["window_start"]
        try:
            anchor_dt = dt.datetime.fromisoformat(anchor_iso)
            anchor_label = anchor_dt.strftime("%H:%M UTC")
        except ValueError:
            anchor_label = ""

        ledger_rows = ""
        for c in daily_champions:
            runners = ""
            if c["runners_up"]:
                parts = [f"{e(r['tool'])} +{fmt(r['views'])}" for r in c["runners_up"]]
                runners = " · ".join(parts)

            # nicer date label: "13 May → 14 May" if it crosses a calendar day
            try:
                ws = dt.datetime.fromisoformat(c["window_start"])
                we = dt.datetime.fromisoformat(c["window_end"])
                date_label = ws.strftime("%-d %b") if os.name != "nt" else ws.strftime("%d %b")
                if ws.date() != we.date():
                    end_label = we.strftime("%-d %b") if os.name != "nt" else we.strftime("%d %b")
                    date_label = f"{date_label} → {end_label}"
            except ValueError:
                date_label = c["window_start"][:10]

            tags = ""
            if c["is_baseline"]:
                tags += ' <span class="ledger-tag baseline">baseline</span>'
            if c["in_progress"]:
                tags += ' <span class="ledger-tag live">live</span>'

            ledger_rows += f"""
              <li class="ledger-row">
                <div class="ledger-day">
                  <span class="day-num">Day {c['day_num']:02d}</span>
                  <span class="day-date">{e(date_label)}{tags}</span>
                </div>
                <div class="ledger-winner">
                  <span class="winner-name">{e(c['tool'])}</span>
                  <span class="winner-views">+{fmt(c['views'])}<span class="vlabel">views</span></span>
                </div>
                <div class="ledger-runners">{runners}</div>
              </li>"""

        ledger_html = f"""
          <div class="section-head">
            <h2>The daily <em>ledger</em></h2>
            <span class="note">{len(daily_champions)} day{'s' if len(daily_champions) != 1 else ''} · anchored {e(anchor_label)}</span>
          </div>
          <ol class="ledger">{ledger_rows}</ol>"""
    else:
        ledger_html = ""

    # ---- per-artwork series ----
    active_artworks = [slug for slug, b in by_artwork.items() if b["tools"]]
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

    return TEMPLATE.format(
        issue_date=e(issue_date),
        champion=champion_html,
        ledger=ledger_html,
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

  /* ---- daily ledger ---- */
  .ledger {{
    list-style: none; padding: 0; margin: 8px 0 0;
    display: flex; flex-direction: column;
  }}
  .ledger-row {{
    display: grid;
    grid-template-columns: 140px 1fr auto;
    gap: 24px; align-items: center;
    padding: 22px 4px;
    border-bottom: 1px solid rgba(20,18,15,0.12);
  }}
  .ledger-row:last-child {{ border-bottom: none; }}
  .ledger-day {{ display: flex; flex-direction: column; gap: 4px; }}
  .ledger-day .day-num {{
    font-family: "Fraunces", serif;
    font-variation-settings: "opsz" 144, "WONK" 1;
    font-style: italic; font-weight: 400; font-size: 30px;
    line-height: 1; letter-spacing: -0.015em; color: var(--accent);
  }}
  .ledger-day .day-date {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.18em; color: var(--ink-soft);
    display: flex; align-items: center; gap: 8px;
  }}
  .baseline-tag, .ledger-tag {{
    display: inline-block; padding: 2px 6px;
    border-radius: 2px; font-size: 9px; letter-spacing: 0.15em;
  }}
  .ledger-tag.baseline {{
    background: var(--paper-deep); color: var(--ink-soft);
  }}
  .ledger-tag.live {{
    background: var(--accent); color: var(--paper);
  }}
  .ledger-tag.live::before {{
    content: ""; display: inline-block;
    width: 5px; height: 5px; border-radius: 50%;
    background: var(--paper); margin-right: 5px; vertical-align: middle;
    animation: pulse 1.6s ease-in-out infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 0.4; }} 50% {{ opacity: 1; }}
  }}
  .ledger-winner {{ display: flex; align-items: baseline; gap: 18px; flex-wrap: wrap; }}
  .ledger-winner .winner-name {{
    font-variation-settings: "opsz" 144, "WONK" 1;
    font-weight: 500; font-size: 24px; letter-spacing: -0.01em;
  }}
  .ledger-winner .winner-views {{
    font-family: "JetBrains Mono", monospace;
    font-variant-numeric: tabular-nums; font-size: 16px;
    color: var(--accent); font-weight: 500;
  }}
  .ledger-winner .winner-views .vlabel {{
    display: inline; font-size: 9px; text-transform: uppercase;
    letter-spacing: 0.15em; color: var(--ink-soft); margin-left: 6px;
  }}
  .ledger-runners {{
    font-family: "JetBrains Mono", monospace; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.13em; color: var(--ink-soft);
    text-align: right; max-width: 360px; line-height: 1.5;
  }}

  /* ---- per-artwork series ---- */
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
    .ledger-row {{ grid-template-columns: 90px 1fr; gap: 16px; }}
    .ledger-runners {{ display: none; }}
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

  {ledger}

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
