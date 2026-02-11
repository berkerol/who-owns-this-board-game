#!/usr/bin/env python3
"""
Generate games.js from users.js (object map of bgg->whatsapp).

Input:  ./users.js
Format:
  const users = { // eslint-disable-line no-unused-vars
    'berkerol': 'Berk Erol'
  }

Output: ./games.js
  window.GAMES = [{ id, name, owners }, ...]

Auth:
- Requires env var BGG_TOKEN (Bearer token)

BGG API:
- /xmlapi2/collection?username=...&own=1&subtype=boardgame
- /xmlapi2/thing?id=...&type=boardgame to resolve canonical (primary) names

Determinism:
- games sorted by (name, id)
- owners sorted

Important filtering:
- Any IDs returned by /collection that do NOT resolve via /thing with type=boardgame
  are silently dropped (prevents expansions/accessories/etc. from leaking in).
"""

from __future__ import annotations

import os
import re
import sys
import time
from typing import Dict, Iterable, List, Optional, Set

import requests
import xml.etree.ElementTree as ET

BGG_COLLECTION_URL = "https://boardgamegeek.com/xmlapi2/collection"
BGG_THING_URL = "https://boardgamegeek.com/xmlapi2/thing"


# -------------------------
# Parse users.js (object literal)
# -------------------------

# Matches entries like:
#   'berkerol': 'Berk Erol'
#   "berkerol": "Berk Erol"
_USER_ENTRY_RE = re.compile(
    r"""
    (['"])(?P<key>(?:\\.|(?!\1).)*)\1      # 'key' or "key"
    \s*:\s*
    (['"])(?P<val>(?:\\.|(?!\3).)*)\3      # 'val' or "val"
    """,
    re.VERBOSE | re.DOTALL,
)


def _unescape_js_string(s: str) -> str:
    return bytes(s, "utf-8").decode("unicode_escape")


def parse_bgg_usernames_from_users_js(path: str) -> List[str]:
    """
    Parses users.js and returns list of BGG usernames (object keys).
    """
    txt = open(path, "r", encoding="utf-8").read()
    usernames: List[str] = []

    for m in _USER_ENTRY_RE.finditer(txt):
        k = _unescape_js_string(m.group("key")).strip()
        if k:
            usernames.append(k)

    # De-duplicate while preserving order
    seen: Set[str] = set()
    out: List[str] = []
    for u in usernames:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# -------------------------
# BGG helpers
# -------------------------

def http_get_xml_text(
    url: str,
    token: str,
    params: dict,
    *,
    timeout: int = 30,
    max_attempts: int = 25,
    backoff_seconds: float = 2.0,
    user_agent: str = "bgg-games-js-generator/1.4",
) -> str:
    headers = {
        "User-Agent": user_agent,
        "Authorization": f"Bearer {token}",
    }

    last_status: Optional[int] = None
    for _ in range(max_attempts):
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        last_status = resp.status_code

        # BGG queues requests: 202 => retry
        if resp.status_code == 202:
            time.sleep(backoff_seconds)
            continue

        resp.raise_for_status()
        return resp.text

    raise TimeoutError(f"{url} kept returning {last_status} after {max_attempts} attempts.")


def chunked(xs: List[int], n: int) -> Iterable[List[int]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def fetch_owned_game_ids_for_user(username: str, token: str) -> List[int]:
    params = {
        "username": username,
        "own": 1,
        "subtype": "boardgame",  # base games only
    }
    xml_text = http_get_xml_text(BGG_COLLECTION_URL, token, params)
    root = ET.fromstring(xml_text)

    ids: List[int] = []
    for item in root.findall("item"):
        oid = item.get("objectid")
        if oid and oid.isdigit():
            ids.append(int(oid))

    # de-dupe preserve order
    seen: Set[int] = set()
    out: List[int] = []
    for gid in ids:
        if gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


def fetch_primary_names(ids: List[int], token: str, *, chunk_size: int = 20) -> Dict[int, str]:
    """
    Fetch canonical (primary) names for game ids using /thing, restricted to boardgames.
    Returns: { game_id: primary_name }
    """
    out: Dict[int, str] = {}

    for ch in chunked(ids, chunk_size):
        params = {
            "id": ",".join(str(i) for i in ch),
            "type": "boardgame",
        }
        xml_text = http_get_xml_text(BGG_THING_URL, token, params)
        root = ET.fromstring(xml_text)

        for item in root.findall("item"):
            oid = item.get("id")
            if not oid or not oid.isdigit():
                continue
            gid = int(oid)

            primary = None
            for name_el in item.findall("name"):
                if name_el.get("type") == "primary":
                    primary = name_el.get("value")
                    break

            if primary:
                out[gid] = primary

        time.sleep(0.2)  # be gentle

    return out


# -------------------------
# JS output
# -------------------------

def js_escape(s: str) -> str:
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def write_games_js(path: str, games: List[dict]) -> None:
    lines: List[str] = []
    lines.append("// AUTO-GENERATED. DO NOT EDIT.")
    lines.append("// Generated by generate_games_js.py")
    lines.append(f"// generated_at: {time.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append("window.GAMES = [")
    for g in games:
        owners_js = ", ".join(f'"{js_escape(u)}"' for u in g["owners"])
        lines.append(f'  {{ id: {g["id"]}, name: "{js_escape(g["name"])}", owners: [{owners_js}] }},')
    lines.append("];")
    lines.append("")
    open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))


# -------------------------
# Main
# -------------------------

def main() -> int:
    repo_root = os.getcwd()
    users_path = os.path.join(repo_root, "users.js")
    out_path = os.path.join(repo_root, "games.js")

    token = os.getenv("BGG_TOKEN")
    if not token:
        print("ERROR: Missing BGG_TOKEN env var.", file=sys.stderr)
        return 2

    if not os.path.exists(users_path):
        print("ERROR: users.js not found in repo root.", file=sys.stderr)
        return 2

    bgg_users = parse_bgg_usernames_from_users_js(users_path)
    if not bgg_users:
        print("ERROR: No usernames found in users.js object literal.", file=sys.stderr)
        return 2

    # game_id -> owners
    game_to_owners: Dict[int, Set[str]] = {}

    for i, username in enumerate(bgg_users, start=1):
        print(f"[{i}/{len(bgg_users)}] {username}: fetching owned games…", file=sys.stderr)
        ids = fetch_owned_game_ids_for_user(username, token)
        for gid in ids:
            game_to_owners.setdefault(gid, set()).add(username)
        time.sleep(0.3)

    all_ids = sorted(game_to_owners.keys())
    id_to_name = fetch_primary_names(all_ids, token)

    # IMPORTANT: silently drop any IDs that did not resolve as type=boardgame
    games_out: List[dict] = []
    for gid in all_ids:
        name = id_to_name.get(gid)
        if not name:
            continue
        owners_sorted = sorted(game_to_owners[gid])
        games_out.append({"id": gid, "name": name, "owners": owners_sorted})

    games_out.sort(key=lambda g: (g["name"].casefold(), g["id"]))

    write_games_js(out_path, games_out)
    print(f"Wrote games.js with {len(games_out)} games.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
