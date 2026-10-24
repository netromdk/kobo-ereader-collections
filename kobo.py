#!/usr/bin/env python3
import argparse
import os
import sqlite3
import sys
from pathlib import Path


DB_NAME = "KoboReader.sqlite"
KOBO_DIR = ".kobo"
ENV_VAR = "KOBO_DB"


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


def build_parser():
  p = argparse.ArgumentParser(
    prog="kobo.py",
    description="Kobo database tool.",
  )
  p.add_argument("--db", help=f"Path to {DB_NAME}")
  p.add_argument("--kobo-root", help=f"Kobo mount root containing {KOBO_DIR}")
  return p


def main(argv=None):
  parser = build_parser()
  args = parser.parse_args(argv)
  db_path = find_kobo_db(explicit=args.db, kobo_root=args.kobo_root)
  print(f"Using SQLite: {db_path}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
