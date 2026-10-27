#!/usr/bin/env python3
"""estama_scraper.py - Estama (estama.jp) Schedule Scraper & Sold-Out Speed Analyzer

Scrapes schedule data for shops on estama.jp (e.g. shop 40900: SUPER AKUMA GIRLS).
Features:
- Time-series tracking: records exact fetch time (HH:MM) to track who sells out first.
- Strict today-only sold-out rule: future days never receive 'sold_out' to avoid decoy casts.
- Recommended checkpoints:
    00:30, 03:00, 05:00, 07:00, 09:00, 11:00,
    13:00, 15:00, 17:00, 19:00, 21:00, 23:00 (12 times / day)
- Speed ranking: analyzes who sells out earlier (the earlier, the more popular!).
- Shift-End (受付終了) vs. Sold-Out Distinction:
    When a shift ends (e.g. 16:00), status changes to '受付終了' (closed).
    A single nightly scrape cannot tell whether the cast sold out earlier or expired unsold.
    By inspecting historical checkpoints before shift-end, casts that hit 'sold_out' at any
    point during the day are correctly classified as sold-out for that day.
- Full SQLite (estama.db) & JSON snapshot integration.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
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
                    load_json, make_session, normalize_status_text, now_jst,
                    save_json)

DEFAULT_SHOP_ID = 40900  # 梅田高級メンズエステ SUPER AKUMA GIRLS

# Recommended execution checkpoints: hourly at :30 (24 per day).
# A full 74-shop run takes ~5 min, so hourly fits and doubles the
# sold-out-speed resolution vs. the old every-2-hours plan.
RECOMMENDED_CHECKPOINTS = [f"{h:02d}:30" for h in range(24)]

# ANSI Colors for terminal output
COLOR_RED = "\033[91m"
COLOR_GREEN = "\033[92m"
COLOR_YELLOW = "\033[93m"
COLOR_CYAN = "\033[96m"
COLOR_MAGENTA = "\033[95m"
COLOR_BOLD = "\033[1m"
COLOR_DIM = "\033[2m"
COLOR_RESET = "\033[0m"


def parse_shift_details(shift_str, slots=None):
    """Decompose shift string into start, end, work_minutes, work_hours.

    e.g. '12:00～20:00' -> ('12:00', '20:00', 480, 8.0)
    e.g. '18:00～27:30' -> ('18:00', '27:30', 570, 9.5)
    e.g. '19:00～翌5:00' -> ('19:00', '05:00', 600, 10.0)
    If shift text is missing/None but slots exist, estimate from active slot range.
    """
    if shift_str:
        s = shift_str.strip()
        m = re.match(r"^(\d{1,2}:\d{2})\s*[～~ー-]\s*(?:翌)?(\d{1,2}:\d{2})$", s)
        if m:
            start_s, end_s = m.group(1), m.group(2)
            sh, sm = map(int, start_s.split(":"))
            eh, em = map(int, end_s.split(":"))
            if "翌" in s or eh < sh:
                eh += 24
            start_mins = sh * 60 + sm
            end_mins = eh * 60 + em
            work_mins = end_mins - start_mins
            work_hours = round(work_mins / 60.0, 1)
            return start_s, end_s, work_mins, work_hours

    # Fallback to timetable slots if available
    if slots:
        active_times = [s[0] for s in slots if s[1] in ("○", "×")]
        if active_times:
            start_s = active_times[0]
            # Slot is 30 mins, so add 30 mins to last slot time
            last_t = active_times[-1]
            lh, lm = map(int, last_t.split(":"))
            end_mins = lh * 60 + lm + 30
            eh = end_mins // 60
            em = end_mins % 60
            end_s = f"{eh:02d}:{em:02d}"

            sh, sm = map(int, start_s.split(":"))
            start_mins = sh * 60 + sm
            work_mins = end_mins - start_mins
            work_hours = round(work_mins / 60.0, 1)
            return start_s, end_s, work_mins, work_hours

    return None, None, None, None


class EstamaScraper:
    def __init__(self, shop_id=DEFAULT_SHOP_ID):
        self.shop_id = int(shop_id)
        self.base_url = f"https://estama.jp/shop/{self.shop_id}/schedule/"
        self.ajax_url = "https://estama.jp/post/shop_schedule_ctrl"
        self.session = make_session(referer=self.base_url)
        self.csrf_name = None
        self.csrf_value = None
        self.shop_metadata = {}

    def init_session(self):
        """Fetch schedule top page to parse CSRF tokens and shop metadata."""
        resp = self.session.get(self.base_url, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # Extract CSRF token
        name_m = re.search(r'define\(\s*[\"\']CSRF_TOKEN_NAME[\"\']\s*,\s*[\"\']([^\"\']+)[\"\']\s*\)', html)
        val_m = re.search(r'define\(\s*[\"\']CSRF_TOKEN_VALUE[\"\']\s*,\s*[\"\']([^\"\']+)[\"\']\s*\)', html)
        if not (name_m and val_m):
            raise RuntimeError("Failed to extract CSRF tokens from schedule page.")
        self.csrf_name = name_m.group(1)
        self.csrf_value = val_m.group(1)

        # Parse shop metadata
        soup = BeautifulSoup(html, "html.parser")
        shop_name = ""
        area = ""
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string)
                if data.get("@type") == "BreadcrumbList":
                    items = sorted(data.get("itemListElement", []), key=lambda x: x.get("position", 0))
                    # Breadcrumb depth varies (6 or 7 levels) depending on
                    # whether the shop has a district-level area entry, so
                    # fixed positions are unreliable (with 6 levels, position
                    # 6 is the schedule page itself, labelled 出勤表).
                    # Identify the shop item by its URL (/shop/<id>/) instead;
                    # the item right before it is the most specific area.
                    for idx, item in enumerate(items):
                        raw_item = item.get("item")
                        if isinstance(raw_item, dict):
                            url = raw_item.get("@id") or raw_item.get("url") or ""
                        else:
                            url = raw_item or ""
                        if re.search(rf"/shop/{self.shop_id}/?$", str(url)):
                            shop_name = item.get("name", "").strip()
                            if idx > 0:
                                area = items[idx - 1].get("name", "").strip()
            except Exception:
                pass

        if shop_name in ("出勤表", "スケジュール"):
            shop_name = ""

        if not shop_name:
            h1 = soup.find("h1")
            if h1:
                t = h1.get_text(strip=True)
                shop_name = re.sub(r"^.*?-\s*", "", t)

        # Store status (e.g. ◎今すぐご案内可)
        store_status = ""
        reserve_link = soup.find("a", href=re.compile(r"/reserve/"))
        if reserve_link:
            st_text = reserve_link.get_text(separator=" ", strip=True)
            st_clean = re.sub(r"\s*予約・お問い合わせ.*$", "", st_text).strip()
            if st_clean and st_clean != "○":
                store_status = st_clean

        # Extract date tabs from top page
        date_tabs = []
        div_day = soup.find("div", class_="div_day")
        if div_day:
            for a in div_day.find_all("a", class_=re.compile(r"js-schedule-ctrl")):
                param_attr = a.get("data-param")
                tab_text = a.get_text(strip=True)
                if param_attr:
                    try:
                        p = json.loads(param_attr)
                        days_offset = int(p.get("days", 0))
                        date_tabs.append({
                            "days": days_offset,
                            "tab_text": tab_text,
                            "class": a.get("class", [])
                        })
                    except Exception:
                        pass

        self.shop_metadata = {
            "shop_id": self.shop_id,
            "name": shop_name or f"Shop {self.shop_id}",
            "area": area,
            "store_status": store_status,
            "source_url": self.base_url,
            "date_tabs": date_tabs,
        }
        return self.shop_metadata

    def fetch_day_schedule(self, days_offset=0, base_date=None, now_dt=None):
        """Fetch schedule for a specific day offset (0=today, 1=tomorrow, etc.).

        Paginates through all pages if more than 7 casts are scheduled.
        """
        if not self.csrf_name:
            self.init_session()

        if now_dt is None:
            now_dt = now_jst()

        if base_date is not None:
            target_date = (datetime.strptime(base_date, "%Y-%m-%d") + timedelta(days=days_offset)).strftime("%Y-%m-%d")
        else:
            # Resolve target_date directly from the site's date tabs
            # This ensures perfect synchronization with the site's business day changeover (around 05:00-06:00 AM)
            tab_match = next((t for t in self.shop_metadata.get("date_tabs", []) if t["days"] == days_offset), None)
            resolved_from_tab = None
            if tab_match:
                m = re.search(r'(\d+)/(\d+)', tab_match["tab_text"])
                if m:
                    month, day = int(m.group(1)), int(m.group(2))
                    cur_year = now_dt.year
                    # Handle year-end rollover (e.g. December to January)
                    if now_dt.month == 12 and month == 1:
                        cur_year += 1
                    elif now_dt.month == 1 and month == 12:
                        cur_year -= 1
                    resolved_from_tab = f"{cur_year:04d}-{month:02d}-{day:02d}"

            target_date = resolved_from_tab or (now_dt + timedelta(days=days_offset)).strftime("%Y-%m-%d")

        all_casts = []
        page = 0
        seen_ids = set()

        while True:
            post_data = {
                self.csrf_name: self.csrf_value,
                "view": "day",
                "days": str(days_offset),
                "week": "0",
                "shop_id": str(self.shop_id),
                "page": str(page)
            }
            resp = self.session.post(self.ajax_url, data=post_data, timeout=15)
            resp.raise_for_status()
            res_json = resp.json()
            html = res_json.get("html", "")
            if not html:
                break

            casts, has_next_page = self._parse_schedule_html(html, target_date, days_offset)
            for c in casts:
                key = (c["cast_id"], c["cast_name"])
                if key not in seen_ids:
                    seen_ids.add(key)
                    all_casts.append(c)

            if not has_next_page:
                break
            page += 1
            time.sleep(0.3)

        return {
            "target_date": target_date,
            "days_offset": days_offset,
            "casts": all_casts
        }

    def _parse_schedule_html(self, html, target_date, days_offset):
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if not table:
            return [], False

        thead = table.find("thead")
        tbody = table.find("tbody")
        if not thead or not tbody:
            return [], False

        cast_cols = []
        for th in thead.find_all("th")[1:]:
            name_raw = ""
            h4 = th.find("h4")
            if h4:
                name_raw = h4.get_text(strip=True)
            elif th.find("div", class_="item-name"):
                name_raw = th.find("div", class_="item-name").get_text(strip=True)

            m = re.match(r"^(.*?)(?:\((\d+)\))?$", name_raw)
            cast_name = m.group(1).strip() if m else name_raw
            age = int(m.group(2)) if (m and m.group(2)) else None

            link = th.find("a", class_=re.compile(r"link-detail|cast"))
            cast_id = None
            if link and link.get("href"):
                id_m = re.search(r"/cast/(\d+)/", link["href"])
                if id_m:
                    cast_id = int(id_m.group(1))

            date_div = th.find("div", class_="item-date")
            shift = None
            if date_div:
                shift = re.sub(r"\s+", "", date_div.get_text(strip=True))

            t_now = th.find("div", class_=re.compile(r"item-t_now|status"))
            status_raw = t_now.get_text(strip=True) if t_now else None

            cast_cols.append({
                "cast_name": cast_name,
                "cast_id": cast_id,
                "age": age,
                "shift": shift,
                "status_raw": status_raw,
                "slots": []
            })

        # Vertical key: td[data-idx] == cast_id. Prefer it over column
        # position so a trailing action cell (TEL) cannot shift marks.
        id_to_col = {}
        for i, c in enumerate(cast_cols):
            if c["cast_id"] is not None:
                id_to_col[str(c["cast_id"])] = i

        for tr in tbody.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            time_slot = cells[0].get_text(strip=True)
            for idx, cell in enumerate(cells[1:]):
                mark = cell.get_text(strip=True)
                target = id_to_col.get(cell.get("data-idx") or "")
                if target is None and idx < len(cast_cols):
                    target = idx
                if target is not None:
                    cast_cols[target]["slots"].append((time_slot, mark))

        final_casts = []
        for col in cast_cols:
            marks = [s[1] for s in col["slots"]]
            circles = marks.count("○")
            crosses = marks.count("×")
            dashes = marks.count("─")
            tels = sum(1 for m in marks if m.upper() == "TEL")

            raw = col["status_raw"]
            norm = self._normalize_status(raw, circles, crosses, days_offset)
            # All-TEL column = dummy cast (not real available/closed).
            # sold_out text still wins: real demand signal.
            if tels > 0 and circles == 0 and crosses == 0 and norm != "sold_out":
                norm = "dummy"

            if circles > 0:
                slot_pattern = f"○{circles}コマ / ×{crosses}コマ (予約可)"
                if tels:
                    slot_pattern += f" + TEL{tels}"
            elif crosses > 0:
                if days_offset == 0:
                    slot_pattern = f"○0コマ / ×{crosses}コマ (本日完売)"
                else:
                    slot_pattern = f"○0コマ / ×{crosses}コマ (全コマ×/予約不可)"
                if tels:
                    slot_pattern += f" + TEL{tels}"
            elif tels > 0 and dashes == 0:
                slot_pattern = f"全{tels}コマ TEL (電話のみ)"
            elif tels > 0:
                slot_pattern = f"全{dashes}コマ ─ + TEL{tels} (電話のみ混在)"
            else:
                slot_pattern = f"全{dashes}コマ ─ (受付終了/待機)"

            if not raw:
                if norm == "sold_out":
                    status_raw = f"本日完売(×{crosses})"
                elif norm in ("available", "low"):
                    status_raw = "予約受付中"
                elif crosses > 0 and circles == 0:
                    status_raw = "全コマ×(事前予約不可/おとり候補)"
                elif norm == "dummy" or (tels > 0 and circles == 0 and crosses == 0):
                    status_raw = "TELのみ"
                else:
                    status_raw = "時間のみ"
            else:
                status_raw = raw
                # Keep site text, but mark TEL-only in raw when norm is dummy
                # and the site text would otherwise look bookable.
                if norm == "dummy" and tels > 0 and circles == 0 and crosses == 0:
                    status_raw = f"{raw} / TELのみ"

            shift_start, shift_end, work_minutes, work_hours = parse_shift_details(col["shift"], col["slots"])

            final_casts.append({
                "cast_name": col["cast_name"],
                "cast_id": col["cast_id"],
                "age": col["age"],
                "shift": col["shift"],
                "shift_start": shift_start,
                "shift_end": shift_end,
                "work_minutes": work_minutes,
                "work_hours": work_hours,
                "status_raw": status_raw,
                "status_norm": norm,
                "slot_pattern": slot_pattern,
                "slots_summary": {
                    "open_circles": circles,
                    "reserved_crosses": crosses,
                    "off_dashes": dashes,
                    "tel_cells": tels
                }
            })

        has_next_page = False
        next_li = soup.find("li", class_="li_next")
        if next_li and next_li.find("a"):
            a_tag = next_li.find("a")
            classes = a_tag.get("class", [])
            if "btn-disable" not in classes:
                has_next_page = True

        return final_casts, has_next_page

    def _normalize_status(self, raw, circles, crosses, days_offset):
        """Map text & slot availability to normalized status:
        ('sold_out', 'closed', 'low', 'available', 'unknown', 'dummy')

        'dummy' is applied by the caller when the whole column is TEL.
        CRITICAL RULE:
        'sold_out' is strictly limited to TODAY (days_offset == 0).
        Future dates (days_offset >= 1) NEVER receive 'sold_out', because live
        shops only set '本日完売' on the day of service, and future full-cross slots
        frequently represent decoy/display-only casts rather than real demand.
        """
        # --- TODAY (days_offset == 0) ---
        if days_offset == 0:
            if raw:
                mapped = normalize_status_text(raw)
                if mapped != "unknown":
                    return mapped

            if circles > 0:
                return "low" if circles <= 2 else "available"
            if crosses == 0 and circles == 0:
                return "closed"
            return "unknown"

        # --- FUTURE DAYS (days_offset >= 1) ---
        if circles > 0:
            return "low" if circles <= 2 else "available"
        if crosses > 0 and circles == 0:
            return "unknown"
        if crosses == 0 and circles == 0:
            return "closed"

        return "unknown"

    def fetch_multi_days(self, num_days=1, base_date=None):
        """Fetch schedule days starting today. Default is today only:
        future days carry no real status (templated text, no sold_out)."""
        self.init_session()
        now_dt = now_jst()
        fetched_at = now_dt.strftime("%Y-%m-%d %H:%M:%S JST")
        fetched_time = now_dt.strftime("%H:%M")

        results = []
        for d in range(num_days):
            day_data = self.fetch_day_schedule(days_offset=d, base_date=base_date, now_dt=now_dt)
            results.append(day_data)

        return {
            "shop": self.shop_metadata,
            "fetched_at": fetched_at,
            "fetched_time": fetched_time,
            "days": results
        }


def format_status_badge(norm):
    if norm == "sold_out":
        return f"{COLOR_RED}【完売 SOLD OUT】{COLOR_RESET}"
    elif norm == "low":
        return f"{COLOR_YELLOW}【残りわずか LOW】{COLOR_RESET}"
    elif norm == "available":
        return f"{COLOR_GREEN}【予約可能 OPEN】{COLOR_RESET}"
    elif norm == "closed":
        return f"{COLOR_DIM}【受付終了 CLOSED】{COLOR_RESET}"
    elif norm == "dummy":
        return f"{COLOR_MAGENTA}【TELのみ DUMMY】{COLOR_RESET}"
    return f"{COLOR_CYAN}【未確定/不可 UNKNOWN】{COLOR_RESET}"


def print_summary_report(data):
    shop = data["shop"]
    print("\n" + "=" * 75)
    print(f"{COLOR_BOLD}🏪 店舗: {shop['name']} (ID: {shop['shop_id']}, エリア: {shop['area']}){COLOR_RESET}")
    print(f"📊 リアルタイム店舗状況: {COLOR_CYAN}{shop['store_status'] or '通常営業'}{COLOR_RESET}")
    print(f"🔗 URL: {shop['source_url']}")
    print(f"🕒 取得日時: {COLOR_BOLD}{data['fetched_at']}{COLOR_RESET} (時刻チェックポイント: {COLOR_YELLOW}{data['fetched_time']}{COLOR_RESET})")
    print("=" * 75)

    for day_info in data["days"]:
        offset = day_info["days_offset"]
        date_str = day_info["target_date"]
        label = "今日" if offset == 0 else ("明日" if offset == 1 else f"{offset}日後")
        casts = day_info["casts"]

        sold_outs = [c for c in casts if c["status_norm"] == "sold_out"]
        availables = [c for c in casts if c["status_norm"] in ("available", "low")]

        print(f"\n{COLOR_BOLD}📅 【{label}】 {date_str} (出勤: {len(casts)}人 | 完売: {len(sold_outs)}人 | 空き: {len(availables)}人){COLOR_RESET}")
        print("-" * 75)

        if not casts:
            print(f"  {COLOR_DIM}(出勤データなし / 休業日){COLOR_RESET}")
            continue

        for c in casts:
            badge = format_status_badge(c["status_norm"])
            age_str = f"({c['age']})" if c["age"] else ""
            if c["shift"] and c["work_hours"]:
                shift_str = f"[{c['shift']} ({c['work_hours']}h)]"
            elif c["shift"]:
                shift_str = f"[{c['shift']}]"
            else:
                shift_str = "[時間未定]"
            slots_info = f"○:{c['slots_summary']['open_circles']} ×:{c['slots_summary']['reserved_crosses']}"
            tels = c["slots_summary"].get("tel_cells", 0)
            if tels:
                slots_info += f" TEL:{tels}"

            print(f"  {badge:<24} {COLOR_BOLD}{c['cast_name']}{age_str}{COLOR_RESET:<14} {shift_str:<22} {slots_info:<12} (表示: {c['status_raw']})")

    print("\n" + "=" * 75)


def export_to_snapshots(data, output_dir=None):
    """Convert multi-day scrape result into estama-tracker compatible snapshot JSON files with fetch time and work hours."""
    if output_dir is None:
        output_dir = BASE_DIR
    else:
        output_dir = Path(output_dir)

    shop = data["shop"]
    time_tag = data["fetched_time"].replace(":", "")
    generated_files = []

    for day_info in data["days"]:
        date_str = day_info["target_date"]
        filename = output_dir / f"snapshot_{date_str}_{time_tag}_shop{shop['shop_id']}.json"

        casts_payload = []
        for c in day_info["casts"]:
            casts_payload.append({
                "cast_name": c["cast_name"],
                "cast_id": c["cast_id"],
                "age": c["age"],
                "shift": c["shift"],
                "shift_start": c.get("shift_start"),
                "shift_end": c.get("shift_end"),
                "work_minutes": c.get("work_minutes"),
                "work_hours": c.get("work_hours"),
                "status_raw": c["status_raw"],
                "slot_pattern": c["slot_pattern"]
            })

        payload = {
            "snapshot_date": date_str,
            "collected_jst": data["fetched_at"],
            "fetched_time": data["fetched_time"],
            "shops": [
                {
                    "shop_id": shop["shop_id"],
                    "name": shop["name"],
                    "area": shop["area"],
                    "store_status": shop["store_status"],
                    "source_url": shop["source_url"],
                    "has_table": len(day_info["casts"]) > 0,
                    "casts": casts_payload
                }
            ]
        }

        save_json(filename, payload)
        generated_files.append(filename)

    return generated_files


def import_into_db(data, db_path=DEFAULT_DB):
    """Import multi-day scrape result directly into SQLite estama.db with fetched_time and work hours."""
    conn = connect_db(db_path)

    shop = data["shop"]
    conn.execute(
        """
        INSERT OR REPLACE INTO shops (shop_id, name, area, store_status, source_url)
        VALUES (?, ?, ?, ?, ?)
        """,
        (shop["shop_id"], shop["name"], shop["area"], shop["store_status"], shop["source_url"])
    )

    total_inserted = 0
    fetched_time = data.get("fetched_time", now_jst().strftime("%H:%M"))
    for day_info in data["days"]:
        target_date = day_info["target_date"]
        for c in day_info["casts"]:
            conn.execute(
                """
                INSERT OR REPLACE INTO snapshots (
                    shop_id, cast_name, cast_id, age, shift,
                    shift_start, shift_end, work_minutes, work_hours,
                    status_raw, status_norm, slot_pattern, target_date, fetched_at, fetched_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shop["shop_id"],
                    c["cast_name"],
                    c["cast_id"],
                    c["age"],
                    c["shift"],
                    c.get("shift_start"),
                    c.get("shift_end"),
                    c.get("work_minutes"),
                    c.get("work_hours"),
                    c["status_raw"],
                    c["status_norm"],
                    c["slot_pattern"],
                    target_date,
                    data["fetched_at"],
                    fetched_time
                )
            )
            total_inserted += 1

    conn.commit()
    conn.close()
    return total_inserted


def show_speed_ranking(date_str=None, db_path=DEFAULT_DB, shop_id=DEFAULT_SHOP_ID):
    """Analyze and display which casts sold out earliest on a given date."""
    if date_str is None:
        date_str = now_jst().strftime("%Y-%m-%d")

    conn = connect_db(db_path, apply_schema=False)
    cur = conn.cursor()

    # Query earliest sold-out time for each cast on target date
    query = """
    SELECT 
        cast_name,
        MIN(fetched_time) as first_sold_out_time,
        COUNT(*) as total_records,
        MAX(shift) as shift,
        MAX(work_hours) as work_hours
    FROM snapshots
    WHERE target_date = ? AND shop_id = ? AND status_norm = 'sold_out'
    GROUP BY cast_name
    ORDER BY first_sold_out_time ASC, work_hours DESC
    """
    cur.execute(query, (date_str, shop_id))
    sold_out_rows = cur.fetchall()

    # Query all active checkpoints on that date
    cur.execute(
        "SELECT DISTINCT fetched_time FROM snapshots WHERE target_date = ? AND shop_id = ? ORDER BY fetched_time",
        (date_str, shop_id)
    )
    recorded_times = [r[0] for r in cur.fetchall()]

    print("\n" + "=" * 75)
    print(f"{COLOR_BOLD}⚡ 完売スピード分析ランキング (日付: {date_str} / 店舗ID: {shop_id}){COLOR_RESET}")
    print(f"📊 記録済みチェックポイント ({len(recorded_times)}回): {', '.join(recorded_times) if recorded_times else 'なし'}")
    print("=" * 75)

    if not sold_out_rows:
        print(f"  {COLOR_DIM}{date_str} に完売検知されたキャストはまだありません。{COLOR_RESET}")
    else:
        medals = ["🥇", "🥈", "🥉"]
        for idx, (name, first_time, rec_cnt, shift, work_hours) in enumerate(sold_out_rows, 1):
            rank_str = medals[idx - 1] if idx <= 3 else f"{idx:2d}位"
            speed_badge = (
                f"{COLOR_RED}【超速完売 ⚡ プレミア】{COLOR_RESET}" if first_time <= "12:00"
                else (f"{COLOR_YELLOW}【夕方完売 🔥 人気】{COLOR_RESET}" if first_time <= "17:00"
                else f"{COLOR_CYAN}【夜間完売】{COLOR_RESET}")
            )
            if shift and work_hours:
                hours_tag = f"{COLOR_MAGENTA}[ロング {work_hours}h]{COLOR_RESET}" if work_hours >= 8.0 else f"[{work_hours}h]"
                shift_info = f"{hours_tag} {shift}"
            elif shift:
                shift_info = f"[{shift}]"
            else:
                shift_info = "[シフト時間未定]"

            print(f"  {rank_str} {COLOR_BOLD}{name:<12}{COLOR_RESET} {speed_badge:<26} 完売検知: {COLOR_BOLD}{first_time}{COLOR_RESET}  {shift_info}")

    # Show timeline matrix if multiple times are recorded
    if len(recorded_times) > 1:
        print("\n" + "-" * 75)
        print(f"{COLOR_BOLD}📈 時系列タイムライン（各チェックポイントでの推移）{COLOR_RESET}")
        print("-" * 75)
        # Fetch status matrix
        cur.execute(
            """
            SELECT cast_name, fetched_time, status_norm
            FROM snapshots
            WHERE target_date = ? AND shop_id = ?
            ORDER BY cast_name, fetched_time
            """,
            (date_str, shop_id)
        )
        matrix = {}
        for cname, ftime, norm in cur.fetchall():
            if cname not in matrix:
                matrix[cname] = {}
            matrix[cname][ftime] = norm

        header = f"  {'キャスト名':<12} " + " ".join([f"{t:>6}" for t in recorded_times])
        print(COLOR_DIM + header + COLOR_RESET)

        for cname, tdict in matrix.items():
            row_items = []
            for t in recorded_times:
                st = tdict.get(t, "-")
                if st == "sold_out":
                    row_items.append(f"{COLOR_RED}完売{COLOR_RESET}")
                elif st in ("available", "low"):
                    row_items.append(f"{COLOR_GREEN}空き{COLOR_RESET}")
                elif st == "closed":
                    row_items.append(f"{COLOR_DIM}終了{COLOR_RESET}")
                else:
                    row_items.append(f"{COLOR_CYAN}不明{COLOR_RESET}")
            print(f"  {cname:<12} " + "   ".join(row_items))

    print("=" * 75 + "\n")
    conn.close()


def print_cron_guide():
    """Print cron schedule instructions for user."""
    print("\n" + "=" * 75)
    print(f"{COLOR_BOLD}⏰ 推奨実行スケジュール（1日24回・1時間おき、毎時30分）{COLOR_RESET}")
    print("=" * 75)
    print("実行時刻の目安:")
    for cp in RECOMMENDED_CHECKPOINTS:
        print(f"  - {cp} JST")

    print("\n【理由】: 朝早く売り切れるキャストほど超人気！何時に売り切れたかを追跡可能。")
    print("\n【本番の実行方法】スケジュール実行は runner.py 経由が前提:")
    print("  runner.py がロック・整合性チェック・R2アップロード・営業日アーカイブを担当する。")
    runner_path = BASE_DIR.parent / "runner.py"
    print(f"{COLOR_CYAN}# チェックポイント（毎時30分）{COLOR_RESET}")
    print(f"30 * * * * python3 {runner_path} checkpoint")
    print(f"{COLOR_CYAN}# ランキング＋ウォッチリスト同期＋出禁判定（毎日 06:15）{COLOR_RESET}")
    print(f"15 6 * * * python3 {runner_path} ranking")
    print("\n【直接実行する場合】(手動・検証用):")
    script_path = BASE_DIR / "estama_scraper.py"
    print(f"python3 {script_path} --all-shops --days 1 --import-db")
    print("=" * 75 + "\n")


def load_shops_json(shops_json_path=DEFAULT_SHOPS_JSON):
    """Load shop list from shops.json. Returns list of {shop_id, name, area, source_url}."""
    p = Path(shops_json_path)
    if not p.exists():
        print(f"Error: {shops_json_path} not found.", file=sys.stderr)
        sys.exit(1)
    data = load_json(p, default={})
    return data.get("shops", [])


def main():
    parser = argparse.ArgumentParser(
        description="Scrape and analyze estama.jp schedule & track sold-out speed across hourly checkpoints."
    )
    parser.add_argument("--shop-id", type=int, default=DEFAULT_SHOP_ID, help=f"Shop ID (default: {DEFAULT_SHOP_ID})")
    parser.add_argument("--shop-url", type=str, help="Full shop schedule URL (overrides --shop-id)")
    parser.add_argument("--all-shops", action="store_true", help=f"Scrape ALL shops listed in shops.json (default: {DEFAULT_SHOPS_JSON})")
    parser.add_argument("--shops-json", type=str, default=str(DEFAULT_SHOPS_JSON), help=f"Path to shops.json (used with --all-shops)")
    parser.add_argument("--days", type=int, default=1, help="Number of days to scrape (default: 1: today only; future days carry no real sold-out status)")
    parser.add_argument("--save-snapshots", action="store_true", help="Save as estama-tracker compatible snapshot JSON files with time tag")
    parser.add_argument("--import-db", action="store_true", help="Import scraped records directly into estama.db with fetched_time")
    parser.add_argument("--db", type=str, default=str(DEFAULT_DB), help=f"Database path (default: {DEFAULT_DB})")
    parser.add_argument("--json", action="store_true", help="Output full JSON instead of formatted report")
    parser.add_argument("--speed-rank", action="store_true", help="Show sold-out speed ranking and timeline for today")
    parser.add_argument("--date", type=str, help="Target date for --speed-rank (YYYY-MM-DD)")
    parser.add_argument("--cron-guide", action="store_true", help="Display the recommended schedule (hourly checkpoints via runner.py)")

    args = parser.parse_args()

    if args.cron_guide:
        print_cron_guide()
        return

    if args.speed_rank:
        show_speed_ranking(date_str=args.date, db_path=args.db, shop_id=args.shop_id)
        return

    # --- 全店一括巡回モード ---
    if args.all_shops:
        shops = load_shops_json(args.shops_json)
        total_shops = len(shops)
        total_imported = 0
        total_snapshot_files = []
        all_data = []

        print(f"{COLOR_BOLD}🏪 全店一括巡回モード: {total_shops}店舗 × {args.days}日分{COLOR_RESET}", file=sys.stderr)
        print("=" * 75, file=sys.stderr)

        for idx, shop_entry in enumerate(shops, 1):
            shop_id = int(shop_entry["shop_id"])
            shop_name = shop_entry.get("name", f"Shop {shop_id}")
            print(f"[{idx:2d}/{total_shops}] {COLOR_CYAN}{shop_name}{COLOR_RESET} (ID: {shop_id})", file=sys.stderr)

            try:
                scraper = EstamaScraper(shop_id=shop_id)
                data = scraper.fetch_multi_days(num_days=args.days)

                if not args.json:
                    print_summary_report(data)
                else:
                    all_data.append(data)

                if args.save_snapshots:
                    files = export_to_snapshots(data)
                    total_snapshot_files.extend(files)
                    print(f"  {COLOR_GREEN}✅ Snapshot: {len(files)}ファイル保存{COLOR_RESET}", file=sys.stderr)

                if args.import_db:
                    count = import_into_db(data, db_path=args.db)
                    total_imported += count
                    print(f"  {COLOR_GREEN}✅ DB: {count}件 インポート{COLOR_RESET}", file=sys.stderr)

            except Exception as e:
                print(f"  {COLOR_RED}❌ エラー: {e}{COLOR_RESET}", file=sys.stderr)

            # 店舗間インターバル (最後の店舗以外)
            if idx < total_shops:
                time.sleep(0.8)

        # 全店完了サマリー
        print("\n" + "=" * 75, file=sys.stderr)
        print(f"{COLOR_BOLD}🎉 全店巡回完了: {total_shops}店舗{COLOR_RESET}", file=sys.stderr)
        if args.import_db:
            print(f"{COLOR_GREEN}✅ 総インポート件数: {total_imported}件 -> {args.db}{COLOR_RESET}")
        if args.save_snapshots:
            print(f"{COLOR_GREEN}✅ 総スナップショット: {len(total_snapshot_files)}ファイル{COLOR_RESET}")
        if args.json:
            print(json.dumps(all_data, ensure_ascii=False, indent=2))
        return

    # --- 単店舗モード ---
    shop_id = args.shop_id
    if args.shop_url:
        m = re.search(r"/shop/(\d+)/", args.shop_url)
        if m:
            shop_id = int(m.group(1))
        else:
            print(f"Error: Could not extract shop ID from URL: {args.shop_url}", file=sys.stderr)
            sys.exit(1)

    print(f"Fetching schedule for shop_id={shop_id} for {args.days} days...", file=sys.stderr)
    scraper = EstamaScraper(shop_id=shop_id)
    data = scraper.fetch_multi_days(num_days=args.days)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print_summary_report(data)

    if args.save_snapshots:
        files = export_to_snapshots(data)
        print(f"\n{COLOR_GREEN}✅ Saved {len(files)} snapshot files with time tags:{COLOR_RESET}")
        for f in files:
            print(f"  - {f}")

    if args.import_db:
        count = import_into_db(data, db_path=args.db)
        print(f"\n{COLOR_GREEN}✅ Successfully imported {count} cast rows (checkpoint: {data['fetched_time']}) into {args.db}!{COLOR_RESET}")


if __name__ == "__main__":
    main()
