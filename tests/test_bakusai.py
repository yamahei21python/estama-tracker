"""Tests for bakusai.py: parsers, notation-variant matching, discovery
with search fallback, resumable budgeted crawl, mention candidates and
anchor backtracking. Network is replaced by a fake HTTP callable.

Run: python3.11 tests/test_bakusai.py
"""
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/estama-bakusai-test")
shutil.rmtree(SCRATCH, ignore_errors=True)
SCRATCH.mkdir(parents=True)
sys.path.insert(0, str(ROOT / "app"))
import bakusai as b  # noqa: E402
import evidence  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


# ------------------------------------------------------------- fixtures --
def thread_list(bid, rows):
    items = []
    for tid, title, replies in rows:
        items.append(f"""
<li style="line-height: 21.5px;">
<span class="thrNumber">1</span> <a href="/thr_res/acode=7/ctgid=136/bid={bid}/tid={tid}/tp=2/" class="adult_thr_title" title="{title}">x</a>
<span class="thrListContent"><span>6時間前</span>
<span class="comment_count_area newslis_count"><span class="thrlis_comment_space"><i></i><span class="comment_balloon newslis_count">レス数</span></span><span>{replies}</span></span></span>
</li>""")
    return ('<div id="thrListInner" class="x"><ul>' + "".join(items)
            + "</ul></div>")


def search_page(results):
    items = []
    for tid, bid, title in results:
        items.append(f"""
<li><div class="search_thr_title"><a href="/thr_res/acode=7/ctgid=136/bid={bid}/tid={tid}/">
<h2 id="tid-{tid}" class="seottl_h2 thr_status_icon" title="{title}">{title}</h2></a></div></li>""")
    return ('<title>『x』のスレッド検索結果｜爆サイ.com大阪版</title>'
            + "".join(items))


def thread_page(tid, bid, page, posts, max_page=1, total=None):
    arts = []
    for no, ts, body, reply in posts:
        anchor = (f'<span class="resOverlay"><a href="/thr_res_show/x/rrid={reply}/">'
                  f"&gt;&gt;{reply}</a></span><br />") if reply else ""
        arts.append(
            f'<div class="article res_list_article " itemscope itemtype="x" '
            f'id="res{no}"><dt><div class="res_meta_wrap"><span class="resnumb">'
            f'<a href="#pos_box">#{no}</a></span><span itemprop="commentTime">'
            f'{ts}</span></div></dt><dd class="body "><div class="resbody" '
            f'itemprop="commentText">{anchor}{body}</div><br>'
            f'<div class="clear"></div></dd><div class="name_goodbad_box">'
            f'<dd class="name">[匿名さん]</dd></div>')
    pager = "".join(
        f'<span class="paging_numberlink"><a href="/thr_res/acode=7/ctgid=136'
        f'/bid={bid}/tid={tid}/p={p}/tp=1/">{p}</a></span>'
        for p in range(1, max_page + 1))
    total = total or sum(1 for _ in posts)
    return (f"<title>t | {total}レス｜爆サイ</title>" + "".join(arts)
            + f'<div class="paging">{pager}</div>')


class FakeHTTP:
    def __init__(self, boards=None, search=None, pages=None,
                 search_status=200, fail_after=None):
        self.boards = boards or {}
        self.search = search or {}
        self.pages = pages or {}
        self.search_status = search_status
        self.calls = []
        self.fail_after = fail_after

    def __call__(self, method, url, data, headers, redirects):
        self.calls.append((method, url))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise AssertionError("budget should have stopped before this call")
        if "/thr_tl/" in url:
            bid = int(url.split("bid=")[1].split("/")[0])
            return SimpleNamespace(status_code=200, headers={}, url=url,
                                   text=self.boards.get(bid, thread_list(bid, [])))
        if url.endswith("/sch_result/"):
            if self.search_status != 200:
                return SimpleNamespace(status_code=self.search_status,
                                       headers={}, url=url, text="error")
            word = (data or {}).get("word", "")
            rows = self.search.get(word, [])
            return SimpleNamespace(
                status_code=200, headers={},
                url="https://bakusai.com/sch_thr_thread/acode=7/x/",
                text=search_page(rows))
        if "/thr_res/" in url:
            if not redirects and "ttgid=" not in url:
                loc = url.split("/p=")[0] + "/ttgid=504/p=2/"
                return SimpleNamespace(status_code=302, url=url, text="",
                                       headers={"Location": loc})
            tid = int(url.split("tid=")[1].split("/")[0])
            page = int(url.split("/p=")[1].split("/")[0])
            text = self.pages.get((tid, page))
            if text is None:
                return SimpleNamespace(status_code=404, headers={}, url=url,
                                       text="")
            return SimpleNamespace(status_code=200, headers={}, url=url,
                                   text=text)
        raise AssertionError(f"unexpected url {url}")


SHOPS = [
    {"shop_id": 1, "name": "ぽっちゃりエステ　つつみ", "area": "谷町",
     "source_url": "https://estama.jp/shop/1/schedule/"},
    {"shop_id": 2, "name": "当たりスパ", "area": "堺筋本町",
     "source_url": "https://estama.jp/shop/2/schedule/"},
    {"shop_id": 3, "name": "梅田高級メンズエステ", "area": "梅田",
     "source_url": "https://estama.jp/shop/3/schedule/"},
]


def fresh_db(name="bakusai.db"):
    return b.connect(SCRATCH / name)


# ------------------------------------------------------------- parsers --
check("parse_thread_list: rows", [
    (r["tid"], r["title"], r["replies"])
    for r in b.parse_thread_list(thread_list(6066, [
        ("1001", "谷町 ぽっちゃりエステつつみ ⑱", "933"),
        ("1002", "当たりスパ ⑳", "12")]), 6066)] == [
    ("1001", "谷町 ぽっちゃりエステつつみ ⑱", 933),
    ("1002", "当たりスパ ⑳", 12)])

got = b.parse_search_results(search_page([("1001", 6066, "谷町 つつみ"),
                                          ("1002", 1714, "当たりスパ")]))
check("parse_search_results: tid/bid/title", got == [
    {"tid": "1001", "bid": 6066, "title": "谷町 つつみ"},
    {"tid": "1002", "bid": 1714, "title": "当たりスパ"}])

pg = b.parse_thread_page(thread_page(1001, 6066, 2, [
    (638, "2026/10/10 17:19", "客なのに、この店に残るメリットがない", 637),
    (639, "2026/10/10 18:01", ">>638 それは言えてる", None)], max_page=3,
    total=737))
check("parse_thread_page: posts + timestamps", [
    (p["post_no"], p["posted_at"]) for p in pg["posts"]] == [
    (638, "2026-10-10 17:19:00"), (639, "2026-10-10 18:01:00")])
check("parse_thread_page: reply_to anchor", [p["reply_to"] for p in pg["posts"]]
      == [637, 638])
check("parse_thread_page: max_page/total", (pg["max_page"], pg["total"])
      == (3, 737))
long_body = "あ" * 800
check("parse_thread_page: body limited to 500",
      len(b.parse_thread_page(thread_page(1, 1, 1, [
          (1, "2026/10/10 10:00", long_body, None)]))["posts"][0]["body"])
      == b.BODY_LIMIT)

# quoted posts: real markup wraps the quote in an escaped data-tipso span;
# only the writer's own text must survive in the body.
quoted = (
    '<div class="article res_list_article " id="res900">'
    '<dt><div class="res_meta_wrap"><span class="resnumb">'
    '<a href="#pos_box">#900</a></span><span itemprop="commentTime">'
    '2026/10/10 12:00</span></div></dt><dd class="body ">'
    '<div class="resbody" itemprop="commentText">'
    "<span data-tipso='&lt;div class=&quot;res_body&quot;&gt;"
    "引用元の本文&lt;/div&gt;&lt;div class=&quot;name&quot;&gt;"
    "[&lt;span&gt;匿名さん&lt;/span&gt;]&lt;/div&gt;' class=\"resOverlay\">"
    '<a href="/thr_res_show/acode=7/ctgid=136/bid=1714/tid=900/rrid=638/"'
    ' target="_blank">&gt;&gt;638</a></span><br />\n'
    '本人の発言だけ残る</div><br><div class="clear"></div></dd>'
    '<div class="name_goodbad_box"><dd class="name">[匿名さん]</dd>'
    '</div></div>'
    '<div class="paging"><span class="paging_numberlink">'
    '<a href="/thr_res/acode=7/ctgid=136/bid=1714/tid=900/p=1/tp=1/">1'
    '</a></span></div>')
qp = b.parse_thread_page(quoted)["posts"]
check("parse_thread_page: quote overlay dropped",
      [p["body"] for p in qp] == ["本人の発言だけ残る"])
check("parse_thread_page: quote anchor kept", qp[0]["reply_to"] == 638)

# -------------------------------------------------------- title matching --
check("title_match: exact full name", b.title_match(
    "谷町 ぽっちゃりエステつつみ ⑱", "ぽっちゃりエステ　つつみ")[0] == 1.0)
check("title_match: parenthesized variant",
      b.title_match("Cheri梅田/京橋（ルーム） Part2",
                    "Cheri（派遣・出張専門店）") == (0.9, "cheri"))
check("title_match: generic/area only -> no link",
      b.title_match("難波 メンエス 高級店", "梅田高級メンズエステ") is None)
check("title_match: area+type variant alone -> no link", (
    b.title_match("梅田メンズエステ SUPER OTONA GIRLS",
                  "梅田メンズエステ SUPER HAPPY GIRLS") or (0,))[0] < 0.8)
check("title_match: unrelated -> no link",
      b.title_match("当たりスパ ⑳", "ぽっちゃりエステ　つつみ") is None)
check("title_match: ascii word boundary (embedded)",
      (b.title_match("CharmeBellfloweR シャルムベルフラワー", "Flower")
       or (0,))[0] < 0.8)
check("title_match: ascii word boundary (delimited)",
      b.title_match("日本橋 FLOWER", "Flower") == (1.0, "flower"))
check("title_match: short cjk name needs primary title",
      b.title_match("【テレ東】ダイヤのA", "ダイヤ") is None)
check("title_match: short cjk name on series title",
      b.title_match("ダイヤ ⑦", "ダイヤ") == (1.0, "ダイヤ"))
check("title_match: short cjk name with subtitle",
      b.title_match("俺の家の話 第3話", "俺の家") is None)
check("title_match: variant still needs boundary",
      (b.title_match("日本橋 belle femme ～ベルファーム～",
                     "Femme Fatale（ファムファタール）") or (0,))[0] < 0.8)

# --------------------------------------------------------- search words --
check("search_word: parenthesized note dropped",
      b.search_word("Cheri（派遣・出張専門店）") == "Cheri")
check("search_word: inner space kept",
      b.search_word("ぽっちゃりエステ　つつみ") == "ぽっちゃりエステ つつみ")
check("search_word: area segment dropped",
      b.search_word("京橋｜MOTEJO🩷教育されたモテる女達👄マンション型")
      == "MOTEJO 教育されたモテる女達 マンション型")
check("search_word: note and descriptor kept",
      b.search_word("belle femme（ベルファーム）　マット型コンセプト")
      == "belle femme マット型コンセプト")
check("search_word: trailing 店 trimmed",
      b.search_word("ぽっちゃりエステ　つつみ店") == "ぽっちゃりエステ つつみ")

# ------------------------------------------------------------- discovery --
boards = {
    6066: thread_list(6066, [("1001", "谷町 ぽっちゃりエステつつみ ⑱", 933),
                             ("1002", "当たりスパ ⑳", 12)]),
    2383: thread_list(2383, [("1003", "梅田 スパ 高級", 5)]),
}
search = {
    b.search_word("ぽっちゃりエステ　つつみ"): [
        ("1001", 6066, "谷町 ぽっちゃりエステつつみ ⑱")],
    b.search_word("当たりスパ"): [("1002", 6066, "当たりスパ ⑳"),
               ("2001", 6067, "当たりスパ と ぽっちゃりエステつつみ")],
    b.search_word("梅田高級メンズエステ"): [],
}
conn = fresh_db()
client = b.Client(conn, http=FakeHTTP(boards=boards, search=search),
                  min_delay=0, max_delay=0, sleep=lambda s: None,
                  log=lambda *a: None)
summary = b.discover(conn, client, SHOPS, boards=[6066, 2383], log=lambda *a: None)
check("discover: mode search", summary["mode"] == "search")
check("discover: threads stored", conn.execute(
    "SELECT COUNT(*) FROM bakusai_threads").fetchone()[0] == 4)
maps = conn.execute(
    "SELECT thread_id, shop_id, confidence, source FROM bakusai_shop_map"
    " ORDER BY thread_id, shop_id").fetchall()
check("discover: many-to-many thread<->shop",
      [tuple(r) for r in maps] == [
          ("1001", 1, 1.0, "search"), ("1002", 2, 1.0, "search"),
          ("2001", 1, 1.0, "search"), ("2001", 2, 1.0, "search")])
check("discover: generic thread 1003 not linked",
      conn.execute("SELECT COUNT(*) FROM bakusai_shop_map WHERE thread_id = '1003'"
                   ).fetchone()[0] == 0)
check("discover: shop_search ok rows", conn.execute(
    "SELECT COUNT(*) FROM bakusai_shop_search WHERE status = 'ok'").fetchone()[0]
    == 3)
deep_path = SCRATCH / "deep_shops.json"
data = b.write_deep_shops(conn, deep_path)
check("deep_shops: only linked shops",
      sorted(s["shop_id"] for s in data["shops"]) == [1, 2])
sys.path.insert(0, str(ROOT / "app"))
import estama_tracker as t  # noqa: E402
check("deep_shops: loadable by tracker",
      t.load_deep_shop_ids(deep_path) == {1, 2})

# search failure -> fallback to latest + dictionary
conn2 = fresh_db("fallback.db")
client2 = b.Client(conn2, http=FakeHTTP(boards=boards, search_status=503),
                   min_delay=0, max_delay=0, sleep=lambda s: None,
                   log=lambda *a: None)
s2 = b.discover(conn2, client2, SHOPS, boards=[6066], log=lambda *a: None)
check("fallback: mode", s2["mode"] == "fallback_latest_only")
check("fallback: shop statuses failed", conn2.execute(
    "SELECT COUNT(*) FROM bakusai_shop_search WHERE status = 'failed'"
).fetchone()[0] == 3)
check("fallback: latest links still stored", conn2.execute(
    "SELECT COUNT(*) FROM bakusai_shop_map").fetchone()[0] == 2)
check("fallback: deep tier still built",
      sorted(s["shop_id"] for s in b.write_deep_shops(
          conn2, SCRATCH / "deep2.json")["shops"]) == [1, 2])

# search page that is not a result page -> treated as failure
conn3 = fresh_db("fallback2.db")
client3 = b.Client(conn3, http=FakeHTTP(boards=boards, search={}),
                   min_delay=0, max_delay=0, sleep=lambda s: None,
                   log=lambda *a: None)
client3._http = lambda m, u, d, h, r: SimpleNamespace(
    status_code=200, headers={}, url="https://bakusai.com/sch_in_thr_bbs0/x/",
    text="<title>検索</title>")
s3 = b.discover(conn3, client3, SHOPS, boards=[6066], log=lambda *a: None)
check("fallback: non-result page detected", s3["mode"] == "fallback_latest_only")

# zero hits for the full name -> retry with the leading brand segment
check("fallback_search_word: brand segment",
      b.fallback_search_word("MOTEJO 教育されたモテる女達 マンション型")
      == "MOTEJO")
check("fallback_search_word: single segment -> none",
      b.fallback_search_word("SARISARI") == "")
conn_r = fresh_db("retry.db")
retry_shop = [{"shop_id": 9, "name": "MOTEJO 教育されたモテる女達 マンション型",
               "area": "京橋", "source_url": ""}]
search_r = {b.search_word(retry_shop[0]["name"]): [],
            "MOTEJO": [("3001", 6066,
                        "MOTEJO 教育されたモテる女達 マンション型 ②")]}
client_r = b.Client(conn_r, http=FakeHTTP(
    boards={6066: thread_list(6066, [])}, search=search_r),
    min_delay=0, max_delay=0, sleep=lambda s: None, log=lambda *a: None)
sr = b.discover(conn_r, client_r, retry_shop, boards=[6066],
                log=lambda *a: None)
check("retry: counted", sr["search_retry"] == 1)
check("retry: linked via retry hits", conn_r.execute(
    "SELECT COUNT(*) FROM bakusai_shop_map WHERE thread_id = '3001'"
    " AND shop_id = 9").fetchone()[0] == 1)
check("retry: word recorded", conn_r.execute(
    "SELECT word FROM bakusai_shop_search WHERE shop_id = 9"
).fetchone()[0] == "MOTEJO")

# --------------------------------------------------------------- crawl --
pages = {
    (1001, 1): thread_page(1001, 6066, 1, [
        (638, "2026/10/10 17:19", "吉川さんめちゃ美人やった", None),
        (639, "2026/10/10 18:01", ">>638 スタイルは良いな", 638)], max_page=2),
    (1001, 2): thread_page(1001, 6066, 2, [
        (687, "2026/10/18 16:26", "⚪︎貞、唾垂らす女", 686)], max_page=2),
}
estama = SCRATCH / "estama.db"
econ = sqlite3.connect(estama)
econ.execute("CREATE TABLE snapshots (shop_id INTEGER, cast_name TEXT)")
econ.executemany("INSERT INTO snapshots VALUES (?,?)",
                 [(1, "吉川"), (1, "美貞")])
econ.commit()
econ.close()

conn = b.connect(SCRATCH / "crawl.db")
b.discover(conn, b.Client(conn, http=FakeHTTP(boards=boards, search=search),
                          min_delay=0, max_delay=0, sleep=lambda s: None,
                          log=lambda *a: None),
           SHOPS, boards=[6066], search=False, log=lambda *a: None)
client = b.Client(conn, http=FakeHTTP(pages=pages), min_delay=0,
                  max_delay=0, sleep=lambda s: None, log=lambda *a: None)
s = b.crawl(conn, client, {1}, estama, log=lambda *a: None)
check("crawl: pages fetched", s["pages"] == 2)
check("crawl: 3 posts stored", conn.execute(
    "SELECT COUNT(*) FROM bakusai_posts").fetchone()[0] == 3)
check("crawl: body/ts/anchor stored", conn.execute(
    "SELECT posted_at, reply_to FROM bakusai_posts WHERE thread_id='1001'"
    " AND post_no=639").fetchone()[:] == ("2026-10-10 18:01:00", 638))
check("crawl: ttgid resolved and stored", conn.execute(
    "SELECT ttgid FROM bakusai_threads WHERE thread_id='1001'"
).fetchone()[0] == "504")
check("crawl: mention exact", conn.execute(
    "SELECT COUNT(*) FROM bakusai_mentions WHERE match_type='exact'"
).fetchone()[0] == 1)
check("crawl: decoy stored as candidate", [
    r[0] for r in conn.execute(
        "SELECT cast_name FROM bakusai_mentions"
        " WHERE match_type='candidate'")] == ["美貞"])

# idempotent rerun
client_r = b.Client(conn, http=FakeHTTP(pages=pages), min_delay=0,
                    max_delay=0, sleep=lambda s: None, log=lambda *a: None)
b.crawl(conn, client_r, {1}, estama, log=lambda *a: None)
check("crawl: rerun adds no duplicate posts", conn.execute(
    "SELECT COUNT(*) FROM bakusai_posts").fetchone()[0] == 3)
check("crawl: rerun adds no duplicate mentions", conn.execute(
    "SELECT COUNT(*) FROM bakusai_mentions").fetchone()[0] == 2)

# limit: only the newest N threads (used for small live validation runs)
conn_l = b.connect(SCRATCH / "limit.db")
for tid_n, last in (("1001", "2026-10-19 09:00:00"),
                    ("1002", "2026-10-20 11:00:00")):
    conn_l.execute(
        "INSERT INTO bakusai_threads(thread_id, bid, title, ttgid,"
        " last_post_at) VALUES(?,?,?,?,?)",
        (tid_n, 6066, "t", "504", last))
    b.link_shop(conn_l, tid_n, SHOPS[0], 1.0, "latest", "x", last)
conn_l.commit()
pages_l = {(1002, 1): thread_page(1002, 6066, 1, [
    (10, "2026/10/20 10:00", "吉川", None)], max_page=1)}
client_l = b.Client(conn_l, http=FakeHTTP(pages=pages_l), min_delay=0,
                    max_delay=0, sleep=lambda s: None, log=lambda *a: None)
sl = b.crawl(conn_l, client_l, {1}, estama, limit=1, log=lambda *a: None)
check("crawl: limit takes newest thread only", sl["threads"] == 1)
check("crawl: limit skipped older thread", conn_l.execute(
    "SELECT COUNT(*) FROM bakusai_posts WHERE thread_id='1001'"
).fetchone()[0] == 0)
check("crawl: limit fetched newest thread", conn_l.execute(
    "SELECT COUNT(*) FROM bakusai_posts WHERE thread_id='1002'"
).fetchone()[0] == 1)

# incremental: a new post appears on the last page
pages2 = dict(pages)
pages2[(1001, 2)] = thread_page(1001, 6066, 2, [
    (687, "2026/10/18 16:26", "⚪︎貞、唾垂らす女", 686),
    (688, "2026/10/19 09:00", "吉川また入ってた", 687)], max_page=2)
client_i = b.Client(conn, http=FakeHTTP(pages=pages2), min_delay=0,
                    max_delay=0, sleep=lambda s: None, log=lambda *a: None)
b.crawl(conn, client_i, {1}, estama, log=lambda *a: None)
check("crawl: incremental picks up new post", conn.execute(
    "SELECT COUNT(*) FROM bakusai_posts").fetchone()[0] == 4)
check("crawl: incremental mention added", conn.execute(
    "SELECT COUNT(*) FROM bakusai_mentions WHERE match_type='exact'"
).fetchone()[0] == 2)

# incremental: the thread gained a page since the last run. The stored
# max_page is stale, so the first fetch has no fresh posts; the crawl
# must jump to the newest page instead of stopping for the day.
conn_p = b.connect(SCRATCH / "pageturn.db")
b.upsert_thread(conn_p, {"tid": "1003", "bid": 6066, "title": "t1003",
                         "replies": 52}, "2026-10-19 09:00:00")
b.link_shop(conn_p, "1003", SHOPS[0], 1.0, "latest", "x",
            "2026-10-19 09:00:00")
conn_p.execute("UPDATE bakusai_threads SET max_page = 1 WHERE"
               " thread_id = '1003'")
conn_p.execute(
    "INSERT INTO bakusai_posts(thread_id, post_no, posted_at, body,"
    " fetched_at) VALUES('1003', 50, '2026-10-19 09:00:00', '吉川',"
    " '2026-10-19 09:00:00')")
conn_p.commit()
pages_p = {
    (1003, 1): thread_page(1003, 6066, 1, [
        (50, "2026/10/19 09:00", "吉川", None)], max_page=2),
    (1003, 2): thread_page(1003, 6066, 2, [
        (51, "2026/10/20 10:00", "新着", None),
        (52, "2026/10/20 11:00", "さらに新着", None)], max_page=2),
}
client_p = b.Client(conn_p, http=FakeHTTP(pages=pages_p), min_delay=0,
                    max_delay=0, sleep=lambda s: None, log=lambda *a: None)
b.crawl(conn_p, client_p, {1}, estama, log=lambda *a: None)
check("crawl: stale max_page jumps to the newest page", [
    r[0] for r in conn_p.execute(
        "SELECT post_no FROM bakusai_posts WHERE thread_id='1003'"
        " ORDER BY post_no")] == [50, 51, 52])

# evidence end-to-end on the crawled DB
ev = evidence.bakusai_evidence(conn, 1, "吉川")
check("evidence: reads crawled mentions", ev and ev["n"] == 2
      and ev["last"] == "2026-10-19" and "吉川" in ev["quote"])

# resumable budget: 1 request for ttgid + 1 page, then stop
conn_b = fresh_db("budget.db")
now = "2026-10-29 12:00:00"
b.upsert_thread(conn_b, {"tid": "1001", "bid": 6066,
                         "title": "谷町 ぽっちゃりエステつつみ ⑱",
                         "replies": 2}, now)
b.link_shop(conn_b, "1001", SHOPS[0], 1.0, "latest",
            "ぽっちゃりエステつつみ", now)
conn_b.commit()
client_b = b.Client(conn_b, http=FakeHTTP(pages=pages), budget=2,
                    min_delay=0, max_delay=0, sleep=lambda s: None,
                    log=lambda *a: None)
sb = b.crawl(conn_b, client_b, {1}, estama, log=lambda *a: None)
check("budget: stopped at cap", sb["stopped"] is not None)
check("budget: partial posts kept", conn_b.execute(
    "SELECT COUNT(*) FROM bakusai_posts").fetchone()[0] == 2)
check("budget: cursor saved", json.loads(conn_b.execute(
    "SELECT value FROM bakusai_state WHERE key='sync:1001'"
).fetchone()[0]) == {"mode": "backfill", "next_page": 2})
client_b2 = b.Client(conn_b, http=FakeHTTP(pages=pages), budget=10,
                     min_delay=0, max_delay=0, sleep=lambda s: None,
                     log=lambda *a: None)
b.crawl(conn_b, client_b2, {1}, estama, log=lambda *a: None)
check("budget: resume completes backfill", conn_b.execute(
    "SELECT COUNT(*) FROM bakusai_posts").fetchone()[0] == 3)

# ------------------------------------------------------- backtrack/decoy --
for no in range(700, 712):
    conn.execute(
        "INSERT INTO bakusai_posts(thread_id, post_no, posted_at, body,"
        " reply_to, fetched_at) VALUES('1001', ?, '2026-10-20 10:00:00', ?,"
        " ?, '2026-10-20 10:00:00')",
        (no, f"body {no}", no - 1 if no > 700 else None))
conn.commit()
chain = b.backtrack(conn, "1001", 711)
check("backtrack: max 10 hops", len(chain) == b.MAX_ANCHOR_HOPS)
check("backtrack: newest first", [c["post_no"] for c in chain] == list(
    range(711, 701, -1)))

check("masked_candidates: wildcard suffix",
      b.masked_candidates("垂らして貰えるだけありがたいと思え、⚪︎貞",
                          [(b.norm("美貞"), "美貞"),
                           (b.norm("美咲"), "美咲")]) == ["美貞"])

conn.close()
conn_b.close()
conn2.close()

if fails:
    print(f"\nRESULT: {len(fails)} FAILED")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("\nRESULT: ALL PASS")
