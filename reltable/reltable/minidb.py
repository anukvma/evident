"""Build an in-memory database holding ONLY chosen rows of the original tables.

Shared by step 04 (gold evidence sufficiency) and the evaluation harness (sufficiency of a
system's retrieved rows). `ids_by_table[t]` is an iterable of rowids or the string "ALL".
"""
import sqlite3


def lenient(con):
    con.text_factory = lambda b: b.decode("utf-8", "replace")   # a few Spider rows are not valid UTF-8
    return con


def build_mini_db(src_path, tables, ids_by_table):
    mini = lenient(sqlite3.connect(":memory:", uri=True))
    mini.execute(f"ATTACH DATABASE 'file:{src_path}?mode=ro' AS src")
    for (ddl,) in mini.execute(
            "select sql from src.sqlite_master where type='table' and sql is not null "
            "and name not like 'sqlite_%'").fetchall():
        mini.execute(ddl)                     # every table exists (empty) so the SQL compiles
    mini.execute("CREATE TEMP TABLE _ids(id INTEGER PRIMARY KEY)")
    for t in tables:
        ids = ids_by_table.get(t)
        if not ids:
            continue
        if isinstance(ids, str) and ids.upper() == "ALL":
            mini.execute(f'INSERT INTO main."{t}" SELECT * FROM src."{t}"')
            continue
        mini.execute("DELETE FROM _ids")
        mini.executemany("INSERT OR IGNORE INTO _ids VALUES (?)", [(int(x),) for x in ids])
        mini.execute(f'INSERT INTO main."{t}" SELECT * FROM src."{t}" WHERE rowid IN (SELECT id FROM _ids)')
    mini.commit()
    mini.execute("DETACH DATABASE src")
    return mini
