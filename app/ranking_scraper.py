#!/usr/bin/env python3
"""ranking_scraper.py - Estama (estama.jp) Popular Therapist Ranking Scraper

Scrapes weekly, daily, or monthly popular therapist rankings on estama.jp.
Supports:
- Osaka Prefecture overall (area 2700)
- Individual Osaka sub-areas (e.g. 2701: Umeda/Osaka Station, 2704: Namba, 2703: Nihonbashi, etc.)
- All 13 Osaka sub-areas batch scraping (--area all)
- SQLite database storage (cast_rankings table) & JSON snapshot export
- Watchlist integration (--sync-watchlist) to auto-track top casts' shops
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: 'requests' and 'beautifulsoup4' are required.", file=sys.stderr)
    print("Install them with: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)

from common import (BASE_DIR, DB_PATH as DEFAULT_DB, SCHEMA_PATH,
                    SHOPS_JSON_PATH as DEFAULT_SHOPS_JSON, connect_db,
                    load_json, make_session, save_json)

# Osaka Area Definitions on estama.jp
OSAKA_AREAS = {
    "2700": "大阪府 (全体総合)",
    "2701": "梅田・大阪駅",
    "2702": "心斎橋・長堀橋",
    "2703": "日本橋",
    "2704": "難波",
    "2705": "堺筋本町・本町・阿波座",
    "2706": "京橋・南森町・天満",
    "2707": "天王寺・谷町",
    "2708": "十三・新大阪・西中島",
    "2709": "堺・堺東・我孫子",
    "2710": "東大阪 (布施)",
    "2711": "江坂",
    "2712": "高槻・茨木",
    "2713": "その他大阪府"
}

# ANSI Colors
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_MAGENTA = "\033[95m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[2m"
COLOR_RESET = "\033[0m"


class RankingScraper:
    def __init__(self):
        self.session = make_session()

    def scrape_area_ranking(self, area_code="2701", period="weekly"):
        """Scrape ranking for a specific area (e.g. 2701: Umeda, 2700: Osaka)."""
        area_name = OSAKA_AREAS.get(str(area_code), f"Area {area_code}")
        url = f"https://estama.jp/kansai/ranking/therapist/{area_code}/{period}/"
        resp = self.session.get(url, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        # Match the actual list item class (both type1/type2 variants)
        items = soup.find_all("div", class_=re.compile(r"therapist_ranking_list_item\b"))
        # Deduplicate: keep outermost items only (avoid nested matches)
        seen_ids = set()
        unique_items = []
        for it in items:
            uid = id(it)
            if uid not in seen_ids:
                seen_ids.add(uid)
                unique_items.append(it)
        items = unique_items

        results = []
        for it in items:
            classes = it.get("class", [])
            classes_str = " ".join(classes)

            # --- Skip PR advertising slots ---
            # PR slots either carry an explicit 'pr_item' class OR lack a numeric rank span.
            # We detect them by the absence of a <span> containing only digits.
            rank_span = it.find("span", class_=re.compile(r"rank-crown|ranknum"))
            if not rank_span:
                continue  # No rank span at all → definitely PR / non-organic

            rm = re.search(r"^\s*(\d+)\s*$", rank_span.get_text())
            if not rm:
                continue  # Span exists but contains no plain number → PR crown image slot
            rank_num = int(rm.group(1))

            # Cast link & name
            cast_links = [a for a in it.find_all("a", href=re.compile(r"/shop/\d+/cast/\d+")) if a.get_text(strip=True)]
            if not cast_links:
                continue
            a_cast = cast_links[0]
            cast_name = a_cast.get_text(strip=True)
            m = re.search(r"/shop/(\d+)/cast/(\d+)", a_cast["href"])
            if not m:
                continue
            shop_id, cast_id = int(m.group(1)), int(m.group(2))

            # Shop link & name
            shop_links = [
                a.get_text(strip=True) for a in it.find_all("a", href=re.compile(r"/shop/\d+/?$"))
                if a.get_text(strip=True) and not a.find("img")
            ]
            shop_name = shop_links[0] if shop_links else ""

            # Cup size
            cup_tag = it.find(class_=re.compile(r"cup"))
            cup = cup_tag.get_text(strip=True) if cup_tag else None

            # Stats (height, age)
            st_div = it.find(class_=re.compile(r"cast_st|profile|status"))
            st_text = st_div.get_text(strip=True) if st_div else ""
            hm = re.search(r"T(\d+)", st_text)
            height = int(hm.group(1)) if hm else None
            am = re.search(r"\((\d+)\)", st_text)
            age = int(am.group(1)) if am else None

            results.append({
                "rank": rank_num,
                "is_pr": False,
                "cast_name": cast_name,
                "cast_id": cast_id,
                "age": age,
                "height": height,
                "cup": cup,
                "shop_name": shop_name,
                "shop_id": shop_id,
                "profile_url": f"https://estama.jp/shop/{shop_id}/cast/{cast_id}/",
                "shop_url": f"https://estama.jp/shop/{shop_id}/"
            })

        return {
            "area_code": str(area_code),
            "area_name": area_name,
            "period": period,
            "url": url,
            "fetched_date": datetime.now().strftime("%Y-%m-%d"),
            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S JST"),
            "count": len(results),
            "rankings": results
        }


def print_ranking_report(data):
    print("\n" + "=" * 78)
    print(f"{COLOR_BOLD}🏆 【{data['area_name']}】 セラピスト人気ランキング ({data['period'].upper()}){COLOR_RESET}")
    print(f"🔗 URL: {data['url']}")
    print(f"🕒 取得日時: {data['fetched_at']} | ランクイン件数: {data['count']}名")
    print("=" * 78)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for item in data["rankings"]:
        rank_val = item["rank"]
        if item["is_pr"]:
            rank_str = f"{COLOR_MAGENTA}[PR]{COLOR_RESET}"
        elif rank_val in medals:
            rank_str = f"{medals[rank_val]} {rank_val:2d}位"
        elif rank_val:
            rank_str = f"   {rank_val:2d}位"
        else:
            rank_str = "    -- "

        name_str = f"{COLOR_BOLD}{item['cast_name']}{COLOR_RESET}"
        details_list = []
        if item["age"]:
            details_list.append(f"{item['age']}歳")
        if item["cup"]:
            details_list.append(item["cup"])
        if item["height"]:
            details_list.append(f"T{item['height']}")
        detail_str = f"({', '.join(details_list)})" if details_list else ""

        shop_info = f"@ {item['shop_name']} (Shop: {item['shop_id']}, Cast: {item['cast_id']})"
        print(f"  {rank_str} {name_str:<18} {detail_str:<18} {COLOR_CYAN}{shop_info}{COLOR_RESET}")

    print("=" * 78 + "\n")


def import_rankings_into_db(data_list, db_path=DEFAULT_DB):
    """Save ranking results into SQLite cast_rankings table."""
    conn = connect_db(db_path)

    total_inserted = 0
    for data in data_list:
        area_code = data["area_code"]
        area_name = data["area_name"]
        period = data["period"]
        fdate = data["fetched_date"]
        fat = data["fetched_at"]

        for item in data["rankings"]:
            conn.execute(
                """
                INSERT OR REPLACE INTO cast_rankings (
                    area_code, area_name, period_type, rank, is_pr,
                    cast_id, cast_name, age, height, cup,
                    shop_id, shop_name, fetched_date, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    area_code, area_name, period, item["rank"], 1 if item["is_pr"] else 0,
                    item["cast_id"], item["cast_name"], item["age"], item["height"], item["cup"],
                    item["shop_id"], item["shop_name"], fdate, fat
                )
            )
            total_inserted += 1

    conn.commit()
    conn.close()
    return total_inserted


def sync_top_shops_to_watchlist(data_list, shops_json_path=DEFAULT_SHOPS_JSON, top_n=10):
    """Extract shops of top-ranked casts and add missing ones to shops.json."""
    current_data = load_json(shops_json_path, default={"shops": []})

    existing_ids = {int(s["shop_id"]) for s in current_data.get("shops", [])}
    new_shops_added = 0

    for data in data_list:
        for item in data["rankings"]:
            if item["is_pr"] or not item["rank"] or item["rank"] > top_n:
                continue
            sid = item["shop_id"]
            if sid not in existing_ids:
                existing_ids.add(sid)
                current_data.setdefault("shops", []).append({
                    "shop_id": sid,
                    "name": item["shop_name"],
                    "area": data["area_name"],
                    "source_url": f"https://estama.jp/shop/{sid}/schedule/"
                })
                new_shops_added += 1

    if new_shops_added > 0:
        save_json(shops_json_path, current_data)

    return new_shops_added


def main():
    parser = argparse.ArgumentParser(
        description="Scrape weekly popular therapist rankings on estama.jp (Osaka & sub-areas)."
    )
    parser.add_argument(
        "--area", type=str, default="2701",
        help="Area code: 2701 (梅田), 2700 (大阪府総合), or 'all' for all 13 Osaka areas (default: 2701)"
    )
    parser.add_argument(
        "--period", type=str, default="weekly", choices=["weekly", "daily", "monthly"],
        help="Ranking period: weekly, daily, monthly (default: weekly)"
    )
    parser.add_argument("--import-db", action="store_true", help="Import rankings into estama.db (cast_rankings table)")
    parser.add_argument("--save-json", action="store_true", help="Save ranking results to JSON snapshot files")
    parser.add_argument("--sync-watchlist", action="store_true", help="Add top ranked casts' shops to shops.json watchlist")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB), help=f"Database path (default: {DEFAULT_DB})")
    parser.add_argument("--json", action="store_true", help="Output JSON directly to stdout")

    args = parser.parse_args()

    scraper = RankingScraper()
    target_areas = []

    if args.area.lower() == "all":
        # All 13 sub-areas + overall
        target_areas = list(OSAKA_AREAS.keys())
    else:
        target_areas = [args.area]

    all_results = []
    for acode in target_areas:
        aname = OSAKA_AREAS.get(acode, acode)
        print(f"Scraping {aname} ({acode}) {args.period} ranking...", file=sys.stderr)
        data = scraper.scrape_area_ranking(area_code=acode, period=args.period)
        all_results.append(data)
        if len(target_areas) > 1:
            time.sleep(0.5)

    if args.json:
        print(json.dumps(all_results if len(all_results) > 1 else all_results[0], ensure_ascii=False, indent=2))
    else:
        for d in all_results:
            print_ranking_report(d)

    if args.save_json:
        for d in all_results:
            filename = BASE_DIR / f"ranking_{d['period']}_{d['area_code']}_{d['fetched_date']}.json"
            save_json(filename, d)
            print(f"{COLOR_GREEN}✅ Saved JSON:{COLOR_RESET} {filename}")

    if args.import_db:
        cnt = import_rankings_into_db(all_results, db_path=args.db)
        print(f"{COLOR_GREEN}✅ Successfully imported {cnt} ranking rows into {args.db}!{COLOR_RESET}")

    if args.sync_watchlist:
        added = sync_top_shops_to_watchlist(all_results)
        print(f"{COLOR_GREEN}✅ Synced watchlist: {added} new top-ranked shops added to shops.json!{COLOR_RESET}")


if __name__ == "__main__":
    main()
