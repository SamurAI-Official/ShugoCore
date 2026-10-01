"""Read-only SQLite inspection for a pulled ShugoCore device DB.

    python runtime/tools/sqlite_check.py <db-path>

Prints journal mode, page accounting, integrity_check and per-table row
counts. Used to decide whether a device DB snapshot is recoverable before
pushing it back to a phone (see the fleet re-key run in the CHANGELOG).
"""
import sqlite3
import sys


def main(argv):
    path = argv[1]
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    cur = con.cursor()
    print(f"path          : {path}")
    print(f"journal_mode  : {cur.execute('PRAGMA journal_mode').fetchone()[0]}")
    print(f"page_count    : {cur.execute('PRAGMA page_count').fetchone()[0]}")
    print(f"page_size     : {cur.execute('PRAGMA page_size').fetchone()[0]}")
    print(f"freelist      : {cur.execute('PRAGMA freelist_count').fetchone()[0]}")
    print(f"integrity     : {cur.execute('PRAGMA integrity_check').fetchone()[0]}")
    objects = cur.execute(
        "SELECT type, name FROM sqlite_master ORDER BY type, name").fetchall()
    print(f"objects       : {len(objects)}")
    for kind, name in objects:
        if kind != "table":
            print(f"  {kind:6} {name}")
    for kind, name in objects:
        if kind != "table":
            continue
        try:
            count = cur.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error as exc:      # noqa: BLE001 - report, don't fail
            count = f"ERR {exc}"
        print(f"  table  {name}: {count} rows")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
