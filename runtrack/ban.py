"""Shop ban policy."""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from runtrack.env import (APP, DB, SHOPS_JSON, BANNED_JSON, log,
                          BAN_AFTER_DAYS, BAN_MIN_OBS_DAYS)
from storage import storage_put
from runtrack.archive import expected_business_date
from common import now_jst

def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def shop_stats(con: sqlite3.Connection, shop_id: int) -> dict:
    first, last, days, total, sold = con.execute(
        """SELECT MIN(target_date), MAX(target_date),
                  COUNT(DISTINCT target_date), COUNT(*),
                  SUM(CASE WHEN status_norm='sold_out' THEN 1 ELSE 0 END)
           FROM snapshots WHERE shop_id=?""", (shop_id,)).fetchone()
    return {"first_seen": first, "last_seen": last, "obs_days": days or 0,
            "rows": total or 0, "sold_out": sold or 0}


def prune_candidates(db_path: Path, today_bd: str) -> list[dict]:
    """Shops observed for >= BAN_AFTER_DAYS with zero sold_out casts ever."""
    shops = _load_json(SHOPS_JSON, {"shops": []})["shops"]
    banned_ids = {b["shop_id"] for b in _load_json(BANNED_JSON, {"banned": []})["banned"]}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = []
    try:
        for shop in shops:
            sid = shop["shop_id"]
            if sid in banned_ids:
                continue
            st = shop_stats(con, sid)
            if not st["first_seen"]:
                continue  # no data at all: a fetch problem, not a verdict
            span = (datetime.fromisoformat(today_bd)
                    - datetime.fromisoformat(st["first_seen"])).days + 1
            if (span >= BAN_AFTER_DAYS and st["obs_days"] >= BAN_MIN_OBS_DAYS
                    and st["sold_out"] == 0):
                out.append({**shop, **st, "span_days": span})
    finally:
        con.close()
    return out


def apply_bans(candidates: list[dict], today_bd: str) -> None:
    shops_doc = _load_json(SHOPS_JSON, {"shops": []})
    banned_doc = _load_json(BANNED_JSON, {"banned": []})
    ids = {c["shop_id"] for c in candidates}
    shops_doc["shops"] = [s for s in shops_doc["shops"] if s["shop_id"] not in ids]
    for c in candidates:
        banned_doc["banned"].append({
            "shop_id": c["shop_id"], "name": c.get("name"), "banned_on": today_bd,
            "span_days": c["span_days"], "obs_days": c["obs_days"],
            "reason": "no sold_out cast in >=20 observed days",
        })
        log(f"BANNED shop {c['shop_id']} {c.get('name')}: observed "
            f"{c['obs_days']} days over {c['span_days']}d span, sold_out=0")
    SHOPS_JSON.write_text(json.dumps(shops_doc, ensure_ascii=False, indent=2))
    BANNED_JSON.write_text(json.dumps(banned_doc, ensure_ascii=False, indent=2))


def enforce_ban() -> list[int]:
    """Drop banned shops that a ranking sync may have re-added to shops.json."""
    banned_ids = {b["shop_id"] for b in _load_json(BANNED_JSON, {"banned": []})["banned"]}
    if not banned_ids:
        return []
    shops_doc = _load_json(SHOPS_JSON, {"shops": []})
    kept = [s for s in shops_doc["shops"] if s["shop_id"] not in banned_ids]
    dropped = [s["shop_id"] for s in shops_doc["shops"] if s["shop_id"] in banned_ids]
    if dropped:
        shops_doc["shops"] = kept
        SHOPS_JSON.write_text(json.dumps(shops_doc, ensure_ascii=False, indent=2))
        log(f"enforce_ban: re-removed banned shops {dropped}")
    return dropped
