"""Tests for the runner's ban (prune) rule: a shop observed for
>=20 days (span) with >=15 observed days and zero sold_out casts is
removed from the watchlist, recorded in banned_shops.json, and kept
out even if a ranking sync re-adds it. History rows are kept.

Run: python3 tests/test_prune.py
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/tmp/estama-prune-test")
subprocess.run(["rm", "-rf", str(SCRATCH)])
(SCRATCH / "app").mkdir(parents=True)
os.environ["ESTAMA_VM_BASE"] = str(SCRATCH)

spec = importlib.util.spec_from_file_location("runner", ROOT / "runner.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

db = SCRATCH / "app" / "estama.db"
con = sqlite3.connect(db)
con.executescript((ROOT / "app" / "schema.sql").read_text())


def add(shop, day, status, ft="12:00"):
    con.execute(
        "INSERT INTO snapshots (shop_id, cast_name, status_norm, target_date,"
        " fetched_time) VALUES (?, 'cast', ?, ?, ?)", (shop, status, day, ft))


days20 = [f"2026-10-{d:02d}" for d in range(1, 21)]
sparse = [f"2026-09-{d:02d}" for d in range(22, 31)] + ["2026-10-15"]
for d in days20:
    add(90001, d, "available")                      # never sold out -> BAN
for d in days20:
    add(90002, d, "available")
add(90002, "2026-10-20", "sold_out", ft="13:00")     # sold out once -> keep
for d in days20[:5]:
    add(90003, d, "available")                      # 5 days only -> keep
for d in sparse:
    add(90004, d, "available")                      # sparse obs -> keep
con.commit()
con.close()

shops = {"shops": [{"shop_id": s, "name": f"shop{s}"} for s in (90001, 90002, 90003, 90004)]}
(SCRATCH / "app" / "shops.json").write_text(json.dumps(shops))

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        fails.append(name)


cands = r.prune_candidates(db, "2026-10-20")
check("only 90001 qualifies for ban", [c["shop_id"] for c in cands] == [90001])
r.apply_bans(cands, "2026-10-20")
watch = [s["shop_id"] for s in json.loads((SCRATCH / "app" / "shops.json").read_text())["shops"]]
banned = json.loads((SCRATCH / "app" / "banned_shops.json").read_text())["banned"]
check("watchlist drops banned shop", watch == [90002, 90003, 90004])
check("ban recorded with stats", len(banned) == 1 and banned[0]["shop_id"] == 90001
      and banned[0]["obs_days"] == 20)

doc = json.loads((SCRATCH / "app" / "shops.json").read_text())
doc["shops"].append({"shop_id": 90001, "name": "shop90001"})
(SCRATCH / "app" / "shops.json").write_text(json.dumps(doc))
dropped = r.enforce_ban()
watch = [s["shop_id"] for s in json.loads((SCRATCH / "app" / "shops.json").read_text())["shops"]]
check("enforce_ban re-removes resurrected shop", dropped == [90001] and 90001 not in watch)
hist = sqlite3.connect(db).execute(
    "SELECT COUNT(*) FROM snapshots WHERE shop_id=90001").fetchone()[0]
check("history rows of banned shop are kept", hist == 20)

print("\nRESULT:", "ALL PASS" if not fails else f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
