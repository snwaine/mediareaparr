import os
import sys
import time
import requests
from datetime import datetime, timedelta, timezone

RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
TAG_LABEL = os.environ.get("TAG_LABEL", "TAG_LABLE2")
DAYS_OLD = int(os.environ.get("DAYS_OLD", "30"))

DELETE_FILES = os.environ.get("DELETE_FILES", "true").lower() == "true"
ADD_IMPORT_EXCLUSION = os.environ.get("ADD_IMPORT_EXCLUSION", "false").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

TIMEOUT = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "30"))

def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)

def radarr_get(path: str):
    url = f"{RADARR_URL}{path}"
    r = requests.get(url, headers={"X-Api-Key": RADARR_API_KEY}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def radarr_delete_movie(movie_id: int):
    url = f"{RADARR_URL}/api/v3/movie/{movie_id}"
    params = {
        "deleteFiles": str(DELETE_FILES).lower(),
        "addImportExclusion": str(ADD_IMPORT_EXCLUSION).lower(),
    }
    r = requests.delete(url, headers={"X-Api-Key": RADARR_API_KEY}, params=params, timeout=TIMEOUT)
    r.raise_for_status()

def parse_radarr_date(s: str) -> datetime:
    # Radarr typically returns ISO8601 like "2024-12-01T10:20:30Z" or with offset
    # datetime.fromisoformat doesn't accept trailing 'Z' pre-3.11 in some cases, so normalize.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def main():
    if not RADARR_URL:
        die("RADARR_URL is required, e.g. http://radarr:7878")
    if not RADARR_API_KEY:
        die("RADARR_API_KEY is required")

    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_OLD)

    print(f"[agregarr-cleanarr
] Starting run")
    print(f"[agregarr-cleanarr
] RADARR_URL={RADARR_URL}")
    print(f"[agregarr-cleanarr
] TAG_LABEL={TAG_LABEL} DAYS_OLD={DAYS_OLD} cutoff={cutoff.isoformat()}")
    print(f"[agregarr-cleanarr
] DELETE_FILES={DELETE_FILES} ADD_IMPORT_EXCLUSION={ADD_IMPORT_EXCLUSION} DRY_RUN={DRY_RUN}")

    # Find tag id
    tags = radarr_get("/api/v3/tag")
    tag = next((t for t in tags if t.get("label") == TAG_LABEL), None)
    if not tag:
        die(f"Tag '{TAG_LABEL}' not found in Radarr. Create it first and tag some movies.", 2)

    tag_id = tag["id"]
    print(f"[agregarr-cleanarr
] tag_id={tag_id}")

    movies = radarr_get("/api/v3/movie")

    to_delete = []
    for m in movies:
        if tag_id not in (m.get("tags") or []):
            continue
        added_str = m.get("added")
        if not added_str:
            continue
        added = parse_radarr_date(added_str)
        if added < cutoff:
            to_delete.append((m["id"], m.get("title"), added_str))

    print(f"[agregarr-cleanarr
] Found {len(to_delete)} movie(s) to delete")

    for movie_id, title, added_str in to_delete:
        print(f"[agregarr-cleanarr
] DELETE candidate: id={movie_id} title='{title}' added={added_str}")
        if DRY_RUN:
            continue
        try:
            radarr_delete_movie(movie_id)
            print(f"[agregarr-cleanarr
] Deleted: id={movie_id} title='{title}'")
        except Exception as e:
            print(f"[agregarr-cleanarr
] ERROR deleting id={movie_id} title='{title}': {e}", file=sys.stderr)

    print(f"[agregarr-cleanarr
] Run complete")

if __name__ == "__main__":
    main()
