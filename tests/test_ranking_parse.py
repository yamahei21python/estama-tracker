"""Ranking parser fixture: one HTML page through scrape_area_ranking.

ranking_scraper.py had zero test references, so a silent change to the
CSS selectors would only show up as a quietly shrinking watchlist.
The fixture covers the real page shape plus the skip rules: PR slots
(no rank span), crown-image slots (non-numeric span), missing cast
links, empty cast names, and shop links that are image-only.

Run: python3 tests/test_ranking_parse.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "app"))

import ranking_scraper as r  # noqa: E402

NOW = datetime(2026, 10, 30, 9, 15, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
r.now_jst = lambda: NOW

HTML = """
<div class="therapist_ranking_list_wrap">
  <div class="therapist_ranking_list_item type1">
    <span class="ranknum">1</span>
    <a href="/shop/12/cast/345/">花子</a>
    <a href="/shop/12/">店A</a>
    <span class="cup">Dカップ</span>
    <div class="cast_st">T158 (24)</div>
  </div>
  <div class="therapist_ranking_list_item pr_item">
    <a href="/shop/99/cast/900/">PR広告嬢</a>
    <a href="/shop/99/">広告店</a>
  </div>
  <div class="therapist_ranking_list_item type1">
    <span class="rank-crown"><img src="/img/crown.png"></span>
    <a href="/shop/13/cast/346/">画像順位</a>
    <a href="/shop/13/">店B</a>
  </div>
  <div class="therapist_ranking_list_item type1">
    <span class="ranknum">4</span>
    <span>キャストリンクなし</span>
  </div>
  <div class="therapist_ranking_list_item type2">
    <span class="ranknum">2</span>
    <a href="/shop/14/cast/347/">すず</a>
    <span class="cup">Bカップ</span>
  </div>
  <div class="therapist_ranking_list_item type2">
    <span class="ranknum">6</span>
    <a href="/shop/15/cast/348/"><img src="/img/blank.png"></a>
    <a href="/shop/15/">店C</a>
  </div>
  <div class="therapist_ranking_list_item type1">
    <span class="ranknum">3</span>
    <a href="/shop/16/cast/349/">あや</a>
    <a href="/shop/16/"><img src="/img/shop.png"></a>
    <div class="cast_st">T162</div>
  </div>
</div>
"""


class FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def get(self, url, timeout=15):
        self.calls.append((url, timeout))
        return FakeResp(self.text)


fails = []


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)
        if detail:
            print("   ", detail)


scraper = r.RankingScraper.__new__(r.RankingScraper)
scraper.session = FakeSession(HTML)
data = scraper.scrape_area_ranking("2701", "weekly")

check("fetch used the area/period URL and a timeout",
      scraper.session.calls == [
          ("https://estama.jp/kansai/ranking/therapist/2701/weekly/", 15)])
check("area name from the table", data["area_name"] == "梅田・大阪駅")
check("clock pinned fetch fields",
      data["fetched_date"] == "2026-10-30"
      and data["fetched_at"] == "2026-10-30 09:15:00 JST")
check("only organic, linked casts are kept", data["count"] == 3,
      [x["cast_name"] for x in data["rankings"]])

got = {x["rank"]: x for x in data["rankings"]}
check("ranks 1/2/3", sorted(got) == [1, 2, 3], sorted(got))

first = got.get(1, {})
check("full profile parsed",
      (first.get("cast_name"), first.get("cast_id"), first.get("shop_id"),
       first.get("shop_name"), first.get("cup"), first.get("height"),
       first.get("age"))
      == ("花子", 345, 12, "店A", "Dカップ", 158, 24), first)
check("pr flag is always false for kept rows",
      all(x["is_pr"] is False for x in data["rankings"]))
check("profile/shop URLs built from ids",
      first.get("profile_url") == "https://estama.jp/shop/12/cast/345/"
      and first.get("shop_url") == "https://estama.jp/shop/12/")

second = got.get(2, {})
check("missing shop link becomes empty name",
      second.get("shop_name") == "" and second.get("cup") == "Bカップ")

third = got.get(3, {})
check("image-only shop link is ignored",
      third.get("shop_name") == "" and third.get("height") == 162
      and third.get("age") is None)

# round-trip into the real schema
with tempfile.TemporaryDirectory() as td:
    db = os.path.join(td, "r.db")
    n = r.import_rankings_into_db([data], db_path=db)
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT area_code, period_type, rank, cast_name, cup, height, age,"
        " shop_id, fetched_date FROM cast_rankings ORDER BY rank").fetchall()
    conn.close()

check("import inserts one row per kept cast", n == 3 and len(rows) == 3, rows)
check("imported fields match the parse",
      rows[0] == ("2701", "weekly", 1, "花子", "Dカップ", 158, 24, 12,
                  "2026-10-30"), rows[:1])

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
