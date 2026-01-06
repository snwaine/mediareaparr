import os
import sys
import json
import argparse
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# --------------------
# Persistent config/state paths
# --------------------
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

STATE_HISTORY_LIMIT = int(os.environ.get("STATE_HISTORY_LIMIT", "20"))


# --------------------
# Utility
# --------------------
def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        # Do not fail the run if state couldn't be written
        pass


def load_cfg() -> Dict[str, Any]:
    return load_json(CONFIG_PATH)


def load_state() -> Dict[str, Any]:
    return load_json(STATE_PATH)


def save_state(state: Dict[str, Any]) -> None:
    save_json(STATE_PATH, state)


def clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except Exception:
        return default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def normalize_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def normalize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    Job schema expected from webui.py:
      id, name, enabled,
      TAG_LABEL, DAYS_OLD,
      SCHED_DAY, SCHED_HOUR,
      DRY_RUN, DELETE_FILES, ADD_IMPORT_EXCLUSION
    """
    j = dict(job or {})
    j["id"] = str(j.get("id") or "").strip()
    j["name"] = str(j.get("name") or "Job").strip()
    j["enabled"] = bool(j.get("enabled", True))

    j["TAG_LABEL"] = str(j.get("TAG_LABEL") or "autodelete30").strip()
    j["DAYS_OLD"] = clamp_int(j.get("DAYS_OLD", 30), 1, 36500, 30)

    j["SCHED_DAY"] = str(j.get("SCHED_DAY") or "daily").lower()
    j["SCHED_HOUR"] = clamp_int(j.get("SCHED_HOUR", 3), 0, 23, 3)

    j["DRY_RUN"] = normalize_bool(j.get("DRY_RUN", True), True)
    j["DELETE_FILES"] = normalize_bool(j.get("DELETE_FILES", True), True)
    j["ADD_IMPORT_EXCLUSION"] = normalize_bool(j.get("ADD_IMPORT_EXCLUSION", False), False)

    return j


# --------------------
# Radarr helpers (parametrized per job where needed)
# --------------------
def radarr_get(radarr_url: str, api_key: str, timeout: int, path: str):
    url = f"{radarr_url}{path}"
    r = requests.get(url, headers={"X-Api-Key": api_key}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def radarr_delete_movie(
    radarr_url: str,
    api_key: str,
    timeout: int,
    movie_id: int,
    delete_files: bool,
    add_import_exclusion: bool
):
    url = f"{radarr_url}/api/v3/movie/{movie_id}"
    params = {
        "deleteFiles": str(delete_files).lower(),
        "addImportExclusion": str(add_import_exclusion).lower(),
    }
    r = requests.delete(url, headers={"X-Api-Key": api_key}, params=params, timeout=timeout)
    r.raise_for_status()


def parse_radarr_date(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------
# Job discovery / Run Now flags
# --------------------
def list_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    jobs = cfg.get("JOBS")
    if isinstance(jobs, list) and jobs:
        out = [normalize_job(j) for j in jobs]
        # filter invalid IDs
        out = [j for j in out if j["id"]]
        return out

    # Backward compatible: single-job config from old keys/env
    def cfg_get(name: str, default: str) -> str:
        return str(cfg.get(name, os.environ.get(name, default)))

    legacy = {
        "id": "legacy",
        "name": "Legacy Job",
        "enabled": True,
        "TAG_LABEL": cfg_get("TAG_LABEL", "autodelete30"),
        "DAYS_OLD": int(cfg_get("DAYS_OLD", "30")),
        "DRY_RUN": cfg_get("DRY_RUN", "true").lower() == "true",
        "DELETE_FILES": cfg_get("DELETE_FILES", "true").lower() == "true",
        "ADD_IMPORT_EXCLUSION": cfg_get("ADD_IMPORT_EXCLUSION", "false").lower() == "true",
        "SCHED_DAY": "daily",
        "SCHED_HOUR": 3,
    }
    return [normalize_job(legacy)]


def run_now_flag_path(job_id: str) -> Path:
    return CONFIG_DIR / f"run_now_{job_id}.flag"


def has_run_now_flag(job_id: str) -> bool:
    try:
        return run_now_flag_path(job_id).exists()
    except Exception:
        return False


def clear_run_now_flag(job_id: str) -> None:
    try:
        p = run_now_flag_path(job_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass


# --------------------
# State helpers (per job + global)
# --------------------
def record_run(state: Dict[str, Any], job_id: str, run_state: Dict[str, Any]) -> None:
    # global last run (kept for dashboard simplicity)
    state["last_run"] = run_state

    # per-job last run
    last_runs = state.get("last_runs")
    if not isinstance(last_runs, dict):
        last_runs = {}
    last_runs[job_id] = run_state
    state["last_runs"] = last_runs

    # global history
    history = state.get("run_history")
    if not isinstance(history, list):
        history = []
    history.insert(0, run_state)
    state["run_history"] = history[:STATE_HISTORY_LIMIT]

    # per-job history
    by_job = state.get("run_history_by_job")
    if not isinstance(by_job, dict):
        by_job = {}
    jhist = by_job.get(job_id)
    if not isinstance(jhist, list):
        jhist = []
    jhist.insert(0, run_state)
    by_job[job_id] = jhist[:STATE_HISTORY_LIMIT]
    state["run_history_by_job"] = by_job


# --------------------
# Core job execution
# --------------------
def run_job(cfg: Dict[str, Any], state: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    # Global settings
    radarr_url = str(cfg.get("RADARR_URL", os.environ.get("RADARR_URL", ""))).rstrip("/")
    api_key = str(cfg.get("RADARR_API_KEY", os.environ.get("RADARR_API_KEY", "")))
    timeout = clamp_int(cfg.get("HTTP_TIMEOUT_SECONDS", os.environ.get("HTTP_TIMEOUT_SECONDS", "30")), 5, 300, 30)

    if not radarr_url:
        raise RuntimeError("RADARR_URL is required, e.g. http://radarr:7878")
    if not api_key:
        raise RuntimeError("RADARR_API_KEY is required")

    job_id = job["id"]
    tag_label = job["TAG_LABEL"]
    days_old = int(job["DAYS_OLD"])
    delete_files = bool(job["DELETE_FILES"])
    add_import_exclusion = bool(job["ADD_IMPORT_EXCLUSION"])
    dry_run = bool(job["DRY_RUN"])

    run_started = datetime.now(timezone.utc)

    run_state = {
        "job_id": job_id,
        "job_name": job.get("name", "Job"),
        "started_at": run_started.isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "status": "running",
        "dry_run": dry_run,
        "tag_label": tag_label,
        "days_old": days_old,
        "delete_files": delete_files,
        "add_import_exclusion": add_import_exclusion,
        "candidates_found": 0,
        "deleted_count": 0,
        "deleted": [],
        "errors": [],
    }

    # Save "running" state early (per job + global last_run)
    record_run(state, job_id, run_state)
    save_state(state)

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_old)

        print(f"[mediareaparr] Starting job id={job_id} name='{job.get('name','Job')}'")
        print(f"[mediareaparr] RADARR_URL={radarr_url}")
        print(f"[mediareaparr] TAG_LABEL={tag_label} DAYS_OLD={days_old} cutoff={cutoff.isoformat()}")
        print(f"[mediareaparr] DELETE_FILES={delete_files} ADD_IMPORT_EXCLUSION={add_import_exclusion} DRY_RUN={dry_run}")

        # Find tag id
        tags = radarr_get(radarr_url, api_key, timeout, "/api/v3/tag")
        tag = next((t for t in tags if t.get("label") == tag_label), None)
        if not tag:
            raise RuntimeError(f"Tag '{tag_label}' not found in Radarr. Create it and tag movies first.")

        tag_id = tag["id"]

        # Get movies
        movies = radarr_get(radarr_url, api_key, timeout, "/api/v3/movie")

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
        record_run(state, job_id, run_state)
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
                "dry_run": dry_run,
            }

            if dry_run:
                run_state["deleted"].append(deleted_entry)
                continue

            try:
                radarr_delete_movie(
                    radarr_url, api_key, timeout,
                    movie_id,
                    delete_files=delete_files,
                    add_import_exclusion=add_import_exclusion
                )
                deleted_entry["deleted_at"] = utc_now_iso()
                run_state["deleted"].append(deleted_entry)
                run_state["deleted_count"] = len([d for d in run_state["deleted"] if d.get("deleted_at")])
                record_run(state, job_id, run_state)
                save_state(state)
                print(f"[mediareaparr] Deleted: id={movie_id} title='{title}'")
            except Exception as e:
                err = f"ERROR deleting id={movie_id} title='{title}': {e}"
                print(f"[mediareaparr] {err}", file=sys.stderr)
                run_state["errors"].append(err)
                record_run(state, job_id, run_state)
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
        record_run(state, job_id, run_state)
        save_state(state)
        print(f"[mediareaparr] Job complete id={job_id} status={run_state['status']}")

    return run_state


# --------------------
# Main
# --------------------
def main():
    ap = argparse.ArgumentParser(description="mediareaparr multi-job runner")
    ap.add_argument("--job-id", dest="job_id", default="", help="Run a single job by id")
    ap.add_argument("--run-enabled", action="store_true", help="Run all enabled jobs (default when no --job-id)")
    ap.add_argument("--run-now-only", action="store_true", help="Only run jobs with /config/run_now_<id>.flag")
    args = ap.parse_args()

    cfg = load_cfg()
    jobs = list_jobs(cfg)
    state = load_state()

    # Select jobs to run
    selected: List[Dict[str, Any]] = []

    if args.job_id:
        job = next((j for j in jobs if j["id"] == args.job_id), None)
        if not job:
            die(f"Job id '{args.job_id}' not found in config.json", 2)
        if not job.get("enabled", True):
            die(f"Job id '{args.job_id}' is disabled", 3)
        selected = [job]
    else:
        if args.run_now_only:
            selected = [j for j in jobs if j.get("enabled", True) and has_run_now_flag(j["id"])]
        else:
            # default: run all enabled jobs
            selected = [j for j in jobs if j.get("enabled", True)]

    if not selected:
        print("[mediareaparr] No jobs selected. Exiting.")
        return

    # Execute jobs
    overall_fail = False
    for job in selected:
        jid = job["id"]
        try:
            run_job(cfg, state, job)
        except Exception as e:
            overall_fail = True
            print(f"[mediareaparr] Job failed id={jid}: {e}", file=sys.stderr)
        finally:
            # If it was a run-now trigger, clear the flag so it doesn't repeat
            clear_run_now_flag(jid)

    if overall_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
