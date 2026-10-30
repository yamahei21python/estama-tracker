"""Deterministic fixture DB shared by the report characterization and
golden-output tests.

Everything that the report can print is represented once (sold-out
streaks, alerts with history, a low forecast candidate, a rename, a
transfer, a legacy cast without cast_id, dummy/unknown rows, rankings
with an unregistered shop, slot events, run_log/scrape_log coverage
problems). Fetch timestamps are fixed strings and distinct per shop so
SQL tie-breaking can never reorder output between runs or machines.

Not a test itself (leading underscore): the CI loop runs test_*.py.
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = (ROOT / "app" / "schema.sql").read_text(encoding="utf-8")

# business days: Fri, Sat, Sun, Mon (2026-10-30 is a Friday)
D0, D1, D2, D3 = "2026-10-23", "2026-10-24", "2026-10-25", "2026-10-26"
DATES = (D0, D1, D2, D3)

RAW = {
    "sold_out": "ご好評につき本日完売",
    "available": "ご予約受付中",
    "low": "残りわずか",
    "closed": "本日の受付を終了しました",
    "dummy": "dummy",
    "unknown": "??",
}


def _snap(conn, shop, name, cid, date, time, norm, fetched_at=None,
          shift=None, work_hours=None, slot=None, age=None):
    conn.execute(
        "INSERT INTO snapshots (shop_id, cast_name, cast_id, age, shift,"
        " status_raw, status_norm, slot_pattern, target_date, fetched_at,"
        " fetched_time) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (shop, name, cid, age, shift, RAW[norm], norm, slot, date,
         fetched_at or f"{date} {time}:00 JST", time))


def _slot(conn, shop, cid, name, date, slot_time, minutes, frm, to,
          observed, checkpoint):
    conn.execute(
        "INSERT INTO slot_events (shop_id, cast_key, cast_id, cast_name,"
        " target_date, slot_time, slot_minutes, from_mark, to_mark,"
        " observed_at, checkpoint) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (shop, str(cid), cid, name, date, slot_time, minutes, frm, to,
         observed, checkpoint))


def build(path):
    """Write the fixture DB to `path` and return the path."""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)

    conn.executemany(
        "INSERT INTO shops (shop_id, name, area, store_status, source_url)"
        " VALUES (?,?,?,?,?)",
        [(1, "テスト梅田店", "梅田", None, "https://estama.jp/shop/1/schedule/"),
         (2, "テスト難波店", "難波", None, None),
         (3, "テスト天王寺店", "天王寺", None, None),
         (4, "テスト堺店", None, None, None)])

    shift = "12:00-20:00"
    # shop 1: alert (101), low candidate (102), streak (103), rename (140)
    for d in (D0, D1, D2):
        _snap(conn, 1, "葵 みさき", 101, d, "10:00", "sold_out",
              shift=shift, work_hours=8.0)
        _snap(conn, 1, "葵 みさき", 101, d, "15:00", "sold_out",
              shift=shift, work_hours=8.0)
    _snap(conn, 1, "葵 みさき", 101, D3, "10:00", "available",
          shift=shift, work_hours=8.0)
    _snap(conn, 1, "葵 みさき", 101, D3, "15:00", "available",
          shift=shift, work_hours=8.0)

    _snap(conn, 1, "白石 さち", 102, D0, "10:00", "available",
          slot="○5コマ / ×0コマ (予約可)")
    _snap(conn, 1, "白石 さち", 102, D0, "15:00", "available",
          slot="○3コマ / ×2コマ (予約可)")
    _snap(conn, 1, "白石 さち", 102, D1, "10:00", "available")
    _snap(conn, 1, "白石 さち", 102, D1, "15:00", "sold_out")
    _snap(conn, 1, "白石 さち", 102, D2, "10:00", "available")
    _snap(conn, 1, "白石 さち", 102, D2, "15:00", "available")
    _snap(conn, 1, "白石 さち", 102, D3, "10:00", "available",
          slot="○5コマ / ×0コマ (予約可)")
    _snap(conn, 1, "白石 さち", 102, D3, "15:00", "low",
          slot="○0コマ / ×5コマ (予約可)")

    for d in DATES:
        _snap(conn, 1, "常連 れい", 103, d, "10:00", "sold_out")
        _snap(conn, 1, "常連 れい", 103, d, "15:00", "sold_out")

    # same cast_id under two names (rename) + a late-night first sold_out
    _snap(conn, 1, "改名 ゆき子", 140, D2, "10:00", "available")
    _snap(conn, 1, "改名 ゆき子", 140, D2, "01:00", "sold_out",
          fetched_at=f"{D3} 01:00:00 JST")
    _snap(conn, 1, "改名 ゆき", 140, D3, "10:00", "available")
    _snap(conn, 1, "改名 ゆき", 140, D3, "15:00", "low")

    # shop 2: alert (201), closed (202), legacy row without cast_id
    _snap(conn, 2, "高橋 ゆい", 201, D0, "10:00", "available")
    _snap(conn, 2, "高橋 ゆい", 201, D0, "15:00", "sold_out")
    for d in (D1, D2):
        _snap(conn, 2, "高橋 ゆい", 201, d, "10:00", "sold_out")
        _snap(conn, 2, "高橋 ゆい", 201, d, "15:00", "sold_out")
    _snap(conn, 2, "高橋 ゆい", 201, D3, "10:00", "available")
    _snap(conn, 2, "高橋 ゆい", 201, D3, "15:00", "available")
    _snap(conn, 2, "空き そら", 202, D3, "12:00", "closed")
    _snap(conn, 2, "名無し レガシー", None, D3, "11:00", "available")

    # shop 3: new casts, dummy/unknown, transfer source (cast 999)
    _snap(conn, 3, "新人 あや", 301, D3, "10:00", "available")
    _snap(conn, 3, "ダミー 子", 303, D3, "11:00", "dummy")
    _snap(conn, 3, "不明 子", 304, D3, "13:00", "unknown")
    for d in (D0, D1, D2):
        _snap(conn, 3, "移籍 まい", 999, d, "10:00", "available")
        _snap(conn, 3, "移籍 まい", 999, d, "15:00", "available")

    # shop 4 (area unset): transfer destination + closed cast
    _snap(conn, 4, "移籍 まい", 999, D3, "10:00", "available")
    _snap(conn, 4, "堺 ひな", 401, D3, "14:00", "closed")

    # slot events: evidence for both alerts, monitors' capture rate
    _slot(conn, 1, 101, "葵 みさき", D3, "18:30", 1110, None, "×",
          f"{D3} 10:00:00 JST", "10:00")
    _slot(conn, 1, 101, "葵 みさき", D3, "19:00", 1140, "○", "×",
          f"{D3} 15:00:00 JST", "15:00")
    _slot(conn, 1, 102, "白石 さち", D3, "19:00", 1140, "○", "×",
          f"{D3} 15:00:00 JST", "15:00")
    _slot(conn, 2, 201, "高橋 ゆい", D3, "19:00", 1140, "○", "×",
          f"{D3} 15:00:00 JST", "15:00")
    _slot(conn, 3, 301, "新人 あや", D3, "20:00", 1200, None, "×",
          f"{D3} 10:00:00 JST", "10:00")
    _slot(conn, 3, 303, "ダミー 子", D3, "20:00", 1200, None, "×",
          f"{D3} 11:00:00 JST", "11:00")
    _slot(conn, 4, 999, "移籍 まい", D3, "20:00", 1200, None, "×",
          f"{D3} 10:00:00 JST", "10:00")
    _slot(conn, 4, 401, "堺 ひな", D3, "21:00", 1260, None, "×",
          f"{D3} 14:00:00 JST", "14:00")

    # rankings: two weekly snapshots on area 2700 (overall Osaka)
    conn.executemany(
        "INSERT INTO cast_rankings (area_code, area_name, period_type,"
        " rank, is_pr, cast_id, cast_name, shop_id, shop_name,"
        " fetched_date, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [("2700", "大阪府総合", "weekly", 8, 0, 101, "葵 みさき", 1,
          "テスト梅田店", D2, f"{D2} 09:00"),
         ("2700", "大阪府総合", "weekly", 5, 0, 201, "高橋 ゆい", 2,
          "テスト難波店", D2, f"{D2} 09:00"),
         ("2700", "大阪府総合", "weekly", 20, 0, 900, "未登録 かな", 900,
          "未登録店X", D2, f"{D2} 09:00"),
         ("2700", "大阪府総合", "weekly", 1, 0, 101, "葵 みさき", 1,
          "テスト梅田店", D3, f"{D3} 09:00"),
         ("2700", "大阪府総合", "weekly", 5, 0, 201, "高橋 ゆい", 2,
          "テスト難波店", D3, f"{D3} 09:00"),
         ("2700", "大阪府総合", "weekly", 25, 0, 900, "未登録 かな", 900,
          "未登録店X", D3, f"{D3} 09:00")])

    # runner / scrape audit: 2 ok + 1 fail checkpoints, one failing and
    # one empty shop
    conn.executemany(
        "INSERT INTO run_log (job, started_at, finished_at, status, detail)"
        " VALUES (?,?,?,?,?)",
        [("checkpoint", f"{D3} 10:00:00 JST", f"{D3} 10:01:00 JST",
          "ok", "shops=4"),
         ("checkpoint", f"{D3} 11:00:00 JST", f"{D3} 11:01:00 JST",
          "ok", "shops=4"),
         ("checkpoint", f"{D3} 12:00:00 JST", f"{D3} 12:01:00 JST",
          "fail", "timeout shop=2"),
         ("prune", f"{D3} 03:00:00 JST", f"{D3} 03:00:05 JST",
          "ok", "archived 1")])
    conn.executemany(
        "INSERT INTO scrape_log (run_id, shop_id, fetched_at, status,"
        " rows, detail) VALUES (?,?,?,?,?,?)",
        [(None, 1, f"{D3} 10:00:00 JST", "ok", 4, None),
         (None, 1, f"{D3} 15:00:00 JST", "ok", 4, None),
         (None, 2, f"{D3} 10:00:00 JST", "fail", 0, "timeout"),
         (None, 2, f"{D3} 11:00:00 JST", "ok", 3, None),
         (None, 2, f"{D3} 15:00:00 JST", "fail", 0, "timeout"),
         (None, 3, f"{D3} 10:00:00 JST", "empty", 0, "0 casts"),
         (None, 3, f"{D3} 15:00:00 JST", "empty", 0, "0 casts")])

    # watch list: both alerting casts so the forecast/notify have targets
    conn.executemany(
        "INSERT INTO cast_watch (cast_key, shop_id, cast_name, added_at,"
        " note) VALUES (?,?,?,?,?)",
        [("101", 1, "葵 みさき", "2026-10-20 12:00 JST", "推し"),
         ("201", 2, "高橋 ゆい", "2026-10-21 12:00 JST", None)])

    conn.commit()
    conn.close()
    return path
