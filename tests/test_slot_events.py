"""Tests for 30-min slot event capture (slot_events).

Covers mark normalization, 27:00 -> 1620 parsing, first-sighting vs.
transition rows, idempotent reruns, reversals, off-shift '─' filler
and unknown-shift handling.
Run: python3.11 tests/test_slot_events.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from common import parse_clock  # noqa: E402
from estama_scraper import (  # noqa: E402
    EstamaScraper, _slot_within_shift, import_into_db, normalize_slot_mark,
    record_slot_events,
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


def make_cast(slots, cast_id=100, shift=("12:00", "20:00")):
    return {
        "cast_name": "テスト嬢",
        "cast_id": cast_id,
        "age": 25,
        "shift": "12:00～20:00",
        "shift_start": shift[0],
        "shift_end": shift[1],
        "work_minutes": 480,
        "work_hours": 8.0,
        "status_raw": "予約受付中",
        "status_norm": "available",
        "slot_pattern": "○1コマ / ×1コマ (予約可)",
        "slots": slots,
    }


def events(conn):
    return conn.execute(
        """SELECT slot_time, slot_minutes, from_mark, to_mark, observed_at,
                  checkpoint
           FROM slot_events ORDER BY slot_time, observed_at, event_id"""
    ).fetchall()


# ---------- mark normalization ----------
for raw, want in [
    ("○", "○"), ("◯", "○"), ("〇", "○"),
    ("×", "×"), ("✕", "×"), ("✖", "×"), ("x", "×"), ("X", "×"),
    ("─", "─"), ("－", "─"), ("-", "─"), ("ー", "─"), ("—", "─"),
    ("TEL", "TEL"), ("tel", "TEL"),
    ("", None), (None, None), ("あ", None), ("○×", None),
]:
    check(f"normalize_slot_mark({raw!r}) -> {want!r}",
          normalize_slot_mark(raw) == want)

check("parse_clock('27:00') == 1620", parse_clock("27:00") == 1620)
check("parse_clock('9:00') == 540", parse_clock("9:00") == 540)

# ---------- shift window ----------
check("27:00 inside 18:00-27:30",
      _slot_within_shift(1620, "18:00", "27:30") is True)
check("27:00 inside 19:00-over5:00",
      _slot_within_shift(1620, "19:00", "05:00") is True)
check("03:00 inside 19:00-over5:00",
      _slot_within_shift(180, "19:00", "05:00") is True)
check("06:00 outside 19:00-over5:00",
      _slot_within_shift(360, "19:00", "05:00") is False)
check("unknown shift returns None",
      _slot_within_shift(1620, None, None) is None)

# ---------- first sighting ----------
conn = fresh()
cast = make_cast([
    ["12:00", "○"], ["12:30", "×"], ["19:00", "─"], ["21:00", "─"],
])
n = record_slot_events(conn, 1, cast, "2026-10-30",
                       "2026-10-30 10:30:00 JST", "10:30")
rows = events(conn)
check("first sighting inserts 3 (off-shift dash skipped)", n == 3)
check("first sightings have from_mark NULL",
      all(r[2] is None for r in rows))
check("21:00 off-shift dash not stored",
      not any(r[0] == "21:00" for r in rows))
check("slot_minutes stored (12:30 -> 750)",
      any(r[0] == "12:30" and r[1] == 750 for r in rows))

# ---------- idempotent rerun ----------
n = record_slot_events(conn, 1, cast, "2026-10-30",
                       "2026-10-30 10:30:00 JST", "10:30")
check("rerun with same fetched_at inserts 0", n == 0)
check("rerun leaves row count unchanged", len(events(conn)) == 3)

# ---------- transitions + reversal ----------
cast2 = make_cast([
    ["12:00", "×"],   # ○ -> ×
    ["12:30", "○"],   # × -> ○ (reversal)
    ["19:00", "─"],   # unchanged
    ["21:00", "─"],   # still off-shift
])
n = record_slot_events(conn, 1, cast2, "2026-10-30",
                       "2026-10-30 11:30:00 JST", "11:30")
rows = events(conn)
check("transition checkpoint inserts 2", n == 2)
check("○ -> × recorded",
      any(r[0] == "12:00" and r[2] == "○" and r[3] == "×" for r in rows))
check("× -> ○ reversal recorded",
      any(r[0] == "12:30" and r[2] == "×" and r[3] == "○" for r in rows))

# ---------- late replay of an older checkpoint ----------
cast3 = make_cast([["12:00", "TEL"]])
n = record_slot_events(conn, 1, cast3, "2026-10-30",
                       "2026-10-30 09:30:00 JST", "09:30")
check("older checkpoint replay inserts 0", n == 0)

# ---------- unknown shift: no initial dashes, states still kept ----------
conn2 = fresh()
cast4 = make_cast([["12:00", "─"], ["12:30", "○"]], cast_id=None,
                  shift=(None, None))
n = record_slot_events(conn2, 1, cast4, "2026-10-30",
                       "2026-10-30 10:30:00 JST", "10:30")
rows2 = events(conn2)
check("unknown shift: initial dash skipped, ○ kept", n == 1)
check("unknown shift: only the ○ state stored",
      [r[0] for r in rows2] == ["12:30"])
check("cast_key falls back to cast_name",
      conn2.execute("SELECT cast_key FROM slot_events").fetchone()[0]
      == "テスト嬢")

# ---------- HTML parsing exposes per-slot marks ----------
HTML = """
<table>
<thead><tr><th>時間</th>
<th><h4>花子(24)</h4><a class="link-detail" href="/cast/12345/">詳細</a>
<div class="item-date">12:00～20:00</div>
<div class="item-t_now">予約受付中</div></th></tr></thead>
<tbody>
<tr><th>12:00</th><td data-idx="12345">○</td></tr>
<tr><th>12:30</th><td data-idx="12345">×</td></tr>
<tr><th>21:00</th><td data-idx="12345">─</td></tr>
</tbody></table>
"""
casts, _ = EstamaScraper(1)._parse_schedule_html(HTML, "2026-10-30", 0)
check("parser exposes slots list",
      bool(casts) and casts[0]["slots"] ==
      [["12:00", "○"], ["12:30", "×"], ["21:00", "─"]])

# ---------- import_into_db wiring (today only) ----------
import tempfile
with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "t.db"
    data = {
        "shop": {"shop_id": 1, "name": "テスト店", "area": "梅田",
                 "store_status": None, "source_url": None},
        "fetched_at": "2026-10-30 10:30:00 JST",
        "fetched_time": "10:30",
        "days": [
            {"target_date": "2026-10-30", "days_offset": 0,
             "casts": [make_cast([["12:00", "○"], ["12:30", "×"]])]},
            {"target_date": "2026-10-31", "days_offset": 1,
             "casts": [make_cast([["12:00", "○"], ["12:30", "○"]])]},
        ],
    }
    inserted = import_into_db(data, db_path=str(db_path))
    conn4 = sqlite3.connect(db_path)
    got = conn4.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0]
    # second import of the identical payload must not duplicate
    import_into_db(data, db_path=str(db_path))
    got2 = conn4.execute("SELECT COUNT(*) FROM slot_events").fetchone()[0]
    conn4.close()
check("import_into_db records today events only", inserted == 2 and got == 2)
check("import_into_db rerun does not duplicate slot events", got2 == 2)

print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("All slot event tests passed.")
