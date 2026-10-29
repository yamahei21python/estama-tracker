"""Tests for daily_cast_status rollup + report sections.

Guards the alert-history foundation: the rollup must reproduce the R3
effective final state (incl. closed walk-back) one row per cast-day,
be idempotent, and key by cast_id with name fallback.
Run: python3 tests/test_rollup.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from estama_tracker import (  # noqa: E402
    rollup_daily, section_daily, section_streaks,
    section_transitions, section_consistency,
    section_forecast, section_coverage, section_runlog,
    section_rank_moves, section_watch, section_momentum,
    section_new_casts, section_cast_detail,
    watch_add, watch_remove, build_notify_text, build_dashboard,
)

SCHEMA = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text()

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def fresh():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO shops VALUES (1, 'テスト店', '梅田', NULL, NULL)")
    return conn


def add(conn, cast, date, norm, fetched_at, cast_id=None):
    conn.execute(
        "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
        " status_norm, target_date, fetched_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?)",
        (cast, cast_id, norm, norm, date, fetched_at),
    )


def roll(conn):
    return conn.execute(
        "SELECT shop_id, cast_key, cast_id, cast_name, target_date,"
        " status_norm, fetched_at FROM daily_cast_status").fetchall()


# 1. rollup materializes R3 final states (closed tail walks back)
c = fresh()
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=10)
add(c, "A", "2026-10-26", "closed", "2026-10-27 05:31:00 JST", cast_id=10)
add(c, "B", "2026-10-26", "available", "2026-10-26 12:00:00 JST")
rollup_daily(c)
rows = roll(c)
check("rollup: one row per cast-day", len(rows) == 2)
a = [r for r in rows if r[3] == "A"][0]
check("rollup: R3 walk-back keeps sold_out", a[5] == "sold_out")
check("rollup: cast_key uses cast_id", a[1] == "10")
b = [r for r in rows if r[3] == "B"][0]
check("rollup: name fallback key", b[1] == "B" and b[2] is None)

# 2. idempotent: second rollup writes the same rows
rollup_daily(c)
check("rollup: idempotent", len(roll(c)) == 2)

# 3. per-date rollup only rebuilds that date
add(c, "A", "2026-10-27", "available", "2026-10-27 12:00:00 JST", cast_id=10)
rollup_daily(c, "2026-10-27")
rows = roll(c)
check("rollup: per-date adds only that day",
      len(rows) == 3 and sum(r[4] == "2026-10-27" for r in rows) == 1)

# 4. rename merges under one cast_key
c = fresh()
add(c, "旧名", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=55)
add(c, "新名", "2026-10-26", "available", "2026-10-26 18:00:00 JST", cast_id=55)
rollup_daily(c)
rows = roll(c)
check("rollup: rename = 1 row", len(rows) == 1 and rows[0][1] == "55")

# 4b. first_sold_out_at = earliest sold_out observation that day
c = fresh()
add(c, "A", "2026-10-26", "available", "2026-10-26 12:00:00 JST", cast_id=10)
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 18:00:00 JST", cast_id=10)
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 20:00:00 JST", cast_id=10)
add(c, "B", "2026-10-26", "available", "2026-10-26 12:00:00 JST", cast_id=11)
rollup_daily(c)
fa = {r[3]: r[7] for r in c.execute(
    "SELECT shop_id, cast_key, cast_id, cast_name, target_date,"
    " status_norm, fetched_at, first_sold_out_at"
    " FROM daily_cast_status")}
check("rollup: first_sold_out_at persisted",
      fa["A"] == "2026-10-26 18:00:00 JST" and fa["B"] is None)

# 4c. was_low flag + forecast_eval self-scoring
c = fresh()
add(c, "A", "2026-10-26", "low", "2026-10-26 12:00:00 JST", cast_id=10)
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 18:00:00 JST", cast_id=10)
add(c, "B", "2026-10-26", "low", "2026-10-26 12:00:00 JST", cast_id=11)
add(c, "B", "2026-10-26", "available", "2026-10-26 18:00:00 JST", cast_id=11)
add(c, "C", "2026-10-26", "low", "2026-10-26 19:00:00 JST", cast_id=12)
add(c, "C", "2026-10-26", "sold_out", "2026-10-26 18:00:00 JST", cast_id=12)
rollup_daily(c)
wl = {r[3]: r[8] for r in c.execute(
    "SELECT shop_id, cast_key, cast_id, cast_name, target_date,"
    " status_norm, fetched_at, first_sold_out_at, was_low"
    " FROM daily_cast_status")}
# A: low before sold_out -> was_low; B: low, never sold -> was_low;
# C: low only AFTER the sold_out -> was_low = 0
check("rollup: was_low flag",
      wl["A"] == 1 and wl["B"] == 1 and wl["C"] == 0)
ev = c.execute(
    "SELECT candidates, hits FROM forecast_eval"
    " WHERE target_date='2026-10-26'").fetchone()
check("rollup: forecast_eval scored", ev == (2, 1))

# 5. section_daily counts use R3 states
c = fresh()
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=1)
add(c, "A", "2026-10-26", "closed", "2026-10-27 05:31:00 JST", cast_id=1)
lines = section_daily(c, ["2026-10-26"])
check("section_daily: sold_out counted", "sold_out=  1" in lines[0])

# 6. streaks across days
c = fresh()
for d in ("2026-10-26", "2026-10-27", "2026-10-28"):
    add(c, "S", d, "sold_out", f"{d} 20:00:00 JST", cast_id=7)
add(c, "S", "2026-10-29", "available", "2026-10-29 12:00:00 JST", cast_id=7)
lines = section_streaks(c, ["2026-10-26", "2026-10-27",
                            "2026-10-28", "2026-10-29"], streak_min=2)
check("section_streaks: streak ends at latest day",
      lines == [] or all("S (" not in l for l in lines))
# now make 10-29 sold out too -> 4-day streak
c.execute("UPDATE snapshots SET status_norm='sold_out'"
          " WHERE target_date='2026-10-29'")
lines = section_streaks(c, ["2026-10-26", "2026-10-27",
                            "2026-10-28", "2026-10-29"], streak_min=2)
check("section_streaks: 4-day streak found",
      len(lines) == 1 and "4日連続完売" in lines[0])

# 7. transitions between last two days
c = fresh()
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 20:00:00 JST", cast_id=1)
add(c, "A", "2026-10-27", "available", "2026-10-27 12:00:00 JST", cast_id=1)
add(c, "B", "2026-10-27", "available", "2026-10-27 12:00:00 JST", cast_id=2)
lines = section_transitions(c, ["2026-10-26", "2026-10-27"])
check("section_transitions: churn + matrix",
      any("共通=1" in l and "新規=1" in l for l in lines)
      and any("sold_out   -> available" in l for l in lines))

# 8. consistency: rename detection
c = fresh()
add(c, "旧名", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=55)
add(c, "新名", "2026-10-26", "available", "2026-10-26 18:00:00 JST", cast_id=55)
lines = section_consistency(c)
check("section_consistency: rename detected",
      any("1組" in l for l in lines))

# 9. forecast: 'low' today + sold-out history = candidate
c = fresh()
add(c, "L", "2026-10-26", "sold_out", "2026-10-26 20:00:00 JST", cast_id=9)
add(c, "L", "2026-10-27", "sold_out", "2026-10-27 20:00:00 JST", cast_id=9)
add(c, "L", "2026-10-28", "low", "2026-10-28 20:00:00 JST", cast_id=9)
add(c, "N", "2026-10-26", "available", "2026-10-26 20:00:00 JST", cast_id=8)
add(c, "N", "2026-10-27", "available", "2026-10-27 20:00:00 JST", cast_id=8)
add(c, "N", "2026-10-28", "low", "2026-10-28 20:00:00 JST", cast_id=8)
lines = section_forecast(c, "2026-10-28", min_days=2)
check("section_forecast: only proven candidates",
      len(lines) == 2 and "L" in lines[1] and "N" not in lines[1])

# 10. coverage: missing hours + stale shop detection
c = fresh()
for h in (6, 7, 9):   # hour 8 missing
    add(c, "A", "2026-10-26", "available",
        f"2026-10-26 {h:02d}:35:00 JST", cast_id=1)
add(c, "B", "2026-10-26", "available", "2026-10-26 06:35:00 JST", cast_id=2)
lines = section_coverage(c, ["2026-10-26"])
check("section_coverage: missing hour detected",
      any("未取得時間=08" in l for l in lines))

# stale shop: one shop scraped in far fewer hours than the median
c = fresh()
c.execute("INSERT INTO shops VALUES (2, 'テスト店2', '難波', NULL, NULL)")
for h in range(6, 12):
    add(c, "A", "2026-10-26", "available",
        f"2026-10-26 {h:02d}:35:00 JST", cast_id=1)
add(c, "B", "2026-10-26", "available", "2026-10-26 06:35:00 JST", cast_id=2)
c.execute("UPDATE snapshots SET shop_id=2 WHERE cast_name='B'")
lines = section_coverage(c, ["2026-10-26"])
check("section_coverage: stale shop flagged",
      any("2 テスト店2" in l and "1時間のみ" in l for l in lines))

# 11. run_log: stale 'running' (dead runner) is flagged
c = fresh()
c.execute(
    "INSERT INTO run_log (job, started_at, status)"
    " VALUES ('checkpoint', '2020-01-01 00:00:00 JST', 'running')")
c.execute(
    "INSERT INTO run_log (job, started_at, status)"
    " VALUES ('prune', '2020-01-01 00:05:00 JST', 'ok')")
lines = section_runlog(c)
check("section_runlog: stale running flagged",
      any("滞留run" in l and "checkpoint" in l for l in lines)
      and not any("滞留run" in l and "prune" in l for l in lines))

# 12. rank moves between two ranking snapshots
c = fresh()
for i, (cid, nm, rk) in enumerate([
        (101, "上昇子", 12), (102, "下降子", 10), (103, "静止子", 20),
        (104, "消え子", 25)]):
    c.execute(
        "INSERT INTO cast_rankings (area_code, area_name, period_type,"
        " rank, is_pr, cast_id, cast_name, shop_id, shop_name,"
        " fetched_date, fetched_at) VALUES ('2700','大阪','weekly',"
        " ?, 0, ?, ?, 1, 'テスト店', '2026-10-28', '2026-10-28 06:00:00 JST')",
        (rk, cid, nm))
for cid, nm, rk in [(101, "上昇子", 2), (102, "下降子", 18),
                    (103, "静止子", 20), (105, "新人", 15)]:
    c.execute(
        "INSERT INTO cast_rankings (area_code, area_name, period_type,"
        " rank, is_pr, cast_id, cast_name, shop_id, shop_name,"
        " fetched_date, fetched_at) VALUES ('2700','大阪','weekly',"
        " ?, 0, ?, ?, 1, 'テスト店', '2026-10-29', '2026-10-29 06:00:00 JST')",
        (rk, cid, nm))
lines = section_rank_moves(c)
check("section_rank_moves: movers + in/out",
      any("上昇子" in l and "12位 -> 2位" in l for l in lines)
      and any("下降子" in l and "10位 -> 18位" in l for l in lines)
      and any("新規IN" in l and "新人" in l for l in lines)
      and any("圏外OUT" in l and "消え子" in l for l in lines)
      and not any("静止子" in l for l in lines))

# 13. cast_watch: add/list/remove + section output
c = fresh()
add(c, "推し", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST", cast_id=77)
add(c, "推し", "2026-10-27", "available", "2026-10-27 12:00:00 JST",
    cast_id=77)
rollup_daily(c)
ck, cname = watch_add(c, 1, cast_id=77)
check("watch_add resolves key+name", ck == "77" and cname == "推し")
lines = section_watch(c, "2026-10-27")
check("section_watch: status + weekday",
      any("推し" in l and "空き" in l and "月1" in l and "火1" in l
          for l in lines))
n = watch_remove(c, 1, cast_id=77)
check("watch_remove", n == 1)

# 14. notify text carries alerts + watch hits
c = fresh()
add(c, "常連", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST",
    cast_id=10)
add(c, "常連", "2026-10-27", "sold_out", "2026-10-27 12:00:00 JST",
    cast_id=10)
add(c, "常連", "2026-10-28", "available", "2026-10-28 12:00:00 JST",
    cast_id=10)
rollup_daily(c)
watch_add(c, 1, cast_id=10)
text = build_notify_text(c, "2026-10-28")
check("build_notify_text: alert + watch hit",
      "普段完売なのに空き" in text and "ウォッチ中" in text)

# 15. dashboard renders key sections
c = fresh()
add(c, "A", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST",
    cast_id=10)
add(c, "B", "2026-10-26", "low", "2026-10-26 12:00:00 JST", cast_id=11)
rollup_daily(c)
html = build_dashboard(c, "2026-10-26")
check("dashboard: cards + sections",
      "estama tracker" in html and "完売（1人）" in html
      and "アラート" in html and "ランキング変動" in html)

# 16. momentum: x-count growth = bookings in flight
c = fresh()
c.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
    " status_norm, slot_pattern, target_date, fetched_at)"
    " VALUES (1, '人気', 50, 'low', 'low', '○19コマ / ×0コマ (予約可)',"
    " '2026-10-26', '2026-10-26 10:00:00 JST')")
c.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
    " status_norm, slot_pattern, target_date, fetched_at)"
    " VALUES (1, '人気', 50, 'low', 'low', '○4コマ / ×9コマ (予約可)',"
    " '2026-10-26', '2026-10-26 12:00:00 JST')")
c.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
    " status_norm, slot_pattern, target_date, fetched_at)"
    " VALUES (1, '閑散', 51, 'ok', 'available', '○15コマ / ×0コマ (予約可)',"
    " '2026-10-26', '2026-10-26 10:00:00 JST')")
# sold out then flipped back to closed at shift end: R3 final is
# sold_out, so the cast must NOT appear as 'still booking' momentum
c.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
    " status_norm, slot_pattern, target_date, fetched_at)"
    " VALUES (1, '完売済', 52, 'ok', 'available', '○10コマ / ×0コマ (予約可)',"
    " '2026-10-26', '2026-10-26 10:00:00 JST')")
c.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
    " status_norm, slot_pattern, target_date, fetched_at)"
    " VALUES (1, '完売済', 52, 'soldout', 'sold_out', '○0コマ / ×6コマ',"
    " '2026-10-26', '2026-10-26 12:00:00 JST')")
c.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
    " status_norm, slot_pattern, target_date, fetched_at)"
    " VALUES (1, '完売済', 52, 'closed', 'closed', NULL,"
    " '2026-10-26', '2026-10-26 23:00:00 JST')")
lines = section_momentum(c, "2026-10-26")
check("section_momentum: booking growth detected",
      any("人気" in l and "9枠消化" in l for l in lines)
      and not any("閑散" in l for l in lines))
check("section_momentum: sold_out (R3) excluded",
      not any("完売済" in l for l in lines))

# 17. new casts + transfers
c = fresh()
add(c, "ベテラン", "2026-10-25", "available", "2026-10-25 12:00:00 JST",
    cast_id=60)
add(c, "ルーキー", "2026-10-26", "available", "2026-10-26 12:00:00 JST",
    cast_id=61)
add(c, "移籍子", "2026-10-25", "available", "2026-10-25 12:00:00 JST",
    cast_id=62)
c.execute("INSERT INTO shops VALUES (9, '別店', '難波', NULL, NULL)")
c.execute(
    "INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,"
    " status_norm, target_date, fetched_at)"
    " VALUES (9, '移籍子', 62, 'ok', 'available',"
    " '2026-10-26', '2026-10-26 12:00:00 JST')")
lines = section_new_casts(c, "2026-10-26")
check("section_new_casts: debut + transfer",
      any("新規cast（2026-10-26 初登場）: 1人" in l for l in lines)
      and any("移籍子" in l and "別店" in l for l in lines))

# 18. cast detail: attendance + sell-out hours + totals
c = fresh()
for i, (d, st) in enumerate([
        ("2026-10-26", "sold_out"), ("2026-10-27", "sold_out"),
        ("2026-10-28", "available")]):
    add(c, "詳細子", d, st, f"{d} 18:00:00 JST", cast_id=70)
rollup_daily(c)
lines = section_cast_detail(c, 1, cast_id=70)
check("section_cast_detail: profile",
      any("出勤曜日傾向" in l and "月: 1日" in l for l in lines)
      and any("完売時刻" in l for l in lines)
      and any("完売 2日 (67%)" in l for l in lines))

# 19. notify additions: watch sold_out + momentum + runner fail
c = fresh()
add(c, "常連", "2026-10-26", "sold_out", "2026-10-26 12:00:00 JST",
    cast_id=10)
rollup_daily(c)
watch_add(c, 1, cast_id=10)
c.execute(
    "INSERT INTO run_log (job, started_at, status, detail)"
    " VALUES ('checkpoint', '2020-01-01 00:00:00 JST', 'fail', 'oops')")
text = build_notify_text(c, "2026-10-26")
check("notify: watch sold_out + runner fail",
      "ウォッチ中のcastが完売" in text and "runner失敗" in text)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
