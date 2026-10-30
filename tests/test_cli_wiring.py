"""CLI wiring for the stage-4 acceptance: rollup / report / dashboard /
notify must actually reach their implementation through `main()`.

The sections themselves are covered by test_report_sections; this file
only proves the argparse wiring and exit codes on the fixture DB.
Run: python3 tests/test_cli_wiring.py
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRATCH = Path(tempfile.mkdtemp(prefix="estama-cli-test-"))
atexit.register(shutil.rmtree, SCRATCH, True)

os.environ["ESTAMA_REVIEW_DB"] = str(SCRATCH / "no-review.db")
os.environ["ESTAMA_BAKUSAI_DB"] = str(SCRATCH / "no-bakusai.db")
os.environ.pop("ESTAMA_NOTIFY_WEBHOOK", None)

sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(HERE))

import _fixture  # noqa: E402
import estama_tracker as t  # noqa: E402

D3 = _fixture.DATES[-1]
DB = str(_fixture.build(SCRATCH / "estama.db"))

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = t.main(argv)
    return code, buf.getvalue()


code, out = run(["rollup", "--db", DB])
check("rollup: exit 0", code == 0)
check("rollup: writes daily_cast_status",
      "rollup: daily_cast_status 28 rows" in out)

code, out = run(["report", "--db", DB, "--date", D3, "--no-evidence"])
check("report: exit 0", code == 0)
check("report: renders the R3 summary head",
      "【1】日次サマリー（R3最終状態）" in out)
check("report: --no-evidence suppresses evidence lines",
      "枠が" not in out and "出典" not in out)

out_path = SCRATCH / "dash.html"
code, out = run(["dashboard", "--db", DB, "--date", D3,
                 "--out", str(out_path)])
check("dashboard: exit 0", code == 0)
check("dashboard: file written", out_path.exists())
html = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
check("dashboard: self-contained html",
      html.startswith("<!DOCTYPE html>") and "<style>" in html
      and "http" not in html)

code, out = run(["notify", "--db", DB, "--date", D3])
check("notify: exit 0", code == 0)
check("notify: prints the alert summary",
      f"[estama] {D3} レポート" in out
      and "■ 普段完売なのに空き:" in out
      and "葵 みさき" in out)
check("notify: reports missing webhook without failing",
      "no ESTAMA_NOTIFY_WEBHOOK set" in out)

code, out = run(["counts", "--db", DB, "--date", D3])
check("counts: exit 0 and R3 counts", code == 0 and "sold_out: 1" in out)

code, out = run(["alert", "--db", DB, "--date", D3])
check("alert: exit 0 with ALERT lines",
      code == 0 and "ALERT テスト梅田店 - 葵 みさき" in out)

code, out = run(["sold-out", "--db", DB, "--date", D3])
check("sold-out: lists the R3 sold-out cast",
      code == 0 and "テスト梅田店 - 常連 れい" in out)

code, out = run(["verify", "--db", DB])
check("verify: exit 0 and prints per-date counts",
      code == 0 and "shops in DB: 4" in out and f"{D3}: cast rows=" in out)

code, out = run(["status"])
check("status: exit 0 and reports the configured watchlist",
      code == 0 and "watchlist:" in out)

code, out = run(["sellout-forecast", "--db", DB, "--date", D3,
                 "--at", "15:00"])
check("sellout-forecast: exit 0 with the anchor line",
      code == 0 and "起点 15:00" in out)

code, out = run(["cast", "--db", DB, "--shop-id", "1",
                 "--cast-name", "常連 れい"])
check("cast: exit 0 with the profile header",
      code == 0 and "テスト梅田店 - 常連 れい (key=" in out)

code, out = run(["watch", "add", "--db", DB, "--shop-id", "1",
                 "--cast-name", "新規 れい", "--note", "cli"])
check("watch add: exit 0", code == 0 and "watch added:" in out)
code, out = run(["watch", "list", "--db", DB])
check("watch list: shows the added cast", code == 0 and "新規 れい" in out)
code, out = run(["watch", "remove", "--db", DB, "--shop-id", "1",
                 "--cast-name", "新規 れい"])
check("watch remove: exit 0", code == 0 and "1 row(s)" in out)

snap_path = SCRATCH / "snap.json"
snap_path.write_text(json.dumps({
    "snapshot_date": "2026-10-29",
    "collected_jst": "2026-10-29 10:30:00 JST",
    "fetched_time": "10:30",
    "shops": [
        {"shop_id": 1, "name": "テスト梅田店", "casts": [
            {"cast_name": "取込 れい",
             "status_raw": "ご好評につき本日完売"}]},
        {"shop_id": 99, "name": "外れ店", "casts": [
            {"cast_name": "対象外"}]},
    ],
}, ensure_ascii=False), encoding="utf-8")
shops_path = SCRATCH / "shops.json"
shops_path.write_text(json.dumps(
    {"shops": [{"shop_id": 1}]}), encoding="utf-8")
code, out = run(["load", str(snap_path), "--shops", str(shops_path),
                 "--db", DB])
check("load: inserts only watchlist shops",
      code == 0 and "'casts_inserted': 1" in out
      and "'shops_skipped_not_in_watchlist': 1" in out)

code, out = run(["demo"])
check("demo: exit 0 and proves the alert rule",
      code == 0 and "demo: alert logic OK" in out)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
