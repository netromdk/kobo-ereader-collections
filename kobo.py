#!/usr/bin/env python3
import argparse
import os
import sqlite3
import sys
from pathlib import Path


DB_NAME = "KoboReader.sqlite"
KOBO_DIR = ".kobo"
ENV_VAR = "KOBO_DB"
ONBOARD_PREFIX = "file:///mnt/onboard/"


def is_kobo_db(path):
  try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cur.fetchall()}
    conn.close()
    required = {"Shelf", "ShelfContent", "content"}
    return required.issubset(tables)
  except sqlite3.Error:
    return False


def _candidates_from_dir(base):
  base_path = Path(base)
  return [
    base_path / DB_NAME,
    base_path / KOBO_DIR / DB_NAME,
  ]


def find_kobo_db(explicit=None, kobo_root=None):
  if explicit:
    p = Path(explicit).expanduser().resolve()
    if p.is_file() and is_kobo_db(p):
      return p
    raise SystemExit(f"Not a valid Kobo database: {p}")

  env = os.environ.get(ENV_VAR)
  if env:
    p = Path(env).expanduser().resolve()
    if p.is_file() and is_kobo_db(p):
      return p
    raise SystemExit(f"{ENV_VAR} is set but is not a valid Kobo database: {p}")

  start = Path(kobo_root).expanduser().resolve() if kobo_root else Path.cwd().resolve()

  for base in [start, *start.parents]:
    for p in _candidates_from_dir(base):
      if p.is_file() and is_kobo_db(p):
        return p

  for base in [start]:
    try:
      for child in base.iterdir():
        if not child.is_dir():
          continue
        p = child / KOBO_DIR / DB_NAME
        if p.is_file() and is_kobo_db(p):
          return p
    except PermissionError:
      pass

  raise SystemExit(
    f"Could not find {DB_NAME} starting from {start}. "
    f"Checked current/parents and immediate children. "
    f"Use --db PATH or set {ENV_VAR}."
  )


def get_mount_root(db_path):
  p = Path(db_path).resolve()
  if p.name == DB_NAME and p.parent.name == KOBO_DIR:
    return p.parent.parent
  return p.parent


def uri_to_local_path(content_id, mount_root):
  if content_id and content_id.startswith(ONBOARD_PREFIX):
    rel = content_id[len(ONBOARD_PREFIX):]
    if rel.startswith("/"):
      rel = rel[1:]
    return Path(mount_root) / rel
  return None


def cmd_collections_list(args):
  db_path = args.db_path
  mount_root = get_mount_root(db_path)
  conn = sqlite3.connect(str(db_path))
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()
  filter_name = getattr(args, "name", None)
  if filter_name:
    cur.execute(
      """
      SELECT Name, Id, CreationDate, LastModified, _IsDeleted, _IsVisible, _IsSynced
      FROM Shelf
      WHERE Name = ?
      ORDER BY Name
      """,
      (filter_name,),
    )
  else:
    cur.execute(
      """
      SELECT Name, Id, CreationDate, LastModified, _IsDeleted, _IsVisible, _IsSynced
      FROM Shelf
      ORDER BY Name
      """
    )
  shelves = cur.fetchall()
  for s in shelves:
    deleted = s["_IsDeleted"] in (1, "1", "true", True)
    if deleted:
      continue
    cur.execute(
      """
      SELECT sc.ContentId, sc.DateModified
      FROM ShelfContent sc
      WHERE sc.ShelfName = ?
        AND (sc._IsDeleted IS NULL OR sc._IsDeleted IN (0,'0','false'))
      """,
      (s["Name"],),
    )
    items = cur.fetchall()
    print(f"Collection: {s['Name']}")
    print(
      f"  Id: {s['Id']} | Created: {s['CreationDate']} | Modified: {s['LastModified']}"
    )
    print(
      f"  Visible: {s['_IsVisible']} | Synced: {s['_IsSynced']} | Books: {len(items)}"
    )
    if not items:
      print("  (no books)")
      print()
      continue
    for it in items:
      cid = it["ContentId"]
      cur.execute(
        """
        SELECT MAX(BookTitle) as BookTitle, MAX(Attribution) as Attribution
        FROM content
        WHERE ContentID = ? OR ContentID LIKE ?
        """,
        (cid, cid + "#%"),
      )
      info = cur.fetchone()
      title = info["BookTitle"] if info and info["BookTitle"] else "(unknown)"
      author = info["Attribution"] if info and info["Attribution"] else ""
      local = uri_to_local_path(cid, mount_root)
      exists = local.exists() if local else False
      line = f"  - {title}"
      if author:
        line += f" by {author}"
      line += f" | {cid}"
      line += f" | on_disk={exists}"
      if local:
        line += f" ({local})"
      print(line)
    print()
  if filter_name and not shelves:
    print(f"Collection not found: {filter_name}")
  conn.close()
  return 0


def build_parser():
  p = argparse.ArgumentParser(
    prog="kobo.py",
    description="Kobo database tool.",
  )
  p.add_argument("--db", help=f"Path to {DB_NAME}")
  p.add_argument("--kobo-root", help=f"Kobo mount root containing {KOBO_DIR}")
  sub = p.add_subparsers(dest="command")
  sp_col = sub.add_parser("collections", aliases=["c", "col", "cols"], help="List/show collections")
  sp_col.add_argument("name", nargs="?")
  sp_col.set_defaults(func=cmd_collections_list)
  return p


def main(argv=None):
  parser = build_parser()
  args = parser.parse_args(argv)
  db_path = find_kobo_db(explicit=args.db, kobo_root=args.kobo_root)
  args.db_path = db_path
  print(f"Using SQLite: {db_path}")
  if hasattr(args, "func"):
    print("---")
    return args.func(args)
  return 0


if __name__ == "__main__":
  sys.exit(main())
