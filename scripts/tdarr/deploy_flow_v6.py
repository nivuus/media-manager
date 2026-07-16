#!/usr/bin/env python3
"""Déploie le flow v6 en base Tdarr. Usage: deploy_flow_v6.py [--apply]  (dry-run par défaut)."""
import json, os, sqlite3, shutil, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "tdarr/server/Tdarr/DB2/SQL/database.db")
FLOW = os.path.join(ROOT, "tdarr/flows/OLED4K_norm_v6.json")
TEST_DIR = "/media/_tdarr_test"
DRY = "--apply" not in sys.argv

def main():
    with open(FLOW) as f:
        flow = json.load(f)
    print("== DRY-RUN (ajouter --apply pour écrire) ==" if DRY else "== APPLY ==")
    if not DRY:
        bak = f"{DB}.bak-{int(time.time())}"
        shutil.copy2(DB, bak); print(f"backup -> {bak}")

    con = sqlite3.connect(DB); con.row_factory = sqlite3.Row
    cur = con.cursor()
    ts = int(time.time() * 1000)

    print(f"flow upsert: {flow['_id']}")
    if not DRY:
        cur.execute("INSERT INTO flowsjsondb(id,timestamp,json_data) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET timestamp=excluded.timestamp, json_data=excluded.json_data",
                    (flow["_id"], ts, json.dumps(flow)))

    for row in cur.execute("SELECT id,json_data FROM librarysettingsjsondb").fetchall():
        data = json.loads(row["json_data"]); name = data.get("name", ""); changed = False
        if name in ("Movies", "TV Shows"):
            data["flowId"] = "OLED4K_norm_v6"; changed = True
            print(f"  {name}: flowId -> OLED4K_norm_v6")
        elif name.startswith("SAMPLE"):
            data["processLibrary"] = False; data["folder"] = TEST_DIR; changed = True
            print(f"  {name}: processLibrary=false, folder -> {TEST_DIR}")
        if changed and not DRY:
            cur.execute("UPDATE librarysettingsjsondb SET json_data=?,timestamp=? WHERE id=?",
                        (json.dumps(data), ts, row["id"]))

    if cur.execute("SELECT id FROM flowsjsondb WHERE id='2Op_kn3UU'").fetchone():
        print("  suppression flow résidu 2Op_kn3UU (DELETED_JUNK)")
        if not DRY:
            cur.execute("DELETE FROM flowsjsondb WHERE id='2Op_kn3UU'")

    if not DRY:
        con.commit(); print("commit DB OK")
    con.close()

if __name__ == "__main__":
    main()
