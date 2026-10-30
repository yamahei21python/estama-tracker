"""Display-only evidence for alerts.

Attaches WHY a cast is interesting to an alert: review count/score/tags
(kagawa_menes.db), the time today's first slot vanished (slot_events in
estama.db), bakusai chatter (bakusai.db) and source URLs.

Nothing here feeds status math, history or the forecast. Every source
is best-effort: a missing DB, a shop that is not linked, or a cast with
no reviews simply produces no line, and the alert itself is unchanged.

Attribution is deliberately strict (see INTEGRATION_DESIGN.md):
  * reviews: the shop must resolve through shop_aliases and the cast
    must match by normalized full name.
  * bakusai: the post body must contain the full cast name AND the
    thread must map to the shop with confidence >= 0.8. Decoy/partial
    name candidates are crawler output only and never surface here.
"""
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
MIN_BAKUSAI_CONFIDENCE = 0.8


def default_review_db() -> Path:
    """Review DB path; env is read lazily so tests can redirect it."""
    return Path(os.environ.get("ESTAMA_REVIEW_DB") or
                Path.home() / "Myproject" / "menesureview" / "kagawa_menes.db")


def default_bakusai_db() -> Path:
    return Path(os.environ.get("ESTAMA_BAKUSAI_DB") or BASE / "bakusai.db")

# Review-text feature tags: counted per review (a review mentioning
# "スタイル" twice still counts once), so "美人×7" means 7 reviews said
# it, not 7 keyword hits. Display only - no scoring uses these.
TAG_RULES = (
    ("スタイル抜群", ("スタイルが良", "スタイル抜群", "スレンダー", "細身")),
    ("胸が大きい", ("巨乳", "胸が大き", "カップ", "おっぱい")),
    ("美人", ("美人", "可愛い", "かわいい", "顔が好")),
    ("揉みが強い", ("揉みが強", "力強い", "強めの施術", "しっかり揉")),
    ("癒し系", ("癒し", "癒され", "リラックス")),
    ("話が面白い", ("話が面白", "会話が楽", "トーク")),
    ("テクニック", ("テクニック", "上手", "気持ち良", "気持ちよ")),
    ("丁寧", ("丁寧", "真面目", "親切")),
    ("ディープリンパ", ("ディープ", "深リンパ", "リンパ")),
)

_conns = {}


def _open_ro(path):
    """Read-only connection, cached per path. None when unusable."""
    key = str(path) if path else ""
    if key in _conns:
        return _conns[key]
    con = None
    if key and Path(key).exists():
        try:
            con = sqlite3.connect(f"file:{key}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
        except sqlite3.Error:
            con = None
    _conns[key] = con
    return con


def close_all():
    for con in _conns.values():
        if con is not None:
            con.close()
    _conns.clear()


def _nfkc(s):
    return unicodedata.normalize("NFKC", s or "")


def norm_person(name):
    """Cast-name normalization shared by the review/bakusai matchers.

    Unifies the formats actually seen in the sources: estama uses
    ideographic spaces ("白石　さち"), reviews add ages and honorifics
    ("白石 さちさん", "白石さち (40代前半)さん")."""
    s = _nfkc(name)
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"(さん|ちゃん|くん|様)$", "", s)
    return s.casefold()


def norm_text(s):
    return re.sub(r"\s+", "", _nfkc(s))


def review_shop_id(rconn, shop_name):
    """menes shop id for an estama shop name, via shop_aliases.

    Exact alias first; then NFKC/space-insensitive equality. No fuzzy
    matching - a wrong shop would put another shop's reviews on the
    alert."""
    if rconn is None or not shop_name:
        return None
    try:
        row = rconn.execute(
            "SELECT menes_id FROM shop_aliases WHERE estama_name = ?",
            (shop_name,)).fetchone()
        if row:
            return row[0]
        target = norm_text(shop_name)
        for mid, alias in rconn.execute(
                "SELECT menes_id, estama_name FROM shop_aliases"):
            if norm_text(alias) == target:
                return mid
    except sqlite3.Error:
        return None
    return None


def review_evidence(rconn, shop_name, cast_name):
    """{'linked', 'n', 'score', 'stars', 'fetched', 'tags', 'url'}.

    linked=False means the shop had no alias; the cast still may have
    zero reviews when linked is True."""
    mid = review_shop_id(rconn, shop_name)
    if mid is None:
        return {"linked": False, "n": 0, "tags": []}
    try:
        rows = rconn.execute(
            """SELECT cast_name, score, recommend_stars, body_preview,
                      scraped_at
               FROM reviews WHERE shop_id = ?""", (mid,)).fetchall()
    except sqlite3.Error:
        return {"linked": False, "n": 0, "tags": []}
    target = norm_person(cast_name)
    hits = [r for r in rows if norm_person(r["cast_name"]) == target]
    if not hits:
        return {"linked": True, "n": 0, "tags": []}
    scores = [r["score"] for r in hits if r["score"] is not None]
    stars = [r["recommend_stars"] for r in hits
             if r["recommend_stars"] is not None]
    fetched = max((r["scraped_at"] or "") for r in hits)[:10] or None
    counts = []
    for tag, kws in TAG_RULES:
        n = sum(1 for r in hits
                if any(k in (r["body_preview"] or "") for k in kws))
        if n:
            counts.append((tag, n))
    counts.sort(key=lambda x: -x[1])
    url = None
    try:
        row = rconn.execute(
            "SELECT hp_url FROM shops WHERE id = ?", (mid,)).fetchone()
        url = row[0] if row and row[0] else None
    except sqlite3.Error:
        pass
    return {
        "linked": True,
        "n": len(hits),
        "score": round(sum(scores) / len(scores), 1) if scores else None,
        "stars": round(sum(stars) / len(stars), 1) if stars else None,
        "fetched": fetched,
        "tags": counts[:2],
        "url": url,
    }


def cast_key_for(conn, shop_id, cast_name, date):
    """cast_key exactly as snapshots/slot_events derive it."""
    try:
        row = conn.execute(
            """SELECT CAST(COALESCE(cast_id, cast_name) AS TEXT)
               FROM snapshots
               WHERE shop_id = ? AND cast_name = ? AND target_date = ?
               ORDER BY fetched_at DESC LIMIT 1""",
            (shop_id, cast_name, date)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def slot_loss_evidence(conn, shop_id, cast_key, date):
    """First time an open slot (○/TEL) closed (×/─) today, + loss count.

    A 'return' (×→○) is not a loss; only the transitions below count,
    so the number means 'slots that flipped shut and stayed reported'."""
    if not cast_key:
        return None
    try:
        rows = conn.execute(
            """SELECT slot_time, from_mark, to_mark, observed_at
               FROM slot_events
               WHERE shop_id = ? AND cast_key = ? AND target_date = ?
                 AND from_mark IN ('○', 'TEL') AND to_mark IN ('×', '─')
               ORDER BY observed_at, slot_minutes""",
            (shop_id, cast_key, date)).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    return {"slot_time": rows[0][0],
            "lost_at": (rows[0][3] or "")[5:16],
            "n": len(rows)}


def bakusai_evidence(bconn, shop_id, cast_name):
    """{'n', 'last', 'quote'} for exact full-name mentions in threads
    attributed to this shop. Only confidence >= 0.8 threads are read;
    decoy candidates never reach this function's output.

    The name filter runs over every attributed post (no LIMIT before
    it): truncating to the newest N posts first would hide older
    mentions and undercount n."""
    if bconn is None:
        return None
    try:
        rows = bconn.execute(
            """SELECT p.post_no, p.posted_at, p.body
               FROM bakusai_posts p
               JOIN bakusai_shop_map m ON m.thread_id = p.thread_id
               WHERE m.shop_id = ? AND m.confidence >= ?
               ORDER BY p.posted_at DESC""",
            (shop_id, MIN_BAKUSAI_CONFIDENCE))
    except sqlite3.Error:
        return None
    target = norm_person(cast_name)
    if not target:
        return None
    hits, latest = 0, None
    for r in rows:
        if target in norm_text(r["body"]).casefold():
            hits += 1
            if latest is None:
                latest = r
    if not latest:
        return None
    quote = re.sub(r"\s+", " ", (latest["body"] or "")).strip()
    return {"n": hits,
            "last": (latest["posted_at"] or "")[:10] or None,
            "quote": quote[:90]}


def collect(conn, shop_id, shop_name, cast_name, date,
            review_db=None, bakusai_db=None):
    """Everything evidence_lines needs, as one dict (None = unavailable)."""
    rconn = _open_ro(review_db if review_db is not None
                     else default_review_db())
    bconn = _open_ro(bakusai_db if bakusai_db is not None
                     else default_bakusai_db())
    try:
        urlrow = conn.execute(
            "SELECT source_url FROM shops WHERE shop_id = ?",
            (shop_id,)).fetchone()
    except sqlite3.Error:
        urlrow = None
    return {
        "review": review_evidence(rconn, shop_name, cast_name),
        "slots": slot_loss_evidence(
            conn, shop_id, cast_key_for(conn, shop_id, cast_name, date),
            date),
        "bakusai": bakusai_evidence(bconn, shop_id, cast_name),
        "url": urlrow[0] if urlrow else None,
    }


def format_lines(ev, prefix="      ", compact=False):
    """Render collect() output. compact=True is the one-line push form."""
    lines = []
    rev = ev.get("review") or {}
    slot = ev.get("slots")
    bak = ev.get("bakusai")
    url = ev.get("url")
    if compact:
        bits = []
        if rev.get("n"):
            bits.append(f"口コミ{rev['n']}件"
                        + (f" {rev['score']}" if rev.get("score") else ""))
        if slot:
            bits.append(f"枠消失{slot['lost_at']}")
        if bak:
            bits.append(f"爆サイ{bak['n']}件")
        if bits:
            lines.append(prefix + "根拠: " + " / ".join(bits))
        return lines

    if rev.get("n"):
        bits = [f"口コミ n={rev['n']}"]
        if rev.get("score") is not None:
            bits.append(f"総合 {rev['score']}")
        if rev.get("stars") is not None:
            bits.append(f"★{rev['stars']}")
        if rev.get("fetched"):
            bits.append(f"取得 {rev['fetched']}")
        line = prefix + "・" + " ".join(bits)
        if rev.get("tags"):
            line += " / タグ: " + ", ".join(
                f"{t}×{n}" for t, n in rev["tags"])
        lines.append(line)
    elif rev.get("linked"):
        lines.append(prefix + "・口コミ: 該当なし（店舗は紐付け済み）")
    if slot:
        lines.append(
            prefix + f"・枠: {slot['slot_time']}枠が {slot['lost_at']} 消失"
            f" (本日 {slot['n']}枠)")
    if bak:
        last = f" 最終 {bak['last'][5:]}" if bak.get("last") else ""
        lines.append(prefix + f"・爆サイ: 言及{bak['n']}件{last}")
        if bak.get("quote"):
            lines.append(prefix + f"  「{bak['quote']}」")
    src = [url] if url else []
    if rev.get("url"):
        src.append(rev["url"])
    if src:
        lines.append(prefix + "・出典: " + " / ".join(src))
    return lines
