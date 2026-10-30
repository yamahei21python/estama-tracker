"""Tests for the 4h sell-out hazard forecast.

Covers business-hour mapping, pooled hazard counts, cast baseline
weighting, the watch/deep candidate filter and the cold-start rule
(n<3 or no cast history -> no probability, baseline ranking only).
Run: python3.11 tests/test_hazard.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from estama_tracker import (  # noqa: E402
    _bd_hour, load_deep_shop_ids, pooled_sellout_hazard, sellout_forecast,
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
    return conn


def add(conn, shop, cast, date, time, norm, cast_id=None):
    conn.execute(
        """INSERT INTO snapshots (shop_id, cast_name, cast_id, status_raw,
               status_norm, target_date, fetched_time, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (shop, cast, cast_id, norm, norm, date, time,
         f"{date} {time}:00 JST"))


# ---------- business-hour mapping ----------
for hh, want in [("10:00", 10), ("06:00", 6), ("05:00", 29),
                 ("00:30", 24), ("27:00", 27), ("9:00", 9),
                 (None, None), ("bad", None)]:
    check(f"_bd_hour({hh!r}) == {want}", _bd_hour(hh) == want)

# ---------- pooled hazard ----------
conn = fresh()
conn.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
conn.execute("INSERT INTO shops VALUES (2, '店B', '難波', NULL, NULL)")
HIST = ("2026-10-26", "2026-10-27", "2026-10-28")
for d in HIST:
    # A: available 10/11, sold out at 12, stays sold 13
    add(conn, 1, "A", d, "10:00", "available", 101)
    add(conn, 1, "A", d, "11:00", "available", 101)
    add(conn, 1, "A", d, "12:00", "sold_out", 101)
    add(conn, 1, "A", d, "13:00", "sold_out", 101)
    # B: never sells out, observed 10..13
    for t in ("10:00", "11:00", "12:00", "13:00"):
        add(conn, 1, "B", d, t, "available", 102)
    # C: only observed at 11
    add(conn, 1, "C", d, "11:00", "available", 103)
    # D: shop 2 (not deep by default), sells out at 12
    add(conn, 2, "D", d, "10:00", "available", 201)
    add(conn, 2, "D", d, "12:00", "sold_out", 201)
conn.commit()

ev, risk = pooled_sellout_hazard(conn, "2026-10-29")
check("events land in the first sold-out bucket", ev[12] == 6)
check("at-risk pools A+B+C+D at 11:00", risk[11] == 12)
check("at-risk excludes sold-out days after the event", risk[13] == 3)

# target day: A/B/C available, D available in a non-deep shop
target = "2026-10-29"
for cid, name in ((101, "A"), (102, "B"), (103, "C")):
    add(conn, 1, name, target, "11:00", "available", cid)
add(conn, 2, "D", target, "11:00", "available", 201)
conn.commit()

fc = sellout_forecast(conn, target, at_hhmm="11:00",
                      deep_shop_ids={1})
check("anchor = 11:00", fc["anchor_hour"] == 11)
check("pooled n = 12", fc["n"] == 12)
check("4h pooled probability = 0.727", fc["pooled_hazard"] == 0.727)
names = [c["cast_name"] for c in fc["qualified"]]
check("only cast A (3/3 history) qualifies", names == ["A"])
a = fc["qualified"][0]
check("A p = pooled * sqrt(1.0) = 0.727", a["p"] == 0.727)
check("A carries baseline 3/3", (a["hist_sold"], a["hist_days"]) == (3, 3))
check("B (0/3) never gets p>=0.5", all(c["cast_name"] != "B"
                                       for c in fc["qualified"]))
check("non-deep shop cast D is excluded",
      all(c["cast_name"] != "D" for c in fc["baseline"]))
check("no probability in the baseline-only list",
      all(c["p"] is None for c in fc["baseline"]))

# watch registration pulls a non-deep shop cast in
conn.execute("INSERT INTO cast_watch VALUES ('201', 2, 'D', '2026-10-29', NULL)")
conn.commit()
fc2 = sellout_forecast(conn, target, at_hhmm="11:00", deep_shop_ids={1})
check("watch cast D becomes a candidate",
      any(c["cast_name"] == "D"
          for c in fc2["qualified"] + fc2["baseline"]))

# ---------- cold start: n<3 -> no probability ----------
cold = fresh()
cold.execute("INSERT INTO shops VALUES (1, '店A', '梅田', NULL, NULL)")
add(cold, 1, "A", "2026-10-28", "10:00", "available", 101)
add(cold, 1, "A", "2026-10-28", "12:00", "sold_out", 101)
add(cold, 1, "A", "2026-10-29", "11:00", "available", 101)
cold.commit()
fc3 = sellout_forecast(cold, "2026-10-29", at_hhmm="11:00",
                       deep_shop_ids={1})
check("cold start: pooled n = 1 at 11:00", fc3["n"] == 1)
check("cold start: no qualified probability", fc3["qualified"] == [])
check("cold start: baseline ranking still shown",
      [c["cast_name"] for c in fc3["baseline"]] == ["A"])
check("cold start: p withheld", fc3["baseline"][0]["p"] is None)

# ---------- deep shop file ----------
tmp = Path(tempfile.mkdtemp(prefix="estama-deep-")) / "deep.json"
tmp.write_text('{"shops": [{"shop_id": 3, "name": "x"}, 7]}')
check("deep shop ids from file", load_deep_shop_ids(tmp) == {3, 7})
check("missing deep file -> empty set",
      load_deep_shop_ids(str(tmp.parent / "no-such-deep.json")) == set())
tmp.unlink()

print()
if fails:
    print(f"FAILED: {len(fails)}")
    sys.exit(1)
print("All hazard tests passed.")
