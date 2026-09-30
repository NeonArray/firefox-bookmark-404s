#!/usr/bin/env python3
"""
check_bookmarks.py

Iterates over all bookmarks stored in Firefox's places.sqlite, checks each
bookmark's URL for a live (200) response, and flags dead ones by prefixing
"(DEAD) " to the bookmark title.

IMPORTANT: Firefox must be COMPLETELY CLOSED before running this script,
otherwise places.sqlite will be locked and writes may fail or corrupt data.

Usage:
    python3 check_bookmarks.py [--profile PATH] [--dry-run] [--workers N]
                                [--timeout SECONDS] [--delete-dead]

By default the script auto-detects your default Firefox profile and makes
a timestamped backup copy of places.sqlite before modifying anything.

Pass --delete-dead to permanently remove bookmarks already flagged with
"(DEAD)" (from a previous run). This mode skips all URL/DNS checks and
only reads/deletes rows already present in the sqlite db.
"""

import argparse
import concurrent.futures
import configparser
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

DEAD_PREFIX = "(DEAD) "
USER_AGENT = (
    "Mozilla/5.0 (compatible; BookmarkChecker/1.0; "
    "+https://example.com/bookmark-checker)"
)


def find_default_profile() -> str:
    """Locate the default Firefox profile directory for the current OS."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support/Firefox")
    elif sys.platform.startswith("win"):
        base = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox")
    else:  # linux and other unix-likes
        base = os.path.expanduser("~/.mozilla/firefox")

    ini_path = os.path.join(base, "profiles.ini")
    if not os.path.isfile(ini_path):
        raise FileNotFoundError(f"Could not find profiles.ini at {ini_path}")

    config = configparser.ConfigParser()
    config.read(ini_path)

    # Prefer an explicit "Default=1" install mapping (modern Firefox), else
    # fall back to the first profile marked Default=1 in a [ProfileN] section.
    default_rel_path = None
    for section in config.sections():
        if section.startswith("Install") and config.has_option(section, "Default"):
            default_rel_path = config.get(section, "Default")
            break

    if not default_rel_path:
        for section in config.sections():
            if section.startswith("Profile") and config.has_option(section, "Default"):
                if config.get(section, "Default") == "1":
                    default_rel_path = config.get(section, "Path")
                    break

    if not default_rel_path:
        # Last resort: just take the first profile listed.
        for section in config.sections():
            if section.startswith("Profile") and config.has_option(section, "Path"):
                default_rel_path = config.get(section, "Path")
                break

    if not default_rel_path:
        raise FileNotFoundError("Could not determine default profile from profiles.ini")

    return os.path.join(base, default_rel_path)


ROOT_FOLDER_NAMES = {
    "root________": "Places Root",
    "menu________": "Bookmarks Menu",
    "toolbar_____": "Bookmarks Toolbar",
    "unfiled_____": "Other Bookmarks",
    "mobile______": "Mobile Bookmarks",
    "tags________": "Tags",
}


def load_folder_index(cur):
    """Load id -> {parent, title, guid} for every row in moz_bookmarks (folders included)."""
    cur.execute("SELECT id, parent, title, guid FROM moz_bookmarks")
    return {row[0]: {"parent": row[1], "title": row[2], "guid": row[3]} for row in cur.fetchall()}


def folder_path(parent_id, folder_index):
    """Walk up from a bookmark's parent folder to a root, returning a readable path."""
    parts = []
    current_id = parent_id
    visited = set()
    while current_id is not None and current_id not in visited:
        visited.add(current_id)
        node = folder_index.get(current_id)
        if node is None:
            break
        guid = node.get("guid")
        if guid in ROOT_FOLDER_NAMES:
            parts.append(ROOT_FOLDER_NAMES[guid])
            break
        parts.append(node.get("title") or "(untitled folder)")
        current_id = node.get("parent")
    return " / ".join(reversed(parts)) if parts else "(unknown location)"


def is_db_locked(db_path: str) -> bool:
    """Best-effort check for whether Firefox currently has the db locked."""
    try:
        conn = sqlite3.connect(db_path, timeout=1)
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        conn.close()
        return False
    except sqlite3.OperationalError:
        return True


def check_url(url: str, timeout: float) -> bool:
    """Return True if the URL responds with HTTP 200, False otherwise."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return True
            # Some servers don't support HEAD properly; fall through to GET.
    except urllib.error.HTTPError as e:
        if e.code == 405 or e.code == 501:
            pass  # method not allowed, retry with GET below
        else:
            return False
    except Exception:
        return False

    # Retry with GET (some servers reject HEAD requests).
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Flag dead Firefox bookmarks with (DEAD).")
    parser.add_argument("--profile", help="Path to Firefox profile directory containing places.sqlite")
    parser.add_argument("--dry-run", action="store_true", help="Only report dead links, don't modify the database")
    parser.add_argument("--workers", type=int, default=16, help="Number of concurrent URL checks (default: 16)")
    parser.add_argument("--timeout", type=float, default=8.0, help="Per-request timeout in seconds (default: 8)")
    parser.add_argument(
        "--delete-dead",
        action="store_true",
        help=(
            "Delete bookmarks already flagged with the '(DEAD)' prefix from the "
            "database. Skips all URL/DNS checks and only processes existing "
            "titles already in the sqlite db."
        ),
    )
    args = parser.parse_args()

    profile_dir = args.profile or find_default_profile()
    db_path = os.path.join(profile_dir, "places.sqlite")

    if not os.path.isfile(db_path):
        sys.exit(f"places.sqlite not found at: {db_path}")

    if is_db_locked(db_path):
        sys.exit(
            "places.sqlite appears to be locked. Please completely quit Firefox "
            "(check background/helper processes too) and try again."
        )

    if not args.dry_run:
        backup_path = f"{db_path}.bak.{datetime.now():%Y%m%d%H%M%S}"
        shutil.copy2(db_path, backup_path)
        print(f"Backup created at: {backup_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    folder_index = load_folder_index(cur)

    if args.delete_dead:
        # No network/DNS activity here at all — just reads titles already
        # marked "(DEAD)" in the db and removes those bookmark rows.
        cur.execute(
            """
            SELECT b.id, b.title, p.url, b.parent
            FROM moz_bookmarks b
            LEFT JOIN moz_places p ON b.fk = p.id
            WHERE b.type = 1 AND b.title LIKE ?
            """,
            (DEAD_PREFIX + "%",),
        )
        flagged = cur.fetchall()
        print(f"Found {len(flagged)} bookmark(s) flagged '{DEAD_PREFIX.strip()}'.")

        if not flagged:
            conn.close()
            return

        print("\nBookmarks to delete:")
        for _, title, url, parent in flagged:
            location = folder_path(parent, folder_index)
            print(f"  - {title!r} -> {url}")
            print(f"      location: {location}")

        if args.dry_run:
            print("\nDry run: no bookmarks were deleted.")
            conn.close()
            return

        for bid, _, _, _ in flagged:
            cur.execute("DELETE FROM moz_bookmarks WHERE id = ?", (bid,))

        conn.commit()
        conn.close()
        print(f"\nDeleted {len(flagged)} bookmark(s).")
        return

    # moz_bookmarks.type = 1 -> bookmark (as opposed to folder=2, separator=3)
    # fk -> moz_places.id, which holds the actual URL
    # dateAdded is stored in microseconds since epoch
    cur.execute(
        """
        SELECT b.id, b.title, p.url, b.parent, b.dateAdded
        FROM moz_bookmarks b
        JOIN moz_places p ON b.fk = p.id
        WHERE b.type = 1 AND p.url LIKE 'http%'
        """
    )
    bookmarks = cur.fetchall()
    print(f"Found {len(bookmarks)} bookmarks to check...")

    dead = []

    def worker(bookmark):
        bid, title, url, parent, date_added = bookmark
        ok = check_url(url, args.timeout)
        return bid, title, url, parent, date_added, ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker, b) for b in bookmarks]
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            bid, title, url, parent, date_added, ok = future.result()
            status = "OK" if ok else "DEAD"
            print(f"[{i}/{len(bookmarks)}] {status:4s} {url}")
            if not ok:
                dead.append((bid, title, url, parent, date_added))

    print(f"\n{len(dead)} dead bookmark(s) found.")

    if not dead:
        conn.close()
        return

    print("\nDead bookmark details:")
    for _, title, url, parent, date_added in dead:
        location = folder_path(parent, folder_index)
        added_str = (
            datetime.fromtimestamp(date_added / 1_000_000).strftime("%Y-%m-%d %H:%M:%S")
            if date_added
            else "unknown"
        )
        print(f"  - {title!r} -> {url}")
        print(f"      location: {location}")
        print(f"      added:    {added_str}")

    if args.dry_run:
        conn.close()
        return

    updated = 0
    now_us = int(time.time() * 1_000_000)  # Firefox stores lastModified in microseconds
    for bid, title, url, parent, date_added in dead:
        title = title or ""
        if title.startswith(DEAD_PREFIX):
            continue
        new_title = DEAD_PREFIX + title
        cur.execute(
            "UPDATE moz_bookmarks SET title = ?, lastModified = ? WHERE id = ?",
            (new_title, now_us, bid),
        )
        updated += 1

    conn.commit()
    conn.close()
    print(f"Updated {updated} bookmark title(s) with '{DEAD_PREFIX.strip()}' flag.")


if __name__ == "__main__":
    main()
