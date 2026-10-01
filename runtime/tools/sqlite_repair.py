"""Produce a clean, transaction-free copy of a ShugoCore device DB.

    python runtime/tools/sqlite_repair.py <src-db> <dst-db>

Why: a DB snapshotted from a *running* app can carry a hot rollback
journal. Restored onto a device whose app opens two connections, each
connection's read lock blocks the other's journal recovery and every
write fails with "database is locked" (SQLITE_BUSY). ``VACUUM INTO``
rewrites the logical content into a fresh file with no journal state,
which is safe to push back.
"""
import sqlite3
import sys


def main(argv):
    src, dst = argv[1], argv[2]
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=5)
    before = con.execute(
        "SELECT type, name FROM sqlite_master ORDER BY type, name").fetchall()
    counts = {}
    for kind, name in before:
        if kind == "table":
            counts[name] = con.execute(
                f'SELECT count(*) FROM "{name}"').fetchone()[0]
    con.execute("VACUUM INTO ?", (dst,))
    con.close()

    check = sqlite3.connect(f"file:{dst}?mode=ro", uri=True, timeout=5)
    after = {}
    for kind, name in check.execute(
            "SELECT type, name FROM sqlite_master ORDER BY type, name").fetchall():
        if kind == "table":
            after[name] = check.execute(
                f'SELECT count(*) FROM "{name}"').fetchone()[0]
    integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    journal = check.execute("PRAGMA journal_mode").fetchone()[0]
    check.close()

    print(f"src: {src}")
    print(f"dst: {dst}")
    print(f"integrity: {integrity}  journal_mode: {journal}")
    ok = True
    for name in sorted(set(counts) | set(after)):
        match = counts.get(name) == after.get(name)
        ok = ok and match
        print(f"  {name}: {counts.get(name)} -> {after.get(name)}"
              f"{'' if match else '   MISMATCH'}")
    print("row counts identical:", ok)
    return 0 if (ok and integrity == "ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
