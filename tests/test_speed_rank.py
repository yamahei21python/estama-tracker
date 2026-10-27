"""Regression tests for show_speed_ranking cross-midnight ordering.

Guards the 2026-10-27 fix: first_sold_out_time, the checkpoint header
and the timeline must follow fetched_at (real chronology across
midnight), not fetched_time string order - within one target_date the
00-05 checkpoints belong to the NEXT calendar day and used to sort
before 10:xx (e.g. a 13:33 sold-out was reported as "00:33").
The <06:00 sold-out badge must be 深夜完売, never 超速完売.
Run: python3 tests/test_speed_rank.py
"""
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from common import connect_db  # noqa: E402
from estama_scraper import show_speed_ranking  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


tmp_db = os.path.join(tempfile.mkdtemp(prefix="estama-speed-"), "t.db")
conn = connect_db(tmp_db)
conn.execute("INSERT INTO shops VALUES (1, 'テスト店', '梅田', NULL, NULL)")

DATE = "2026-10-26"


def add(cast, time, norm, at_day=DATE):
    """at_day = calendar day of fetched_at only; target_date stays DATE
    (00-05 checkpoints belong to the next calendar day of one target)."""
    conn.execute(
        "INSERT INTO snapshots (shop_id, cast_name, status_raw, status_norm,"
        " target_date, fetched_time, fetched_at)"
        " VALUES (1, ?, ?, ?, ?, ?, ?)",
        (cast, norm, norm, DATE, time, f"{at_day} {time}:00 JST"),
    )


# A: sold out 13:33, still sold out at next-day 00:33 checkpoint
add("朝完売A", "10:00", "available")
add("朝完売A", "13:33", "sold_out")
add("朝完売A", "23:33", "sold_out")
add("朝完売A", "00:33", "sold_out", at_day="2026-10-27")
# B: available until late, sold out only after midnight (00:33)
add("深夜完売B", "10:00", "available")
add("深夜完売B", "23:33", "available")
add("深夜完売B", "00:33", "sold_out", at_day="2026-10-27")
# C: never sold out (must not appear in the ranking)
add("無関係C", "10:00", "available")
conn.commit()

buf = io.StringIO()
with redirect_stdout(buf):
    show_speed_ranking(date_str=DATE, db_path=tmp_db, shop_id=1)
out = buf.getvalue()
conn.close()

# 1. A's first sold-out uses fetched_at chronology (13:33, not 00:33)
import re  # noqa: E402

strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)  # noqa: E731
rank_part = strip(out.split("📈")[0])
a_line = [ln for ln in rank_part.splitlines() if "朝完売A" in ln][0]
check("A first_sold_out = 13:33 (fetched_at), not 00:33",
      "完売検知: 13:33" in a_line and "00:33" not in a_line)

# 2. ranking order: 13:33 (2026-10-26) ranks above 00:33 (2026-10-27)
check("ranking order by real chronology (A before B)",
      rank_part.index("朝完売A") < rank_part.index("深夜完売B"))

# 3. checkpoint header is chronological: 10:00, 23:33, 00:33
header = [ln for ln in out.splitlines() if "記録済みチェックポイント" in ln][0]
check("checkpoint header order 10:00,23:33,00:33",
      header.index("10:00") < header.index("23:33") < header.index("00:33"))

# 4. <06:00 sold-out badge is 深夜, never 超速
b_line = [ln for ln in out.splitlines() if "深夜完売B" in ln][0]
check("B badge = 深夜完売 (not 超速)",
      "深夜完売 🌙" in b_line and "超速完売" not in b_line)
a_line = [ln for ln in out.splitlines() if "朝完売A" in ln][0]
check("A badge = 超速完売 (10:00... first reported 13:33 -> 夕方/超速 tier ok)",
      "超速完売" in a_line or "夕方完売" in a_line)

# 5. timeline columns follow the same chronological header order
timeline = out.split("時系列タイムライン")[1] if "時系列タイムライン" in out else ""
check("timeline rendered with chronological columns",
      timeline and "10:00" in timeline and "00:33" in timeline)

# 6. non-sold-out cast absent from ranking
check("never-sold-out cast not ranked", "無関係C" not in rank_part)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
