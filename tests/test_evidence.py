"""Tests for evidence.py: review aggregation, slot-loss timing, bakusai
attribution, and graceful degradation when a source DB is missing.

Builds three tiny throwaway DBs (estama / review / bakusai) in /tmp.
Run: python3.11 tests/test_evidence.py
"""
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/estama-evidence-test")
shutil.rmtree(SCRATCH, ignore_errors=True)
SCRATCH.mkdir(parents=True)

sys.path.insert(0, str(ROOT / "app"))
import evidence as ev  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


# ---------------------------------------------------------------- estama db
est_db = SCRATCH / "estama.db"
con = sqlite3.connect(est_db)
con.executescript((ROOT / "app" / "schema.sql").read_text())
con.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL,"
            " 'https://estama.jp/shop/1/schedule/')")
con.execute("INSERT INTO shops VALUES (2, '店B', '難波', NULL, NULL)")
for d in ("2026-10-27", "2026-10-28"):
    con.execute(
        "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_norm,"
        " target_date, fetched_at, fetched_time)"
        " VALUES (1, '白石 さち', 777, 'available', ?, ?, '10:00')",
        (d, f"{d} 10:00"))
con.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_norm,"
    " target_date, fetched_at, fetched_time)"
    " VALUES (2, '他の子', 888, 'available', '2026-10-28',"
    " '2026-10-28 10:00', '10:00')")
slot = ("INSERT INTO slot_events (shop_id, cast_key, cast_id, cast_name,"
        " target_date, slot_time, slot_minutes, from_mark, to_mark,"
        " observed_at, checkpoint) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
con.executemany(slot, [
    (1, "777", 777, "白石 さち", "2026-10-28", "18:30", 1110,
     None, "○", "2026-10-28 09:00", "09:00"),
    (1, "777", 777, "白石 さち", "2026-10-28", "19:00", 1140,
     "○", "×", "2026-10-28 11:00", "11:00"),
    (1, "777", 777, "白石 さち", "2026-10-28", "19:00", 1140,
     "×", "○", "2026-10-28 10:30", "10:30"),   # return: not a loss
    (1, "777", 777, "白石 さち", "2026-10-28", "20:00", 1200,
     "TEL", "×", "2026-10-28 12:00", "12:00"),
    (1, "777", 777, "白石 さち", "2026-10-27", "19:00", 1140,
     "○", "×", "2026-10-27 15:00", "15:00"),   # other day
    (2, "888", 888, "他の子", "2026-10-28", "19:00", 1140,
     "○", "×", "2026-10-28 10:00", "10:00"),   # other cast
])
con.commit()

# ---------------------------------------------------------------- review db
rev_db = SCRATCH / "review.db"
r = sqlite3.connect(rev_db)
r.executescript("""
CREATE TABLE shops (id INTEGER PRIMARY KEY, name TEXT, hp_url TEXT);
CREATE TABLE shop_aliases (menes_id INTEGER, estama_name TEXT);
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER,
    cast_name TEXT, score INTEGER, recommend_stars INTEGER,
    body_preview TEXT, scraped_at TEXT);
""")
r.execute("INSERT INTO shops VALUES (41, '店A(メンエス)', 'https://men.example/a')")
r.execute("INSERT INTO shop_aliases VALUES (41, '店A')")
r.executemany(
    "INSERT INTO reviews (shop_id, cast_name, score, recommend_stars,"
    " body_preview, scraped_at) VALUES (?,?,?,?,?,?)",
    [(41, "白石 さちさん", 80, 4, "スタイルが良くて美人でした", "2026-06-01T10:00"),
     (41, "白石さち (40代前半)さん", 90, 5, "揉みが強くて気持ち良かった", "2026-06-16T09:00"),
     (41, "白石　さち", 70, 3, "美人で話が面白い。スタイルも良い", "2026-05-20T09:00"),
     (41, "他の子さん", 60, 3, "可もなく不可もなく", "2026-06-10T09:00")])
r.commit()

# --------------------------------------------------------------- bakusai db
bak_db = SCRATCH / "bakusai.db"
b = sqlite3.connect(bak_db)
b.executescript("""
CREATE TABLE bakusai_posts (
    thread_id TEXT, post_no INTEGER, posted_at TEXT, body TEXT,
    PRIMARY KEY (thread_id, post_no));
CREATE TABLE bakusai_shop_map (
    thread_id TEXT PRIMARY KEY, shop_id INTEGER, confidence REAL);
""")
b.executemany(
    "INSERT INTO bakusai_shop_map VALUES (?,?,?)",
    [("t1", 1, 0.9), ("t2", 2, 0.9), ("t3", 1, 0.5)])
b.executemany(
    "INSERT INTO bakusai_posts VALUES (?,?,?,?)",
    [("t1", 10, "2026-10-20 10:00", "白石 さちさん、めっちゃ神対応だった"),
     ("t1", 11, "2026-10-22 11:00", "今日は混んでたな"),
     ("t1", 12, "2026-10-26 12:00", "白石さちの枠埋まるの早すぎ"),
     ("t2", 1, "2026-10-27 12:00", "白石 さち が別店にいた"),      # other shop
     ("t3", 1, "2026-10-27 13:00", "白石 さち 低信頼スレ"),        # low conf
     ("t1", 13, "2026-10-25 12:00", "白石だけ名前が出てる"),         # partial
     ("t1", 14, "2026-10-24 12:00", "他の子は普通")])                # no name
b.commit()

# ------------------------------------------------------------------ checks
check("norm_person: spacing/honorific/age variants unify",
      len({ev.norm_person(x) for x in
           ("白石　さち", "白石 さちさん", "白石さち (40代前半)さん",
            "白石さち")}) == 1)
check("norm_person: different name differs",
      ev.norm_person("白石 さち") != ev.norm_person("白石 みさ"))

rconn = sqlite3.connect(rev_db)
rconn.row_factory = sqlite3.Row
check("review shop alias exact", ev.review_shop_id(rconn, "店A") == 41)
check("review shop alias normalized",
      ev.review_shop_id(rconn, "店A") == 41
      and ev.review_shop_id(rconn, "店Ｃ") is None)

rev = ev.review_evidence(rconn, "店A", "白石 さち")
check("review: 3 matching reviews only", rev["n"] == 3)
check("review: avg score 80.0", rev["score"] == 80.0)
check("review: avg stars 4.0", rev["stars"] == 4.0)
check("review: fetched date is the latest scrape",
      rev["fetched"] == "2026-06-16")
check("review: exactly 2 tags", len(rev["tags"]) == 2)
check("review: tag counts are review counts, not keyword hits",
      sum(n for _, n in rev["tags"]) <= rev["n"]
      and ("美人", 2) in rev["tags"])
check("review: hp url captured", rev["url"] == "https://men.example/a")

none_rev = ev.review_evidence(rconn, "店A", "存在しない子")
check("review: linked shop but no cast reviews -> n=0",
      none_rev["linked"] is True and none_rev["n"] == 0)
unlinked = ev.review_evidence(rconn, "店Z", "白石 さち")
check("review: unlinked shop -> linked False", unlinked["linked"] is False)

ck = ev.cast_key_for(con, 1, "白石 さち", "2026-10-28")
check("cast_key resolves cast_id", ck == "777")
loss = ev.slot_loss_evidence(con, 1, ck, "2026-10-28")
check("slot loss: earliest open->closed transition",
      loss["slot_time"] == "19:00" and loss["lost_at"] == "10-28 11:00")
check("slot loss: return (x->o) not counted, TEL->x counted", loss["n"] == 2)
check("slot loss: other day ignored",
      ev.slot_loss_evidence(con, 1, ck, "2026-10-27")["lost_at"]
      == "10-27 15:00")
check("slot loss: missing table -> None",
      ev.slot_loss_evidence(con, 1, "999", "2026-10-28") is None)

bconn = sqlite3.connect(bak_db)
bconn.row_factory = sqlite3.Row
bak = ev.bakusai_evidence(bconn, 1, "白石 さち")
check("bakusai: only high-confidence, same-shop, full-name posts",
      bak["n"] == 2)
check("bakusai: last mention date", bak["last"] == "2026-10-26")
check("bakusai: quote is the latest mention",
      "枠埋まるの早すぎ" in bak["quote"])
check("bakusai: low-confidence thread excluded",
      "低信頼" not in bak["quote"])
check("bakusai: other shop's thread excluded",
      ev.bakusai_evidence(bconn, 1, "白石 さち")["n"] == 2
      and ev.bakusai_evidence(bconn, 2, "白石 さち")["n"] == 1)

full = ev.collect(con, 1, "店A", "白石 さち", "2026-10-28",
                  review_db=str(rev_db), bakusai_db=str(bak_db))
lines = ev.format_lines(full)
text = "\n".join(lines)
check("format: review line with score/date/tags",
      "口コミ n=3" in text and "取得 2026-06-16" in text
      and "タグ:" in text)
check("format: slot line", "19:00枠が 10-28 11:00 消失" in text)
check("format: bakusai line + quote", "爆サイ: 言及2件" in text
      and "「" in text)
check("format: source urls include estama + review",
      "https://estama.jp/shop/1/schedule/" in text
      and "https://men.example/a" in text)

compact = ev.format_lines(full, prefix="    ", compact=True)
check("format compact: one 根拠 line",
      len(compact) == 1 and "根拠: 口コミ3件 80.0" in compact[0]
      and "枠消失10-28 11:00" in compact[0] and "爆サイ2件" in compact[0])

# missing / broken sources degrade to no lines, never raise
con.execute("INSERT INTO shops VALUES (3, '店C', NULL, NULL, NULL)")
con.commit()
empty = ev.collect(con, 3, "店C", "誰もいない", "2026-10-28",
                   review_db=str(SCRATCH / "nope.db"),
                   bakusai_db=str(SCRATCH / "nope.db"))
check("missing DBs: review unlinked, bakusai None",
      empty["review"]["linked"] is False and empty["bakusai"] is None)
check("missing DBs: no lines for shop without url",
      ev.format_lines(empty) == [])

broken = SCRATCH / "broken.db"
broken.write_text("not a sqlite file")
check("broken DB does not raise",
      ev.review_evidence(sqlite3.connect(":memory:"), "店A", "x") is not None)
try:
    ev.collect(con, 3, "店C", "x", "2026-10-28",
               review_db=str(broken), bakusai_db=str(broken))
    ok = True
except Exception:
    ok = False
check("corrupt DB file does not raise", ok)

# integration: tracker wrapper never breaks the alert
import estama_tracker as t  # noqa: E402

lines2 = t.alert_evidence_lines(con, "店A", "白石 さち", "2026-10-28",
                                review_db=str(rev_db),
                                bakusai_db=str(bak_db))
check("tracker wrapper returns evidence lines",
      any("口コミ n=3" in l for l in lines2))
try:
    t.alert_evidence_lines(con, "店A", "白石 さち", "2026-10-28",
                           review_db=str(broken), bakusai_db=str(broken))
    check("tracker wrapper swallows bad db path", True)
except Exception:
    check("tracker wrapper swallows bad db path", False)
check("tracker wrapper unknown shop -> no lines",
      t.alert_evidence_lines(con, "ない店", "x", "2026-10-28",
                             review_db=str(rev_db)) == [])

r.close()
b.close()
con.close()
ev.close_all()

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
