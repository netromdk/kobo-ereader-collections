#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DB_NAME = "KoboReader.sqlite"
KOBO_DIR = ".kobo"
ENV_VAR = "KOBO_DB"
ONBOARD_PREFIX = "file:///mnt/onboard/"
CALIBRE_METADATA = "metadata.calibre"

def now_kobo():
  return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    f"Could not find {DB_NAME} starting from {start}.\n"
    f"Use `--db PATH` or set {ENV_VAR}."
  )


def is_shelf_deleted(row):
  if row is None:
    return True
  try:
    v = row["_IsDeleted"]
  except Exception:
    try:
      v = row[5]
    except Exception:
      return False
  if isinstance(v, bool):
    return v
  if isinstance(v, int):
    return v != 0
  if isinstance(v, str):
    s = v.strip().lower()
    if s in ("true", "1", "yes"):
      return True
    if s in ("false", "0", "no", ""):
      return False
    return False
  return False


def is_shelf_content_deleted(row):
  if row is None:
    return True
  try:
    v = row["_IsDeleted"]
  except Exception:
    return True
  if isinstance(v, bool):
    return v
  if isinstance(v, int):
    return v != 0
  if isinstance(v, str):
    s = v.strip().lower()
    if s in ("true", "1", "yes"):
      return True
    if s in ("false", "0", "no", ""):
      return False
    return False
  return True


def get_non_deleted_shelves(cur):
  cur.execute("SELECT Name, Id, _IsDeleted FROM Shelf")
  rows = cur.fetchall()
  result = []
  for s in rows:
    if is_shelf_deleted(s):
      continue
    result.append(s)
  return result


def shelf_exists(cur, name):
  cur.execute(
    """
    SELECT 1 FROM Shelf
    WHERE Name = ? AND
          (_IsDeleted IS NULL OR _IsDeleted IN (0,'0','false'))
    """,
    (name,),
  )
  return cur.fetchone() is not None


def create_shelf(cur, name, now):
  cur.execute(
    """
    INSERT INTO Shelf (CreationDate, Id, InternalName, LastModified, Name, Type, _IsDeleted,
                       _IsVisible, _IsSynced)
    VALUES (?, ?, ?, ?, ?, '', 'false', 'true', 'false')
    """,
    (now, name, name, now, name),
  )


def delete_shelf_soft(cur, name, now):
  cur.execute(
    """
    UPDATE Shelf SET _IsDeleted = 'true', LastModified = ?, _IsSynced = 'false'
    WHERE Name = ?
    """,
    (now, name),
  )
  cur.execute(
    """
    UPDATE ShelfContent SET _IsDeleted = 'true', DateModified = ?, _IsSynced = 'false'
    WHERE ShelfName = ?
    """,
    (now, name),
  )


def membership_exists(cur, shelf_name, cid):
  cur.execute(
    """
    SELECT 1 FROM ShelfContent
    WHERE ShelfName = ? AND ContentId = ? AND
          (_IsDeleted IS NULL OR _IsDeleted IN (0,'0','false'))
    """,
    (shelf_name, cid),
  )
  return cur.fetchone() is not None


def add_to_shelf(cur, shelf_name, cid, now):
  cur.execute(
    """
    INSERT OR REPLACE INTO ShelfContent
      (ShelfName, ContentId, DateModified, _IsDeleted, _IsSynced)
    VALUES (?, ?, ?, 'false', 'false')
    """,
    (shelf_name, cid, now),
  )
  cur.execute("UPDATE Shelf SET LastModified = ? WHERE Name = ?", (now, shelf_name))


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


def find_metadata_calibre(mount_root):
  p = Path(mount_root) / CALIBRE_METADATA
  if p.is_file():
    return p
  return None


def load_metadata_calibre(path):
  with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
  return data


def find_file_on_mount(mount_root, lpath):
  if not lpath:
    return None
  p1 = Path(mount_root) / lpath
  if p1.is_file():
    return p1
  base = os.path.basename(lpath)
  p2 = Path(mount_root) / base
  if p2.is_file():
    return p2
  for root, dirs, files in os.walk(mount_root):
    if base in files:
      return Path(root) / base
  return None


def file_to_content_id(local_path, mount_root):
  try:
    rel = local_path.resolve().relative_to(mount_root.resolve())
  except Exception:
    return None
  rel_str = str(rel).replace("\\", "/")
  return ONBOARD_PREFIX + rel_str


def get_book_info(cur, cid):
  if not cid:
    return None, None
  cur.execute(
    """
    SELECT BookTitle, Attribution
    FROM content
    WHERE ContentID = ?
    ORDER BY CASE WHEN ContentID LIKE '%#%' OR ContentID LIKE '%!%' THEN 1 ELSE 0 END
    LIMIT 1
    """,
    (cid,),
  )
  row = cur.fetchone()
  if row and row["BookTitle"]:
    return row["BookTitle"], row["Attribution"]
  for sep in ["#", "!"]:
    cur.execute(
      """
      SELECT BookTitle, Attribution
      FROM content
      WHERE ContentID LIKE ?
      ORDER BY LENGTH(ContentID)
      LIMIT 1
      """,
      (cid + sep + "%",),
    )
    row = cur.fetchone()
    if row and row["BookTitle"]:
      return row["BookTitle"], row["Attribution"]
  no_scheme = cid
  if no_scheme.startswith("file://"):
    no_scheme = no_scheme[7:]
  if no_scheme and no_scheme != cid:
    cur.execute(
      """
      SELECT BookTitle, Attribution
      FROM content
      WHERE ContentID = ? OR ContentID LIKE ? OR ContentID LIKE ?
      ORDER BY CASE WHEN ContentID LIKE '%#%' OR ContentID LIKE '%!%' THEN 1 ELSE 0 END
      LIMIT 1
      """,
      (no_scheme, no_scheme + "#%", no_scheme + "!%"),
    )
    row = cur.fetchone()
    if row and row["BookTitle"]:
      return row["BookTitle"], row["Attribution"]
  base = os.path.basename(cid)
  if base:
    cur.execute(
      """
      SELECT BookTitle, Attribution
      FROM content
      WHERE ContentID LIKE ?
      LIMIT 1
      """,
      ("%" + base + "%",),
    )
    row = cur.fetchone()
    if row and row["BookTitle"]:
      return row["BookTitle"], row["Attribution"]
  return None, None


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
    shelves = cur.fetchall()
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
    deleted = is_shelf_deleted(s)
    if deleted and filter_name is None:
      continue
    if filter_name and deleted:
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
      title, author = get_book_info(cur, cid)
      if not title:
        title = "(unknown)"
      if not author:
        author = ""
      local = uri_to_local_path(cid, mount_root)
      if not local:
        if cid and cid.startswith("/"):
          local = Path(mount_root) / cid.lstrip("/")
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


def plan_tags_to_collections(db_path, mount_root, books, ignore):
  wanted_tags = set()
  for b in books:
    for t in (b.get("tags") or []):
      if t and t not in ignore:
        wanted_tags.add(t)
  conn = sqlite3.connect(str(db_path))
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()
  shelves = get_non_deleted_shelves(cur)
  to_delete = []
  for s in shelves:
    name = s["Name"]
    if name not in wanted_tags:
      to_delete.append(name)
  to_create = set()
  to_add = []
  seen_add = set()
  for b in books:
    tags = [t for t in (b.get("tags") or []) if t and t not in ignore]
    if not tags:
      continue
    lpath = b.get("lpath")
    local = find_file_on_mount(mount_root, lpath)
    if not local:
      continue
    cid = file_to_content_id(local, mount_root)
    if not cid:
      continue
    for tag in tags:
      if not shelf_exists(cur, tag):
        to_create.add(tag)
      if not membership_exists(cur, tag, cid):
        key = (tag, cid)
        if key not in seen_add:
          to_add.append(key)
          seen_add.add(key)
  conn.close()
  return {
    "wanted_tags": wanted_tags,
    "to_delete": sorted(set(to_delete)),
    "to_create": sorted(list(to_create)),
    "to_add": to_add,
    "has_destructive": len(to_delete) > 0,
  }


def apply_tags_to_collections(plan, db_path, dry_run_flag, force):
  if dry_run_flag:
    for name in plan["to_delete"]:
      print(f"Would remove collection: {name}")
    for tag in plan["to_create"]:
      print(f"Would create collection: {tag}")
    for tag, cid in plan["to_add"]:
      print(f"Would add {cid} to {tag}")
    print("Dry run complete")
    return 0
  has_changes = (len(plan["to_delete"]) > 0 or
                 len(plan["to_create"]) > 0 or
                 len(plan["to_add"]) > 0)
  if not has_changes:
    print("No changes needed")
    return 0
  if plan["has_destructive"] and not force:
    print("Destructive changes detected:")
    for name in plan["to_delete"]:
      print(f"  Would remove collection: {name}")
    print("Use --yes to apply destructive changes")
    return 1
  backup = str(db_path) + ".bak"
  shutil.copy2(db_path, backup)
  print(f"Backed up to {backup}")
  conn = sqlite3.connect(str(db_path))
  conn.row_factory = sqlite3.Row
  cur = conn.cursor()
  now = now_kobo()
  for name in plan["to_delete"]:
    delete_shelf_soft(cur, name, now)
    print(f"Removed collection: {name}")
  for tag in plan["to_create"]:
    create_shelf(cur, tag, now)
    print(f"Created collection: {tag}")
  count_added = 0
  for tag, cid in plan["to_add"]:
    if not membership_exists(cur, tag, cid):
      add_to_shelf(cur, tag, cid, now)
      print(f"Added {cid} to {tag}")
      count_added += 1
  conn.commit()
  conn.close()
  print(f"Done. Added {count_added} memberships")
  return 0


def cmd_tags_to_collections(args):
  db_path = args.db_path
  mount_root = get_mount_root(db_path)
  calibre_root = getattr(args, "calibre", None)
  if calibre_root:
    calibre_root = Path(calibre_root).expanduser().resolve()
  else:
    calibre_root = mount_root
  meta_path = find_metadata_calibre(calibre_root)
  if not meta_path:
    meta_path = find_metadata_calibre(mount_root)
  if not meta_path:
    print(f"{CALIBRE_METADATA} not found")
    searched = []
    p1 = Path(calibre_root) / CALIBRE_METADATA
    if not p1.is_file():
      searched.append(str(calibre_root))
    p2 = Path(mount_root) / CALIBRE_METADATA
    if not p2.is_file() and str(mount_root) not in searched:
      searched.append(str(mount_root))
    if searched:
      print(f"Searched in {', '.join(searched)}")
    return 1
  print(f"Using {CALIBRE_METADATA}: {meta_path}")
  books = load_metadata_calibre(meta_path)
  ignore = set()
  if getattr(args, "ignore_tags", None):
    for t in args.ignore_tags.split(","):
      t = t.strip()
      if t:
        ignore.add(t)
  dry_run_flag = getattr(args, "dry_run", False)
  force = getattr(args, "yes", False)
  plan = plan_tags_to_collections(db_path, mount_root, books, ignore)
  return apply_tags_to_collections(plan, db_path, dry_run_flag, force)


def build_parser():
  p = argparse.ArgumentParser(
    prog="kobo.py",
    description="Kobo database tool.",
  )
  p.add_argument("--db", help=f"Path to {DB_NAME}")
  p.add_argument("--kobo-root", help=f"Kobo mount root containing {KOBO_DIR}")
  sub = p.add_subparsers(dest="command")
  sp_col = sub.add_parser("collections", aliases=["c"], help="List/show collections")
  sp_col.add_argument("name", nargs="?")
  sp_col.set_defaults(func=cmd_collections_list)
  sp_tags = sub.add_parser("tags-to-collections", aliases=["t2c"],
                           help="Turn Calibre tags into Kobo collections")
  sp_tags.add_argument("--calibre", help="Path to mounted Kobo root or Calibre library")
  sp_tags.add_argument("--dry-run", action="store_true", help="Show changes without applying")
  sp_tags.add_argument("--yes", action="store_true", help="Apply changes")
  sp_tags.add_argument("--ignore-tags", help="Comma separated tags to ignore")
  sp_tags.set_defaults(func=cmd_tags_to_collections)
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
