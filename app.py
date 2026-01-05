import os
import sys
import json
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone

# --------------------
# Persistent config/state paths
# --------------------
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

STATE_HISTORY_LIMIT = int(os.environ.get("STATE_HISTORY_LIMIT", "20"))

# --------------------
# Load config.json (if exists) and fall back to env vars
# --------------------
def load_cfg() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

_cfg = load_cfg()

def cfg_get(name: str, default: str) -> str:
    return str(_cfg.get(name, os.environ.get(name, default)))

RADARR_URL = cfg_get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = cfg_get("RADARR_API_KEY", "")
TAG_LABEL = cfg_get("TAG_LABEL", "autodelete30")
DAYS_OLD = int(cfg_get("DAYS_OLD", "30"))

DELETE_FILES = cfg_get("DELETE_FILES", "true").lower() == "true"
ADD_IMPORT_EXCLUSION = cfg_get("ADD_IMPORT_EXCLUSION", "false").lower() == "true"
DRY_RUN = cfg_get("DRY_RUN", "true").lower() == "true"
TIMEOUT = int(cfg_get("HTTP_TIMEOUT_SECONDS", "30"))

# --------------------
# Utility
# --------------------
def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_state() -> dict:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def save_state(state: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        # Don't fail the run because state couldn't be written
        pass

# --------------------
# Radarr API helpers
# --------------------
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
    # Normalize trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# --------------------
# Main
# --------------------
def main():
    if not RADARR_URL:
        die("RADARR_URL is required, e.g. http://radarr:7878")
    if not RADARR_API_KEY:
        die("RADARR_API_KEY is required")

    run_started = datetime.now(timezone.utc)
    state = load_state()

    # Initialize run state early
    run_state = {
        "started_at": run_started.isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "status": "running",
        "dry_run": DRY_RUN,
        "tag_label": TAG_LABEL,
        "days_old": DAYS_OLD,
        "delete_files": DELETE_FILES,
        "add_import_exclusion": ADD_IMPORT_EXCLUSION,
        "candidates_found": 0,
        "deleted_count": 0,
        "deleted": [],   # list of objects (dry-run included)
        "errors": [],
    }

    state["last_run"] = run_state
    save_state(state)

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS_OLD)

        print(f"[mediareaparr] Starting run")
        print(f"[mediareaparr] RADARR_URL={RADARR_URL}")
        print(f"[mediareaparr] TAG_LABEL={TAG_LABEL} DAYS_OLD={DAYS_OLD} cutoff={cutoff.isoformat()}")
        print(f"[mediareaparr] DELETE_FILES={DELETE_FILES} ADD_IMPORT_EXCLUSION={ADD_IMPORT_EXCLUSION} DRY_RUN={DRY_RUN}")

        # Find tag id
        tags = radarr_get("/api/v3/tag")
        tag = next((t for t in tags if t.get("label") == TAG_LABEL), None)
        if not tag:
            raise RuntimeError(f"Tag '{TAG_LABEL}' not found in Radarr. Create it and tag movies first.")

        tag_id = tag["id"]

        # Get movies
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
                age_days = int((datetime.now(timezone.utc) - added).total_seconds() // 86400)
                to_delete.append((m, age_days))

        # Oldest first
        to_delete.sort(key=lambda x: x[1], reverse=True)

        run_state["candidates_found"] = len(to_delete)
        save_state(state)

        for m, age_days in to_delete:
            movie_id = m["id"]
            title = m.get("title")
            year = m.get("year")
            added_str = m.get("added")
            path = m.get("path")

            print(f"[mediareaparr] DELETE candidate: id={movie_id} title='{title}' added={added_str}")

            deleted_entry = {
                "id": movie_id,
                "title": title,
                "year": year,
                "added": added_str,
                "age_days": age_days,
                "path": path,
                "deleted_at": None,
                "dry_run": DRY_RUN,
            }

            if DRY_RUN:
                # For dry-run, we record what *would* be deleted
                run_state["deleted"].append(deleted_entry)
                continue

            try:
                radarr_delete_movie(movie_id)
                deleted_entry["deleted_at"] = utc_now_iso()
                run_state["deleted"].append(deleted_entry)
                run_state["deleted_count"] = len([d for d in run_state["deleted"] if d.get("deleted_at")])
                save_state(state)
                print(f"[mediareaparr] Deleted: id={movie_id} title='{title}'")
            except Exception as e:
                err = f"ERROR deleting id={movie_id} title='{title}': {e}"
                print(f"[mediareaparr] {err}", file=sys.stderr)
                run_state["errors"].append(err)
                save_state(state)

        run_state["status"] = "ok" if not run_state["errors"] else "ok_with_errors"

    except Exception as e:
        run_state["status"] = "failed"
        run_state["errors"].append(str(e))
        raise
    finally:
        finished = datetime.now(timezone.utc)
        run_state["finished_at"] = finished.isoformat()
        run_state["duration_seconds"] = int((finished - run_started).total_seconds())

        # Save last_run + history
        state["last_run"] = run_state
        history = state.get("run_history") or []
        history.insert(0, run_state)  # newest first
        history = history[:STATE_HISTORY_LIMIT]
        state["run_history"] = history
        save_state(state)

        print(f"[mediareaparr] Run complete status={run_state['status']}")

if __name__ == "__main__":
    main()
