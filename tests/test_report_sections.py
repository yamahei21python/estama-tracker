"""Characterization smoke tests for the report sections that had no
coverage before the stage-4 split (timing/area/shops/ranking/sellout
forecast) plus print_report end-to-end.

Runs on the shared deterministic fixture DB (tests/_fixture.py) and
asserts stable key lines, not the full output - the full output is
frozen by tests/test_report_golden.py.
Run: python3 tests/test_report_sections.py
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-sections-test-"))
atexit.register(shutil.rmtree, SCRATCH, True)

# evidence must never read the developer's real review/bakusai DBs here
os.environ["ESTAMA_REVIEW_DB"] = str(SCRATCH / "no-review.db")
os.environ["ESTAMA_BAKUSAI_DB"] = str(SCRATCH / "no-bakusai.db")

sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(HERE))

import _fixture  # noqa: E402
import estama_tracker as t  # noqa: E402
from common import connect_db  # noqa: E402

D0, D1, D2, D3 = _fixture.DATES

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def line_with(lines, needle):
    return next((ln for ln in lines if needle in ln), "")


DB = _fixture.build(SCRATCH / "estama.db")
conn = connect_db(DB)
t.rollup_daily(conn)  # daily_cast_status + forecast_eval

# ------------------------------------------------------------- timing
timing = t.section_timing(conn, list(_fixture.DATES))
check("timing: one line per day", len([ln for ln in timing if D0 in ln]) == 1)
d2_line = line_with(timing, D2)
check("timing: D2 sold-out count counts distinct cast-days",
      "sold_out=  4人" in d2_line)
check("timing: late-night (00-05) first sold-out is counted",
      "深夜(00-05時)=  1" in d2_line)
check("timing: regulars list has the 4-day seller",
      any("常連の完売時刻" in ln for ln in timing)
      and any("テスト梅田店 - 常連 れい" in ln for ln in timing))
check("timing: regular hour history printed",
      "10-23=10,10-24=10,10-25=10,10-26=10" in line_with(timing, "常連 れい"))

# --------------------------------------------------------------- area
area = t.section_area(conn, D3)
check("area: 梅田 ranked by sold-out rate",
      area[0].startswith("  梅田") and "sold_out=  1/   4 ( 25.0%)" in area[0])
check("area: unset area bucket exists",
      any(ln.startswith("  (未設定)") for ln in area))
check("area: 難波 sold_out 0/3", any(ln.startswith("  難波")
                                    and "0/   3" in ln for ln in area))

# -------------------------------------------------------------- shops
shops = t.section_shops(conn, D3)
check("shops: min_casts=3 excludes the 2-cast shop",
      not any("テスト堺店" in ln for ln in shops))
check("shops: 梅田 tops the rate ranking",
      shops[0].lstrip().startswith("テスト梅田店")
      and "1/  4 ( 25.0%)" in shops[0])
check("shops: three qualifying shops", len(shops) == 3)

# ------------------------------------------------------------ ranking
ranking = t.section_ranking(conn, D3)
check("ranking: latest fetched_date used",
      f"ranking fetched_date: {D3}" in ranking[0])
check("ranking: coverage line",
      "掲載 cast=3 / 観測=2 (67%)  sold_out=0" in ranking[1])
check("ranking: top10 comparison",
      "top10 cast=2 / 観測=2  sold_out=0" in ranking[2])
check("ranking: unregistered shop gap listed",
      "未登録店（ランキング掲載だが巡回対象外）" in line_with(ranking, "未登録店")
      and "900 未登録店X" in line_with(ranking, "900 未登録店X"))

# --------------------------------------------------- sellout forecast
fc = t.section_sellout_forecast(conn, D3, deep_shop_ids=set())
check("forecast: anchor comes from the latest checkpoint",
      "起点 15:00 / 4時間以内" in fc[0] and "n=8" in fc[0])
check("forecast: p>=50% candidates get a pooled probability",
      any(ln.startswith("  予測 57% テスト梅田店 - 葵 みさき") for ln in fc)
      and any(ln.startswith("  予測 57% テスト難波店 - 高橋 ゆい") for ln in fc))
check("forecast: candidate rows carry prior rate and tier",
      any("完売率 100% 3/3日, n=8, watch" in ln for ln in fc))
check("forecast: n<3 withholding message absent when n=8",
      "確率は出さない" not in "\n".join(fc))

# ------------------------------------------------------- print_report
buf = io.StringIO()
with redirect_stdout(buf):
    t.print_report(conn, D3, evidence=True)
out = buf.getvalue()
heads = ["【0】監視（", "【1】日次サマリー", "【2】完売ストリーク",
         "【3】アラート", "【4】完売予兆", "【4b】予約の勢い",
         "【4c】4時間以内", "【5】完売の初観測タイミング",
         "【6】キャスト出入り", "【7】エリア別", "【8】店舗別",
         "【9】週間ランキング照合", "【9b】ランキング変動",
         "【9c】新規cast", "【10】cast整合性", "【11】データカバレッジ",
         "【12】ウォッチ中", "【13】runner ジョブ履歴"]
check("print_report: all section headers present",
      all(h in out for h in heads))
check("print_report: monitor warnings come first",
      out.index("【0】監視") < out.index("checkpoint未達"))
check("print_report: both alerts printed with history",
      "ALERT テスト梅田店 - 葵 みさき" in out
      and "ALERT テスト難波店 - 高橋 ゆい" in out
      and "(完売 3/3日, 1.0)" in out)
check("print_report: slot-loss evidence attached",
      "19:00枠が 10-26 15:00 消失" in out)
check("print_report: rename detected",
      "同cast_id→複数名（改名）: 1組" in out)
check("print_report: transfer detected",
      "移籍 まい (id=999)" in out
      and "テスト天王寺店, テスト堺店" in out)
check("print_report: momentum line",
      "勢い テスト梅田店 - 白石 さち: 5枠消化" in out)
check("print_report: forecast self-score from rollup",
      "採点: low→完売 的中 0/2 (0%)" in out)

# empty DB path stays a clean message, not a traceback
empty = connect_db(SCRATCH / "empty.db")
buf = io.StringIO()
with redirect_stdout(buf):
    t.print_report(empty)
check("print_report: empty DB message", "snapshots が空です" in buf.getvalue())

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
