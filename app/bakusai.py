#!/usr/bin/env python3
"""bakusai.py - bakusai.com discovery and thread crawler (bakusai.db).

Separate from estama.db on purpose: scraped chatter is best-effort
evidence for alerts (see evidence.py), never part of the status math.

Discovery follows INTEGRATION_DESIGN.md:
  * the 7 Osaka boards' latest-30 lists (/thr_tl/.../p=1/),
  * the area-wide thread search (POST /sch_result/) for each tracked
    shop, with every form field and the session cookie the endpoint
    requires,
  * a notation-variant dictionary for shop names.

If the search endpoint is unavailable (missing fields, lost cookie,
access error) discovery falls back to latest-30 + dictionary only and
records the failure per shop - it never fabricates links.

Crawling is limited to the fixed deep tier (deep_shops.json) and stores
the first 500 chars of each post with post number, timestamp and the
anchor it replies to (MAX_ANCHOR_HOPS for chain reconstruction). Decoy
names are saved as candidates only; alert attribution stays restricted
to threads whose title names the shop (evidence.py enforces >= 0.8)
plus an exact full cast name in the body.

Backfill is resumable and capped at DAILY_BUDGET requests per day;
the per-day counter lives in bakusai_requests, so a killed run simply
continues on the next invocation.
"""

import argparse
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime
from email.utils import mktime_tz, parsedate_tz
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
JST = ZoneInfo("Asia/Tokyo")
DEFAULT_DB = Path(os.environ.get("ESTAMA_BAKUSAI_DB") or BASE / "bakusai.db")
DEFAULT_SHOPS = BASE / "shops.json"
DEFAULT_DEEP = BASE / "deep_shops.json"

HOST = "https://bakusai.com"
ACODE = 7
CTGID = 136
BOARDS = {
    1714: "キタ・北摂近郊",
    6066: "中央・東部",
    6067: "ミナミ・泉州",
    2383: "大阪メンエス総合",
    4887: "大阪メンエス個人",
    5878: "大阪外国人お店",
    5882: "大阪外国人総合",
}
SEARCH_FORM_BID = 2383  # area search is acode-wide; any board id satisfies the form
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

BODY_LIMIT = 500
MAX_ANCHOR_HOPS = 10
DAILY_BUDGET = 500
MIN_DELAY = 1.5
MAX_DELAY = 2.0
MIN_ALERT_CONFIDENCE = 0.8
PAGE_SIZE = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS bakusai_threads (
  thread_id     TEXT PRIMARY KEY,
  bid           INTEGER,
  title         TEXT,
  url           TEXT,
  ttgid         TEXT,
  replies       INTEGER,
  max_page      INTEGER,
  first_post_no INTEGER,
  last_post_at  TEXT,
  first_seen    TEXT,
  checked_at    TEXT
);
CREATE TABLE IF NOT EXISTS bakusai_posts (
  thread_id  TEXT NOT NULL,
  post_no    INTEGER NOT NULL,
  posted_at  TEXT,
  body       TEXT,
  reply_to   INTEGER,
  fetched_at TEXT,
  PRIMARY KEY (thread_id, post_no)
);
CREATE TABLE IF NOT EXISTS bakusai_shop_map (
  thread_id  TEXT NOT NULL,
  shop_id    INTEGER NOT NULL,
  shop_name  TEXT,
  confidence REAL NOT NULL,
  source     TEXT,
  matched    TEXT,
  updated_at TEXT,
  PRIMARY KEY (thread_id, shop_id)
);
CREATE TABLE IF NOT EXISTS bakusai_mentions (
  thread_id  TEXT NOT NULL,
  post_no    INTEGER NOT NULL,
  cast_name  TEXT NOT NULL,
  match_type TEXT NOT NULL,
  shop_id    INTEGER,
  PRIMARY KEY (thread_id, post_no, cast_name)
);
CREATE TABLE IF NOT EXISTS bakusai_shop_search (
  shop_id    INTEGER PRIMARY KEY,
  word       TEXT,
  status     TEXT,
  n_results  INTEGER,
  checked_at TEXT
);
CREATE TABLE IF NOT EXISTS bakusai_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS bakusai_requests (
  day  TEXT NOT NULL,
  kind TEXT NOT NULL,
  n    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, kind)
);
"""

# Board titles and shop names share these tokens; matching on them
# would link every thread to every shop in the same area.
GENERIC_WORDS = {
    "メンズエステ", "メンエス", "エステ", "スパ", "spa", "大阪", "日本橋",
    "梅田", "難波", "堺", "京橋", "天王寺", "本町", "心斎橋", "新大阪",
    "高級", "room", "ルーム", "店", "センター", "collection", "premium",
    "premium", "mvp", "エロ", "大人", "お店",
}

CIRCLED = "\u2460-\u2473\u3251-\u325f\u32b1-\u32bf"
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF"
                   "\uFE0F\u200D\u200B\u20E3\u2122]")
MASK_CHARS = "○◯〇●⚪"
MASK_RE = re.compile("[" + MASK_CHARS + "]")


class BudgetExhausted(RuntimeError):
    pass


class SearchFailed(RuntimeError):
    pass


def now_jst():
    return datetime.now(JST)


def connect(path=None):
    con = sqlite3.connect(str(path or DEFAULT_DB))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- naming --
def norm(text):
    """NFKC + drop circled thread numbers/emoji/punctuation, lower-case.

    Shared by title matching and body mention lookup, so a thread title
    like 'ぽっちゃりエステつつみ ⑱' matches the shop name exactly.
    """
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = EMOJI.sub("", s)
    s = re.sub("[" + CIRCLED + "]", " ", s)
    s = re.sub(r"[「」『』【】\[\]（）()<>《》〈〉]", "", s)
    s = re.sub(r"[\s\u3000]", "", s)
    s = re.sub(r"[.\,\-_/\\~〜～ー―‐—・:;!?'\"`|+*=&#%^$@※☆★♪♡♥…‥、。]",
               "", s)
    return re.sub(r"(店|るーむ|room)+$", "", s)


def name_variants(name):
    """Full name, name before parentheses, name before a space."""
    out = []
    for v in (name,
              re.split(r"[（(【\[]", name or "")[0],
              re.split(r"[\s\u3000]", name or "")[0]):
        n = norm(v)
        if n and len(n) >= 2:
            out.append(n)
    return list(dict.fromkeys(out))


def _distinctive(normed):
    """True when generic area/type words are not the whole string.

    '梅田メンズエステ' is shared by many shops; matching a thread title
    on it says nothing about which shop the thread is about.
    """
    rest = normed
    for w in sorted((norm(w) for w in GENERIC_WORDS), key=len, reverse=True):
        if w:
            rest = rest.replace(w, "")
    return bool(rest)


def _name_segments(shop_name):
    """Distinctive chunks of a display name, generic area words dropped.

    '京橋｜MOTEJO🩷教育された...' keeps only MOTEJO; 'belle femme
    （ベルファーム）　マット型コンセプト' keeps both chunks.
    """
    s = unicodedata.normalize("NFKC", shop_name or "")
    s = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]?", " ", s)
    s = EMOJI.sub(" ", s)
    s = re.sub("[" + CIRCLED + "]", " ", s)
    s = re.sub(r"[|｜~〜]", " ", s)
    s = re.sub(r"[\s\u3000]+", " ", s)
    out = []
    for seg in s.split(" "):
        seg = seg.strip("-—…‥・:;!?\"'「」『』,.")
        seg = re.sub(r"(店|ルーム|るーむ|room)$", "", seg, flags=re.I)
        n = norm(seg)
        if len(n) >= 2 and n not in GENERIC_WORDS:
            out.append(seg.strip())
    return list(dict.fromkeys(out))


def search_word(shop_name):
    """Area-search query: cleaned name with internal spaces preserved."""
    segs = _name_segments(shop_name)
    return " ".join(segs) or (shop_name or "").strip()


def fallback_search_word(shop_name):
    """Leading brand segment, retried only when the full name finds nothing.

    Descriptor-heavy names ("MOTEJO 教育された... マンション型") return no
    hits as a whole, while the leading segment still matches.
    """
    segs = _name_segments(shop_name)
    if len(segs) > 1 and segs[0] != search_word(shop_name):
        return segs[0]
    return ""


def _is_ascii(word):
    return bool(word) and word.isascii() and bool(re.search(r"[a-z0-9]", word))


def _title_has(nt, v):
    """Containment with the guards that stop the known false positives.

    ASCII names must sit on an ASCII word boundary, so 'flower' no
    longer matches CharmeBellfloweR and 'cheri' no longer matches
    Cherish/MACHERIE. Very short CJK names ('俺の家' matched drama
    titles, 'ダイヤ' matched ダイヤのA) must be the title's primary shop:
    nothing but digits/area words may remain after removing the name.
    """
    if _is_ascii(v):
        return re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])",
                         nt) is not None
    if v not in nt:
        return False
    if len(v) <= 3:
        rest = nt.replace(v, "")
        rest = re.sub(r"[0-9]", "", rest)
        for w in GENERIC_WORDS:
            w = norm(w)
            if len(w) >= 2:
                rest = rest.replace(w, "")
        return not rest
    return True


def title_match(title, shop_name):
    """(confidence, matched_variant) or None for one shop vs one title.

    1.0 full normalized name, 0.9 parenthesis/space-stripped variant,
    0.6 a long distinctive token (candidate only, below the alert
    threshold). Generic area words never match.
    """
    nt = norm(title)
    if not nt:
        return None
    variants = name_variants(shop_name)
    if not variants:
        return None
    full = norm(shop_name)
    if full and len(full) >= 2 and _title_has(nt, full):
        return 1.0, full
    for v in variants[1:]:
        if len(v) >= 3 and _distinctive(v) and _title_has(nt, v):
            return 0.9, v
    # Token fallback: split the RAW name first - norm() deletes the
    # separators, which made this branch unreachable for names like
    # 'sirena~シレーナ~'.
    toks = list(variants)
    for seg in re.split(r"[~|｜/〜～&＆]", shop_name or ""):
        n = norm(seg)
        if n:
            toks.append(n)
    for tok in toks:
        for t in _tokens(tok):
            if t in GENERIC_WORDS or len(t) < 4 or not _distinctive(t):
                continue
            if _title_has(nt, t):
                return 0.6, t
    return None


def _tokens(normed_name):
    for part in re.split(r"[~|｜/〜～&＆]", normed_name):
        if len(part) >= 4:
            yield part


# ---------------------------------------------------------------- parsing --
def parse_thread_list(page_html, bid):
    """Latest-30 list rows: [{tid,bid,title,replies}] (deduped by tid)."""
    start = page_html.find('id="thrListInner"')
    if start < 0:
        return []
    end = page_html.find("</ul>", start)
    body = page_html[start:end if end > 0 else len(page_html)]
    rows, seen = [], set()
    for block in re.split(r"<li\s", body)[1:]:
        m = re.search(r'<a href="([^"]*tid=(\d+)[^"]*)"[^>]*title="([^"]*)"',
                      block)
        if not m:
            continue
        tid, title = m.group(2), html.unescape(m.group(3))
        if tid in seen:
            continue
        seen.add(tid)
        rep = re.search(r"レス数</span>\s*</span>\s*<span>([\d,]+)</span>",
                        block)
        rows.append({"tid": tid, "bid": bid, "title": title.strip(),
                     "replies": int(rep.group(1).replace(",", ""))
                     if rep else None})
    return rows


def parse_search_results(page_html):
    """POST /sch_result/ result rows: [{tid,bid,title}]."""
    rows, seen = [], set()
    for m in re.finditer(
            r'<a href="([^"]*tid=(\d+)[^"]*)"[^>]*>\s*'
            r'<h2[^>]*title="([^"]*)"', page_html):
        href, tid, title = m.group(1), m.group(2), html.unescape(m.group(3))
        if tid in seen:
            continue
        seen.add(tid)
        b = re.search(r"bid=(\d+)", href)
        rows.append({"tid": tid, "bid": int(b.group(1)) if b else None,
                     "title": title.strip()})
    return rows


def parse_thread_page(page_html):
    """One thr_res page -> {'posts': [...], 'max_page': int, 'total': int}.

    Posts carry post_no, posted_at ('YYYY-MM-DD HH:MM:SS'), the first
    500 chars of the body and reply_to (the >>N anchor), if any.
    """
    posts = []
    for m in re.finditer(
            r'<div class="article res_list_article[^"]*"[^>]*id="res(\d+)">'
            r"(.*?)<div class=\"name_goodbad_box\">", page_html, re.S):
        post_no, block = int(m.group(1)), m.group(2)
        t = re.search(r'commentTime">([^<]*)<', block)
        posted_at = None
        if t:
            try:
                posted_at = datetime.strptime(
                    t.group(1).strip(), "%Y/%m/%d %H:%M").strftime(
                        "%Y-%m-%d %H:%M:%S")
            except ValueError:
                posted_at = t.group(1).strip()
        b = re.search(r'itemprop="commentText">(.*?)</div>', block, re.S)
        raw = b.group(1) if b else block
        # Quoted reply overlay: <span data-tipso='...escaped html...'
        # class="resOverlay">>>N</span>. Drop it before unescaping, or the
        # quote's escaped markup turns into literal text.
        raw = re.sub(r"<span[^>]*\bdata-tipso=[^>]*>.*?</span>", " ", raw,
                     flags=re.S)
        text = html.unescape(raw)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        anchor = re.search(r"&gt;&gt;\s*(\d+)\s*</a>", block)
        if not anchor:
            anchor = re.search(r">>\s*(\d+)", text)
        posts.append({"post_no": post_no, "posted_at": posted_at,
                      "body": text[:BODY_LIMIT],
                      "reply_to": int(anchor.group(1)) if anchor else None})
    pages = [int(p) for p in re.findall(
        r'href="[^"]*/thr_res/[^"]*?/p=(\d+)/tp=1/"', page_html)]
    cur = re.search(r'class="paging_number">(\d+)<', page_html)
    if cur:
        pages.append(int(cur.group(1)))
    total = re.search(r"(\d+)\s*レス", page_html)
    return {"posts": posts, "max_page": max(pages) if pages else 1,
            "total": int(total.group(1)) if total else None}


# ---------------------------------------------------------------- client --
class Client:
    """Paced, budgeted HTTP access to bakusai.com.

    `http(method, url, data, headers, redirects)` defaults to
    requests.Session; tests inject a fake returning status_code/text/
    headers/url. Every request spends one unit of the day's budget.
    """

    def __init__(self, conn, http=None, day=None, budget=DAILY_BUDGET,
                 min_delay=MIN_DELAY, max_delay=MAX_DELAY, sleep=None,
                 log=print):
        self.conn = conn
        self._fixed_day = day
        self.budget = budget
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._sleep = sleep or time.sleep
        self._log = log
        self._cookies = {}
        self.requests_made = 0
        if http is None:
            import requests
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT,
                              "Accept-Language": "ja,en;q=0.8"})
            self._session = s
            self._http = self._requests_http
        else:
            self._http = http

    @property
    def day(self):
        """Budget day (JST) - re-evaluated so a run crossing midnight
        cannot bill the new day's requests to the previous one."""
        return self._fixed_day or now_jst().strftime("%Y-%m-%d")

    def _requests_http(self, method, url, data, headers, redirects):
        r = self._session.request(method, url, data=data, headers=headers,
                                  timeout=25, allow_redirects=redirects)
        return r

    def spent_today(self):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(n), 0) FROM bakusai_requests WHERE day = ?",
            (self.day,)).fetchone()
        return row[0]

    def _spend(self, kind):
        if self.spent_today() >= self.budget:
            raise BudgetExhausted(
                f"1日のリクエスト上限 {self.budget} に到達")
        self.conn.execute(
            "INSERT INTO bakusai_requests(day, kind, n) VALUES(?,?,1)"
            " ON CONFLICT(day, kind) DO UPDATE SET n = n + 1",
            (self.day, kind))
        self.conn.commit()
        self.requests_made += 1

    @staticmethod
    def _retry_after(r):
        """Retry-After seconds: integer form or an HTTP date."""
        raw = str((getattr(r, "headers", {}) or {}).get("Retry-After", ""))
        try:
            wait = int(raw.strip())
        except (TypeError, ValueError):
            stamp = parsedate_tz(raw)
            wait = int(mktime_tz(stamp) - time.time()) if stamp else 10
        wait = max(wait, 1)
        if wait > 3600:
            raise BudgetExhausted(
                f"429 Retry-After が {wait}s のため本日は停止")
        return wait

    def _send(self, method, url, data, headers, kind):
        """One HTTP hit: bill it, pace it, honour 429 Retry-After."""
        attempts = 0
        while True:
            self._spend(kind)
            if self.requests_made > 1:
                self._sleep(random.uniform(self.min_delay, self.max_delay))
            r = self._http(method, url, data, headers, False)
            if getattr(r, "status_code", 0) != 429 or attempts >= 2:
                return r
            attempts += 1
            wait = self._retry_after(r)
            self._log(f"  429: {wait}s 待機して再試行")
            self._sleep(wait)

    def request(self, method, url, data=None, referer=None, redirects=True,
                kind="misc"):
        headers = {}
        if referer:
            headers["Referer"] = referer
        hops = 0
        while True:
            r = self._send(method, url, data, headers, kind)
            loc = (getattr(r, "headers", {}) or {}).get("Location")
            code = getattr(r, "status_code", 0)
            if not redirects or not loc or code not in (301, 302, 303,
                                                        307, 308):
                return r
            hops += 1
            if hops > 5:
                return r
            # Follow manually so every hop is billed and paced; letting
            # requests follow silently made the 500/day cap leak.
            headers["Referer"] = url
            url = urljoin(url, loc)
            if code not in (307, 308):
                method, data = "GET", None

    # -- discovery -----------------------------------------------------
    def latest_threads(self, bid):
        url = (f"{HOST}/thr_tl/acode={ACODE}/ctgid={CTGID}/bid={bid}/p=1/")
        r = self.request("GET", url, kind="latest")
        if getattr(r, "status_code", 0) != 200:
            raise SearchFailed(f"board {bid}: HTTP {r.status_code}")
        return parse_thread_list(r.text, bid)

    def search_threads(self, word):
        """Area-wide thread search; raises SearchFailed when unavailable."""
        url = f"{HOST}/sch_result/"
        referer = (f"{HOST}/thr_tl/acode={ACODE}/ctgid={CTGID}"
                   f"/bid={SEARCH_FORM_BID}/")
        data = {"bid": str(SEARCH_FORM_BID), "tid": "", "ctgid": str(CTGID),
                "acode": str(ACODE), "ctrid": "", "sch": "thr_sch",
                "sch_range": "area", "word": word,
                "button1": "スレッド検索"}
        r = self.request("POST", url, data=data, referer=referer, kind="search")
        effective = getattr(r, "url", "") or ""
        if getattr(r, "status_code", 0) != 200:
            raise SearchFailed(f"検索 '{word}': HTTP {r.status_code}")
        if "/sch_thr_thread/" not in effective and "スレッド検索結果" not in r.text:
            raise SearchFailed(f"検索 '{word}': 結果ページ以外が返却")
        return parse_search_results(r.text)

    # -- crawling ------------------------------------------------------
    def resolve_ttgid(self, tid, bid):
        url = (f"{HOST}/thr_res/acode={ACODE}/ctgid={CTGID}/bid={bid}"
               f"/tid={tid}/p=1/")
        r = self.request("GET", url, redirects=False, kind="crawl")
        loc = (getattr(r, "headers", {}) or {}).get("Location", "") or ""
        m = re.search(r"ttgid=(\d+)", loc)
        return m.group(1) if m else None

    def fetch_page(self, tid, bid, ttgid, page):
        parts = [f"{HOST}/thr_res/acode={ACODE}/ctgid={CTGID}",
                 f"bid={bid}", f"tid={tid}"]
        if ttgid:
            parts.append(f"ttgid={ttgid}")
        parts.append(f"p={page}")
        url = "/".join(parts) + "/"
        r = self.request("GET", url, kind="crawl")
        if getattr(r, "status_code", 0) != 200:
            raise SearchFailed(f"tid {tid} p{page}: HTTP {r.status_code}")
        return parse_thread_page(r.text)


# -------------------------------------------------------------- discovery --
def upsert_thread(conn, t, now):
    conn.execute(
        "INSERT INTO bakusai_threads(thread_id, bid, title, url, replies,"
        " first_seen, checked_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(thread_id) DO UPDATE SET"
        "  title = excluded.title,"
        "  bid = COALESCE(NULLIF(excluded.bid, 0), bakusai_threads.bid),"
        "  replies = COALESCE(excluded.replies, bakusai_threads.replies),"
        "  checked_at = excluded.checked_at",
        (t["tid"], t.get("bid"), t["title"],
         f"{HOST}/thr_res/acode={ACODE}/ctgid={CTGID}/bid={t.get('bid')}"
         f"/tid={t['tid']}/", t.get("replies"), now, now))


def link_shop(conn, tid, shop, confidence, source, matched, now):
    conn.execute(
        "INSERT INTO bakusai_shop_map(thread_id, shop_id, shop_name,"
        " confidence, source, matched, updated_at) VALUES(?,?,?,?,?,?,?)"
        " ON CONFLICT(thread_id, shop_id) DO UPDATE SET"
        "  confidence = MAX(bakusai_shop_map.confidence, excluded.confidence),"
        "  source = excluded.source, matched = excluded.matched,"
        "  updated_at = excluded.updated_at",
        (tid, shop["shop_id"], shop["name"], confidence, source, matched,
         now))


def discover(conn, client, shops, boards=None, search=True, now=None,
             log=print):
    """Run latest-30 + area search discovery; returns a summary dict."""
    now = now or now_jst().strftime("%Y-%m-%d %H:%M:%S")
    summary = {"latest_threads": 0, "search_results": 0, "links": 0,
               "search_ok": 0, "search_failed": 0, "search_skipped": 0,
               "search_retry": 0, "budget_stopped": False}
    boards = list(boards or BOARDS)

    threads = []
    for bid in boards:
        try:
            rows = client.latest_threads(bid)
        except BudgetExhausted:
            summary["budget_stopped"] = True
            rows = []
        except SearchFailed as e:
            log(f"  board {bid} 取得失敗: {e}")
            rows = []
        for t in rows:
            upsert_thread(conn, t, now)
        threads.extend(rows)
        log(f"  latest bid={bid}: {len(rows)} threads")
    summary["latest_threads"] = len({t["tid"] for t in threads})

    # exact/derived title matches on the latest-30 pool
    for t in threads:
        for shop in shops:
            hit = title_match(t["title"], shop["name"])
            if hit:
                link_shop(conn, t["tid"], shop, hit[0], "latest", hit[1], now)
                summary["links"] += 1
    conn.commit()

    if not search:
        conn.executemany(
            "INSERT INTO bakusai_shop_search(shop_id, word, status,"
            " n_results, checked_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(shop_id) DO UPDATE SET status = excluded.status,"
            " n_results = excluded.n_results, checked_at = excluded.checked_at",
            [(s["shop_id"], s["name"], "skipped", 0, now) for s in shops])
        conn.commit()
        summary["search_skipped"] = len(shops)
        summary["mode"] = "latest_only"
        return summary

    failures = 0
    for shop in shops:
        word = search_word(shop["name"])
        try:
            rows = client.search_threads(word)
            if not rows:
                retry = fallback_search_word(shop["name"])
                if retry:
                    summary["search_retry"] += 1
                    try:
                        rows = client.search_threads(retry)
                    except SearchFailed as e:
                        log(f"  検索再試行失敗 {shop['name']}: {e}")
                        rows = []
                    else:
                        if rows:
                            word = retry
        except BudgetExhausted:
            summary["budget_stopped"] = True
            break
        except SearchFailed as e:
            failures += 1
            summary["search_failed"] += 1
            conn.execute(
                "INSERT INTO bakusai_shop_search(shop_id, word, status,"
                " n_results, checked_at) VALUES(?,?,?,?,?)"
                " ON CONFLICT(shop_id) DO UPDATE SET status = excluded.status,"
                " n_results = excluded.n_results,"
                " checked_at = excluded.checked_at",
                (shop["shop_id"], word, "failed", 0, now))
            conn.commit()
            log(f"  検索失敗 {shop['name']}: {e}")
            continue
        summary["search_ok"] += 1
        summary["search_results"] += len(rows)
        conn.execute(
            "INSERT INTO bakusai_shop_search(shop_id, word, status,"
            " n_results, checked_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(shop_id) DO UPDATE SET status = excluded.status,"
            " n_results = excluded.n_results, checked_at = excluded.checked_at",
            (shop["shop_id"], word, "ok", len(rows), now))
        for t in rows:
            if t.get("bid"):
                upsert_thread(conn, t, now)
            # A search hit can name several shops; link every strong
            # title match so thread<->shop stays many-to-many.
            for other in shops:
                hit = title_match(t["title"], other["name"])
                if hit and hit[0] >= MIN_ALERT_CONFIDENCE:
                    link_shop(conn, t["tid"], other, hit[0], "search",
                              hit[1], now)
                    summary["links"] += 1
        conn.commit()

    if summary["budget_stopped"]:
        summary["mode"] = "budget_stopped"
    elif failures and failures == len(shops):
        summary["mode"] = "fallback_latest_only"
    elif failures:
        summary["mode"] = "partial_search"
    else:
        summary["mode"] = "search"
    return summary


def linked_shops(conn, min_confidence=MIN_ALERT_CONFIDENCE):
    """[(shop_id, shop_name, thread_count)] from strong title links."""
    rows = conn.execute(
        "SELECT shop_id, MAX(shop_name), COUNT(*) FROM bakusai_shop_map"
        " WHERE confidence >= ? GROUP BY shop_id ORDER BY COUNT(*) DESC",
        (min_confidence,)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def write_deep_shops(conn, path, min_confidence=MIN_ALERT_CONFIDENCE,
                     now=None):
    shops = linked_shops(conn, min_confidence)
    data = {
        "generated_at": (now or now_jst()).strftime("%Y-%m-%d %H:%M:%S"),
        "source": "bakusai discovery (latest-30 + area search)",
        "note": "スレッドが確認できた店のみ。口コミ順位などで埋めない。",
        "shops": [{"shop_id": sid, "name": name, "threads": n,
                   "confidence": min_confidence} for sid, name, n in shops],
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2)
                          + "\n", encoding="utf-8")
    return data


# ------------------------------------------------------------------ crawl --
def load_casts(estama_db, shop_ids):
    """{shop_id: [(norm_name, display_name)]} from estama snapshots."""
    if not shop_ids or not Path(estama_db).exists():
        return {}
    con = sqlite3.connect(f"file:{estama_db}?mode=ro", uri=True)
    try:
        marks = ",".join("?" * len(shop_ids))
        rows = con.execute(
            f"SELECT DISTINCT shop_id, cast_name FROM snapshots"
            f" WHERE shop_id IN ({marks}) AND cast_name IS NOT NULL"
            f" AND cast_name <> ''", tuple(shop_ids)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    out = {}
    for shop_id, name in rows:
        n = norm(name)
        if len(n) >= 2:
            out.setdefault(shop_id, []).append((n, name))
    return out


def masked_candidates(text, names):
    """Names matching a decoy token (one or more masked chars).

    Masked '⚪︎貞' can only narrow the dictionary down; it is stored
    as a candidate and never attributed to a shop/alert automatically.
    """
    cleaned = re.sub("[\uFE0E\uFE0F\uFFFD]", "", text)
    hits = []
    for tok in re.findall(r"[^\s、。,.!?！？「」『』]{2,12}", cleaned):
        if not MASK_RE.search(tok):
            continue
        for normed, name in names:
            if len(normed) != len(tok):
                continue
            if all(t in MASK_CHARS or t == m for t, m in zip(tok, normed)):
                hits.append(name)
    return list(dict.fromkeys(hits))


def _insert_posts(conn, tid, posts, now):
    added = 0
    for p in posts:
        cur = conn.execute(
            "INSERT OR IGNORE INTO bakusai_posts(thread_id, post_no,"
            " posted_at, body, reply_to, fetched_at) VALUES(?,?,?,?,?,?)",
            (tid, p["post_no"], p["posted_at"], p["body"], p["reply_to"],
             now))
        added += cur.rowcount
    return added


def _annotate_mentions(conn, tid, shop_ids, casts, now):
    names = []
    for sid in shop_ids:
        for normed, name in casts.get(sid, []):
            names.append((sid, normed, name))
    if not names:
        return 0
    rows = conn.execute(
        "SELECT post_no, body FROM bakusai_posts WHERE thread_id = ?", (tid,)
    ).fetchall()
    added = 0
    for r in rows:
        nb = norm(r["body"])
        if not nb:
            continue
        for sid, normed, name in names:
            if normed in nb:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO bakusai_mentions(thread_id,"
                    " post_no, cast_name, match_type, shop_id)"
                    " VALUES(?,?,?,?,?)",
                    (tid, r["post_no"], name, "exact", sid))
                added += cur.rowcount
        for name in masked_candidates(r["body"],
                                      [(n, nm) for _, n, nm in names]):
            for sid, normed, nm in names:
                if nm != name:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO bakusai_mentions(thread_id,"
                    " post_no, cast_name, match_type, shop_id)"
                    " VALUES(?,?,?,?,?)",
                    (tid, r["post_no"], name, "candidate", sid))
                added += cur.rowcount
                break
    return added


def _get_state(conn, key):
    row = conn.execute("SELECT value FROM bakusai_state WHERE key = ?",
                       (key,)).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def _set_state(conn, key, value):
    conn.execute("INSERT INTO bakusai_state(key, value) VALUES(?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))
    conn.commit()


def sync_thread(conn, client, thread, casts, now, max_pages=None, log=print):
    """Bring one thread up to date; resumable via bakusai_state."""
    tid, bid = thread["thread_id"], thread["bid"]
    ttgid = thread["ttgid"] or client.resolve_ttgid(tid, bid)
    if ttgid:
        conn.execute("UPDATE bakusai_threads SET ttgid = ? WHERE thread_id = ?",
                     (ttgid, tid))
        conn.commit()
    state_key = f"sync:{tid}"
    state = _get_state(conn, state_key) or {}
    last = conn.execute(
        "SELECT COALESCE(MAX(post_no), 0) FROM bakusai_posts"
        " WHERE thread_id = ?", (tid,)).fetchone()[0]

    if state.get("mode") == "backfill" or last == 0:
        page = state.get("next_page", 1)
        pages_done = 0
        while True:
            pg = client.fetch_page(tid, bid, ttgid, page)
            added = _insert_posts(conn, tid, pg["posts"], now)
            conn.execute(
                "UPDATE bakusai_threads SET max_page = ?, last_post_at ="
                " COALESCE((SELECT MAX(posted_at) FROM bakusai_posts"
                "  WHERE thread_id = ?), last_post_at),"
                " first_post_no = COALESCE(first_post_no,"
                "  (SELECT MIN(post_no) FROM bakusai_posts"
                "  WHERE thread_id = ?)), checked_at = ?"
                " WHERE thread_id = ?",
                (pg["max_page"], tid, tid, now, tid))
            conn.commit()
            pages_done += 1
            log(f"    tid={tid} p{page}/{pg['max_page']} +{added} res")
            if page >= pg["max_page"]:
                _set_state(conn, state_key, {"mode": "inc"})
                break
            page += 1
            _set_state(conn, state_key, {"mode": "backfill",
                                         "next_page": page})
            if max_pages and pages_done >= max_pages:
                return pages_done
        _annotate_mentions(
            conn, tid, [r[0] for r in conn.execute(
                "SELECT shop_id FROM bakusai_shop_map WHERE thread_id = ?"
                " AND confidence >= ?", (tid, MIN_ALERT_CONFIDENCE))],
            casts, now)
        conn.commit()
        return pages_done

    max_page = thread["max_page"] or conn.execute(
        "SELECT max_page FROM bakusai_threads WHERE thread_id = ?",
        (tid,)).fetchone()[0]
    if not max_page:
        pg = client.fetch_page(tid, bid, ttgid, 1)
        max_page = pg["max_page"]
        _insert_posts(conn, tid, pg["posts"], now)
    page = max_page
    pages_done = 0
    while page >= 1:
        pg = client.fetch_page(tid, bid, ttgid, page)
        fresh = [p for p in pg["posts"] if p["post_no"] > last]
        added = _insert_posts(conn, tid, fresh, now)
        conn.execute(
            "UPDATE bakusai_threads SET max_page = ?, last_post_at ="
            " COALESCE((SELECT MAX(posted_at) FROM bakusai_posts"
            "  WHERE thread_id = ?), last_post_at), checked_at = ?"
            " WHERE thread_id = ?", (pg["max_page"], tid, now, tid))
        conn.commit()
        pages_done += 1
        if added:
            log(f"    tid={tid} p{page} +{added} res")
            _annotate_mentions(
                conn, tid, [r[0] for r in conn.execute(
                    "SELECT shop_id FROM bakusai_shop_map"
                    " WHERE thread_id = ? AND confidence >= ?",
                    (tid, MIN_ALERT_CONFIDENCE))], casts, now)
            conn.commit()
        if not added:
            # Stale stored max_page: the FIRST fetch of the run may land
            # on a page that now holds only old posts, with the fresh
            # ones on the newest page the parser just reported. After
            # that first check we are walking down, so a larger
            # max_page must not send us back up again.
            if (pages_done == 1 and pg["max_page"] > page
                    and (not max_pages or pages_done < max_pages)):
                page = pg["max_page"]
                continue
            break
        if page <= 1 or (max_pages and pages_done >= max_pages):
            break
        page -= 1
    return pages_done


def crawl(conn, client, deep_shop_ids, estama_db, now=None, max_pages=None,
          limit=None, log=print):
    """Crawl threads linked to a deep-tier shop (newest activity first)."""
    now = now or now_jst().strftime("%Y-%m-%d %H:%M:%S")
    casts = load_casts(estama_db, deep_shop_ids)
    if deep_shop_ids:
        marks = ",".join("?" * len(deep_shop_ids))
        threads = conn.execute(
            "SELECT DISTINCT t.* FROM bakusai_threads t"
            " JOIN bakusai_shop_map m ON m.thread_id = t.thread_id"
            f" WHERE m.confidence >= ? AND m.shop_id IN ({marks})"
            " ORDER BY COALESCE(t.last_post_at, '') DESC",
            (MIN_ALERT_CONFIDENCE, *deep_shop_ids)).fetchall()
    else:
        threads = []
    if limit:
        threads = threads[:limit]
    summary = {"threads": len(threads), "pages": 0, "stopped": None,
               "requests": 0}
    try:
        for t in threads:
            try:
                summary["pages"] += sync_thread(conn, client, t, casts, now,
                                                max_pages=max_pages, log=log)
            except SearchFailed as e:
                # One dead/renamed thread must not starve the rest of
                # the deep tier (a 404 used to abort the whole crawl).
                log(f"  tid={t['thread_id']} 取得失敗: {e}")
    except BudgetExhausted as e:
        summary["stopped"] = str(e)
        log(f"  停止: {e}（次回再開）")
    summary["requests"] = client.requests_made
    return summary


def backtrack(conn, thread_id, post_no, hops=MAX_ANCHOR_HOPS):
    """Anchor chain, newest first, at most `hops` posts."""
    chain, seen = [], set()
    cur = post_no
    while cur is not None and len(chain) < hops and cur not in seen:
        seen.add(cur)
        row = conn.execute(
            "SELECT post_no, posted_at, body, reply_to FROM bakusai_posts"
            " WHERE thread_id = ? AND post_no = ?", (thread_id, cur)
        ).fetchone()
        if not row:
            break
        chain.append(dict(row))
        cur = row["reply_to"]
    return chain


# -------------------------------------------------------------------- CLI --
def load_shops(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    shops = data.get("shops", []) if isinstance(data, dict) else data
    return [s for s in shops if s.get("shop_id") and s.get("name")]


def load_deep_ids(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {int(s["shop_id"]) for s in data.get("shops", [])
            if isinstance(s, dict) and s.get("shop_id")}


def cmd_status(args):
    conn = connect(args.db)

    def q(sql, *params):
        return conn.execute(sql, params).fetchone()[0]

    threads = q("SELECT COUNT(*) FROM bakusai_threads")
    posts = q("SELECT COUNT(*) FROM bakusai_posts")
    links = q("SELECT COUNT(*) FROM bakusai_shop_map")
    strong = q("SELECT COUNT(*) FROM bakusai_shop_map WHERE confidence >= ?",
               MIN_ALERT_CONFIDENCE)
    mentions = q("SELECT COUNT(*) FROM bakusai_mentions")
    candidates = q("SELECT COUNT(*) FROM bakusai_mentions"
                   " WHERE match_type = 'candidate'")
    today = now_jst().strftime("%Y-%m-%d")
    used = q("SELECT COALESCE(SUM(n), 0) FROM bakusai_requests"
             " WHERE day = ?", today)
    print(f"db: {args.db}")
    print(f"threads: {threads}")
    print(f"posts:   {posts}")
    print(f"links:   {links} (strong: {strong})")
    print(f"mentions:{mentions} (candidates: {candidates})")
    print(f"requests today: {used} / {args.budget}")
    for sid, name, n in linked_shops(conn)[:25]:
        print(f"  shop {sid} {name}: {n} threads")
    return 0


def cmd_discover(args):
    conn = connect(args.db)
    shops = load_shops(args.shops)
    boards = ([int(x) for x in args.boards.split(",")]
              if args.boards else list(BOARDS))
    client = Client(conn, budget=args.budget)
    log = print
    log(f"discover: {len(shops)} shops, boards={boards}, "
        f"search={'off' if args.no_search else 'on'}")
    summary = discover(conn, client, shops, boards=boards,
                       search=not args.no_search, now=now_jst().strftime(
                           "%Y-%m-%d %H:%M:%S"), log=log)
    log(f"結果: {summary}")
    if summary["budget_stopped"]:
        log("予算上限で停止: deep tier は更新しない（既存ファイルを維持）")
        return 1
    if summary["mode"] == "fallback_latest_only":
        log("検索が全件失敗: deep tier は更新しない（既存ファイルを維持）")
        return 1
    data = write_deep_shops(conn, args.deep_out)
    log(f"deep tier: {len(data['shops'])} 店 -> {args.deep_out}")
    return 0


def cmd_crawl(args):
    conn = connect(args.db)
    deep = load_deep_ids(args.deep)
    if not deep:
        print(f"{args.deep} が空です。先に discover を実行してください")
        return 1
    client = Client(conn, budget=args.budget)
    summary = crawl(conn, client, deep, args.estama_db,
                    max_pages=args.max_pages, limit=args.limit)
    print(f"crawl: {summary}")
    return 0


def cmd_backtrack(args):
    conn = connect(args.db)
    for row in backtrack(conn, args.thread, args.post, hops=args.hops):
        print(f"#{row['post_no']} {row['posted_at'] or '?':19}"
              f" ->{row['reply_to'] or '-':>5}  {row['body'][:80]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="bakusai.com crawler")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="create bakusai.db schema")
    p = sub.add_parser("status", help="print DB counts")
    p.add_argument("--budget", type=int, default=DAILY_BUDGET)

    p = sub.add_parser("discover", help="latest-30 + area search")
    p.add_argument("--shops", default=str(DEFAULT_SHOPS))
    p.add_argument("--deep-out", default=str(DEFAULT_DEEP))
    p.add_argument("--boards", default="", help="comma separated bid list")
    p.add_argument("--budget", type=int, default=DAILY_BUDGET)
    p.add_argument("--no-search", action="store_true",
                   help="skip area search (dictionary only)")

    p = sub.add_parser("crawl", help="crawl deep-tier threads")
    p.add_argument("--deep", default=str(DEFAULT_DEEP))
    p.add_argument("--estama-db", default=str(BASE / "estama.db"))
    p.add_argument("--budget", type=int, default=DAILY_BUDGET)
    p.add_argument("--max-pages", type=int, default=None,
                   help="pages per thread this run (default: all)")
    p.add_argument("--limit", type=int, default=None,
                   help="only the N most recently active threads")

    p = sub.add_parser("backtrack", help="print an anchor chain")
    p.add_argument("--thread", required=True)
    p.add_argument("--post", type=int, required=True)
    p.add_argument("--hops", type=int, default=MAX_ANCHOR_HOPS)

    args = ap.parse_args(argv)
    if args.cmd == "init":
        connect(args.db).close()
        print(f"initialized {args.db}")
        return 0
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "discover":
        return cmd_discover(args)
    if args.cmd == "crawl":
        return cmd_crawl(args)
    if args.cmd == "backtrack":
        return cmd_backtrack(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
