"""READ-ONLY: finds Nudge database files and shows which one holds your real data. Opens every file in read-only mode; changes, creates and deletes nothing.
Run from anywhere:   python check_db.py "C:\\path\\to\\Nudge"      (the folder that contains backend\\ and frontend\\)"""
import sqlite3, sys, time
from pathlib import Path
root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
skip = {"node_modules", ".venv", "venv", ".git", "__pycache__", "dist"}
found = []
def walk(d, depth=0):
    if depth > 3: return
    try: items = list(d.iterdir())
    except OSError: return
    for p in items:
        if p.is_dir():
            if p.name not in skip: walk(p, depth + 1)
        elif p.suffix in (".db", ".sqlite", ".sqlite3") or ".db.bak" in p.name: found.append(p)
walk(root)
if not found: print("No database files found under", root); sys.exit()
TABLES = ("leads", "post_opportunities", "drafts", "activities", "conversations", "conversation_messages", "scans", "issue_outreach")
for p in sorted(found, key=lambda x: x.stat().st_mtime, reverse=True):
    st = p.stat(); print(f"\n{p}\n  size {st.st_size:,} bytes | modified {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
    try:
        c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        have = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        print("  rows:", {t: c.execute(f"select count(*) from {t}").fetchone()[0] for t in TABLES if t in have} or "(no Nudge tables)")
        if "activities" in have: print("  newest activity:", c.execute("select max(timestamp) from activities").fetchone()[0])
        if "scans" in have: print("  newest scan:", c.execute("select max(started_at) from scans").fetchone()[0])
        if "leads" in have: print("  leads by source:", dict(c.execute("select source,count(*) from leads group by 1").fetchall()))
        if "post_opportunities" in have: print("  posts by source/status:", {f"{a}/{b}": n for a, b, n in c.execute("select source,status,count(*) from post_opportunities group by 1,2")})
        c.close()
    except Exception as e: print("  could not read:", e)
print("\nThe live database is normally backend\\outreach.db. The one with the newest activity/scan is the one you have been using.")