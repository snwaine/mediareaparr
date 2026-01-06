import os
import json
import signal
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests
from flask import (
    Flask, request, redirect, render_template_string,
    flash, get_flashed_messages, send_file
)

# --------------------------
# Paths
# --------------------------
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

LOGO_CANDIDATES = [
    CONFIG_DIR / "logo.png",
    CONFIG_DIR / "logo.jpg",
    CONFIG_DIR / "logo.jpeg",
    CONFIG_DIR / "logo.svg",
    CONFIG_DIR / "logo" / "logo.png",
    CONFIG_DIR / "logo" / "logo.jpg",
    CONFIG_DIR / "logo" / "logo.jpeg",
    CONFIG_DIR / "logo" / "logo.svg",
]

app = Flask(__name__)
app.secret_key = "mediareaparr-secret"


# --------------------------
# Helpers
# --------------------------
def env_default(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except Exception:
        return default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def make_job_id() -> str:
    return uuid.uuid4().hex[:10]


def checkbox(name: str) -> bool:
    return request.form.get(name) == "on"


def cron_from_day_hour(day_key: str, hour: int) -> str:
    hour = clamp_int(hour, 0, 23, 3)
    dow_map = {
        "daily": "*",
        "sun": "0",
        "mon": "1",
        "tue": "2",
        "wed": "3",
        "thu": "4",
        "fri": "5",
        "sat": "6",
    }
    dow = dow_map.get((day_key or "daily").lower(), "*")
    return f"15 {hour} * * {dow}"


def schedule_label(day_key: str, hour: int) -> str:
    day_key = (day_key or "daily").lower()
    names = {
        "daily": "Daily",
        "mon": "Monday",
        "tue": "Tuesday",
        "wed": "Wednesday",
        "thu": "Thursday",
        "fri": "Friday",
        "sat": "Saturday",
        "sun": "Sunday",
    }
    day_txt = names.get(day_key, "Daily")
    h = clamp_int(hour, 0, 23, 3)
    return f"{day_txt} • {h:02d}:00"


def job_defaults() -> Dict[str, Any]:
    return {
        "id": make_job_id(),
        "name": "New Job",
        "enabled": True,
        "TAG_LABEL": "autodelete30",
        "DAYS_OLD": 30,
        "SCHED_DAY": "daily",
        "SCHED_HOUR": 3,
        "DRY_RUN": True,
        "DELETE_FILES": True,
        "ADD_IMPORT_EXCLUSION": False,
    }


def normalize_job(j: Dict[str, Any]) -> Dict[str, Any]:
    d = job_defaults()
    d.update(j or {})
    d["id"] = str(d.get("id") or make_job_id())
    d["name"] = str(d.get("name") or "Job").strip()[:60] or "Job"
    d["enabled"] = bool(d.get("enabled", True))

    d["TAG_LABEL"] = str(d.get("TAG_LABEL") or "").strip()
    d["DAYS_OLD"] = clamp_int(d.get("DAYS_OLD", 30), 1, 36500, 30)

    d["SCHED_DAY"] = str(d.get("SCHED_DAY") or "daily").lower()
    if d["SCHED_DAY"] not in ("daily", "mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        d["SCHED_DAY"] = "daily"
    d["SCHED_HOUR"] = clamp_int(d.get("SCHED_HOUR", 3), 0, 23, 3)

    d["DRY_RUN"] = bool(d.get("DRY_RUN", True))
    d["DELETE_FILES"] = bool(d.get("DELETE_FILES", True))
    d["ADD_IMPORT_EXCLUSION"] = bool(d.get("ADD_IMPORT_EXCLUSION", False))
    return d


# --------------------------
# Config / State
# --------------------------
def load_config() -> Dict[str, Any]:
    cfg = {
        "RADARR_URL": env_default("RADARR_URL", "http://radarr:7878").rstrip("/"),
        "RADARR_API_KEY": env_default("RADARR_API_KEY", ""),
        "SONARR_URL": env_default("SONARR_URL", "").rstrip("/"),
        "SONARR_API_KEY": env_default("SONARR_API_KEY", ""),
        "HTTP_TIMEOUT_SECONDS": int(env_default("HTTP_TIMEOUT_SECONDS", "30")),
        "UI_THEME": env_default("UI_THEME", "dark"),
        "RADARR_OK": False,
        "SONARR_OK": False,
        "JOBS": [],
    }

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k in cfg.keys():
                if k in data:
                    cfg[k] = data[k]
        except Exception:
            pass

    t = (cfg.get("UI_THEME") or "dark").lower()
    cfg["UI_THEME"] = t if t in ("dark", "light") else "dark"
    cfg["RADARR_OK"] = bool(cfg.get("RADARR_OK", False))
    cfg["SONARR_OK"] = bool(cfg.get("SONARR_OK", False))
    cfg["HTTP_TIMEOUT_SECONDS"] = clamp_int(cfg.get("HTTP_TIMEOUT_SECONDS", 30), 5, 300, 30)

    jobs = cfg.get("JOBS") or []
    if not isinstance(jobs, list):
        jobs = []
    jobs = [normalize_job(j) for j in jobs]

    if not jobs:
        j = job_defaults()
        j["name"] = "Default Job"
        # default tag should be empty until user selects, but keep existing behavior if you like:
        j["TAG_LABEL"] = ""
        jobs = [normalize_job(j)]

    cfg["JOBS"] = jobs
    cfg["RADARR_URL"] = (cfg.get("RADARR_URL") or "").rstrip("/")
    cfg["RADARR_API_KEY"] = cfg.get("RADARR_API_KEY") or ""
    cfg["SONARR_URL"] = (cfg.get("SONARR_URL") or "").rstrip("/")
    cfg["SONARR_API_KEY"] = cfg.get("SONARR_API_KEY") or ""
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# --------------------------
# Logo helpers
# --------------------------
def find_logo_path() -> Optional[Path]:
    for p in LOGO_CANDIDATES:
        if p.exists() and p.is_file():
            return p
    return None


def logo_mime(p: Path) -> str:
    ext = p.suffix.lower()
    if ext == ".png":
        return "image/png"
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".svg":
        return "image/svg+xml"
    return "application/octet-stream"


# --------------------------
# Radarr helpers
# --------------------------
def radarr_headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {"X-Api-Key": cfg.get("RADARR_API_KEY", "")}


def radarr_get(cfg: Dict[str, Any], path: str):
    url = cfg["RADARR_URL"].rstrip("/") + path
    r = requests.get(url, headers=radarr_headers(cfg), timeout=int(cfg.get("HTTP_TIMEOUT_SECONDS", 30)))
    r.raise_for_status()
    return r.json()


def parse_radarr_date(s: str):
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def preview_candidates(cfg: Dict[str, Any], job: Dict[str, Any]):
    tag_label = (job.get("TAG_LABEL") or "").strip()
    if not tag_label:
        return {"error": "Tag is empty. Edit the job and select a tag.", "candidates": [], "cutoff": ""}

    days_old = int(job.get("DAYS_OLD", 30))
    now = datetime.now(timezone.utc)
    cutoff = now - __import__("datetime").timedelta(days=days_old)

    tags = radarr_get(cfg, "/api/v3/tag")
    tag = next((t for t in tags if t.get("label") == tag_label), None)
    if not tag:
        return {"error": f"Tag '{tag_label}' not found in Radarr.", "candidates": [], "cutoff": cutoff.isoformat()}

    tag_id = tag["id"]
    movies = radarr_get(cfg, "/api/v3/movie")

    candidates = []
    for m in movies:
        if tag_id not in (m.get("tags") or []):
            continue
        added_str = m.get("added")
        if not added_str:
            continue
        added = parse_radarr_date(added_str).astimezone(timezone.utc)
        if added < cutoff:
            age_days = int((now - added).total_seconds() // 86400)
            candidates.append({
                "id": m.get("id"),
                "title": m.get("title"),
                "year": m.get("year"),
                "added": added_str,
                "age_days": age_days,
                "path": m.get("path"),
            })

    candidates.sort(key=lambda x: x["age_days"], reverse=True)
    return {"error": None, "candidates": candidates, "tag_id": tag_id, "cutoff": cutoff.isoformat()}


# --------------------------
# Toasts
# --------------------------
def render_toasts() -> str:
    msgs = get_flashed_messages(with_categories=True)
    if not msgs:
        return ""

    items = []
    for cat, msg in msgs:
        t = "ok" if cat == "success" else "err"
        items.append(f'<div class="toast {t}">{safe_html(msg)}</div>')

    return f'<div id="toastHost" class="toastHost">{"".join(items)}</div>'


# --------------------------
# UI (base styles + scripts)
# --------------------------
BASE_HEAD = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --bg:#0b0f14;
    --panel:#0f1620;
    --panel2:#0c121b;
    --muted:#9aa7b2;
    --text:#e6edf3;
    --line:#1f2a36;
    --line2:#283241;

    --accent:#22c55e;
    --accent2:#16a34a;

    --warn:#f59e0b;
    --bad:#ef4444;
    --shadow: 0 12px 30px rgba(0,0,0,.35);
  }

  [data-theme="light"]{
    --bg:#f7f8fb;
    --panel:#ffffff;
    --panel2:#ffffff;
    --muted:#526171;
    --text:#0b1220;
    --line:#e5e7eb;
    --line2:#d1d5db;

    --accent:#6d28d9;
    --accent2:#7c3aed;

    --warn:#d97706;
    --bad:#dc2626;
    --shadow: 0 12px 30px rgba(0,0,0,.08);
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }

  body{
    min-height: 100vh;
    margin:0;
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Apple Color Emoji","Segoe UI Emoji";
    background:
      radial-gradient(1200px 700px at 20% 0%, rgba(34,197,94,.18), transparent 60%),
      radial-gradient(900px 600px at 100% 10%, rgba(34,197,94,.10), transparent 55%),
      var(--bg);
    background-attachment: fixed;
    color: var(--text);
  }

  body[data-theme="dark"] { color-scheme: dark; }
  body[data-theme="light"] { color-scheme: light; }

  a{ color: var(--text); text-decoration: none; }
  a:hover{ text-decoration: underline; }

  .wrap{ max-width: 1200px; margin: 0 auto; padding: 22px 18px 36px; }

  .topbar{
    display:flex; align-items:center; justify-content: space-between;
    gap:12px;
    padding: 14px 16px;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
    box-shadow: var(--shadow);
    position: sticky;
    top: 14px;
    z-index: 20;
    backdrop-filter: blur(10px);
  }
  .brand{ display:flex; align-items:center; gap:12px; }
  .logoWrap{
    width: 38px; height: 38px; border-radius: 12px;
    border: 1px solid var(--line2);
    background: rgba(255,255,255,.03);
    overflow:hidden;
    display:flex; align-items:center; justify-content:center;
  }
  .logoBadge{
    width: 38px; height: 38px; border-radius: 12px;
    background: linear-gradient(135deg, rgba(34,197,94,.92), rgba(22,163,74,.65));
    box-shadow: 0 10px 24px rgba(34,197,94,.20);
  }
  .logoImg{
    width: 100%;
    height: 100%;
    object-fit: contain;
    display:block;
    background: rgba(0,0,0,.08);
  }

  .title h1{ margin:0; font-size: 16px; letter-spacing:.2px; }
  .title .sub{ color: var(--muted); font-size: 12px; margin-top: 2px; }

  .nav{ display:flex; align-items:center; gap:8px; flex-wrap: wrap; justify-content: flex-end; }
  .pill{
    border: 1px solid var(--line2);
    background: rgba(255,255,255,.03);
    padding: 8px 11px;
    border-radius: 999px;
    font-size: 13px;
    cursor: pointer;
    color: var(--text);
  }
  .pill.active{
    border-color: rgba(34,197,94,.65);
    box-shadow: 0 0 0 3px rgba(34,197,94,.18);
  }

  .grid{ display:grid; grid-template-columns: repeat(12, 1fr); gap: 14px; margin-top: 16px; }

  .card{
    grid-column: span 12;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
    box-shadow: var(--shadow);
    overflow:hidden;
  }
  .card .hd{
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
    display:flex; align-items:center; justify-content: space-between;
    gap:12px;
    background: rgba(0,0,0,.12);
  }
  [data-theme="light"] .card .hd{ background: rgba(255,255,255,.55); }
  .card .hd h2{ margin:0; font-size: 14px; letter-spacing:.2px; }
  .card .bd{ padding: 14px 16px; }

  .muted{ color: var(--muted); }
  code{
    background: rgba(255,255,255,.06);
    border: 1px solid var(--line2);
    padding: 2px 7px;
    border-radius: 10px;
    color: #dbeafe;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono","Courier New", monospace;
    font-size: 12px;
  }
  [data-theme="light"] code{ color: #1e40af; }

  .btnrow{ display:flex; gap:10px; flex-wrap: wrap; align-items:center; }
  .btn{
    border: 1px solid var(--line2);
    background: rgba(255,255,255,.03);
    color: var(--text);
    padding: 10px 12px;
    border-radius: 12px;
    cursor:pointer;
    font-weight: 600;
    font-size: 13px;
  }
  .btn:hover{ border-color: rgba(34,197,94,.55); }
  .btn:disabled{
    opacity: .45;
    cursor: not-allowed;
    filter: grayscale(0.35);
  }
  .btn.primary{
    border-color: rgba(34,197,94,.55);
    background: linear-gradient(135deg, rgba(34,197,94,.28), rgba(34,197,94,.10));
  }
  .btn.good{
    border-color: rgba(34,197,94,.55);
    background: linear-gradient(135deg, rgba(34,197,94,.22), rgba(34,197,94,.08));
  }
  .btn.warn{
    border-color: rgba(245,158,11,.55);
    background: linear-gradient(135deg, rgba(245,158,11,.22), rgba(245,158,11,.08));
  }
  .btn.bad{
    border-color: rgba(239,68,68,.55);
    background: linear-gradient(135deg, rgba(239,68,68,.20), rgba(239,68,68,.08));
  }

  .form{ display:grid; grid-template-columns: 1fr; gap: 12px; }
  @media(min-width: 900px){ .form{ grid-template-columns: 1fr 1fr; } }

  .field{
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 10px 12px;
    background: rgba(0,0,0,.18);
    position: relative;
  }
  [data-theme="light"] .field{ background: rgba(0,0,0,.03); }

  .field label{ display:block; font-size: 12px; color: var(--muted); margin-bottom: 8px; }

  .field input[type=text], .field input[type=password], .field input[type=number], .field select{
    width: 100%;
    border: 1px solid var(--line2);
    background: rgba(255,255,255,.04);
    color: var(--text);
    padding: 10px 10px;
    border-radius: 12px;
    outline: none;
  }
  [data-theme="light"] .field input, [data-theme="light"] .field select{ background: rgba(0,0,0,.02); }

  /* Dropdown fixes */
  .field select{
    appearance: none;
    -webkit-appearance: none;
    -moz-appearance: none;

    background: rgba(255,255,255,.06);
    padding-right: 36px;
    cursor: pointer;

    background-image:
      linear-gradient(45deg, transparent 50%, var(--muted) 50%),
      linear-gradient(135deg, var(--muted) 50%, transparent 50%);
    background-position:
      calc(100% - 18px) 50%,
      calc(100% - 12px) 50%;
    background-size: 6px 6px, 6px 6px;
    background-repeat: no-repeat;
  }
  [data-theme="light"] .field select{ background: rgba(0,0,0,.02); }

  body[data-theme="dark"] .field select option{
    background-color: #0f1620;
    color: #e6edf3;
  }
  body[data-theme="light"] .field select option{
    background-color: #ffffff;
    color: #0b1220;
  }

  .field input:focus, .field select:focus{
    border-color: rgba(34,197,94,.65);
    box-shadow: 0 0 0 3px rgba(34,197,94,.15);
  }

  .checks{ display:flex; flex-direction: column; gap: 10px; margin-top: 4px; }
  .check{
    display:flex; align-items:center; gap:10px;
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 10px 12px;
    background: rgba(0,0,0,.18);
  }
  [data-theme="light"] .check{ background: rgba(0,0,0,.03); }
  .check input{ transform: scale(1.2); }

  /* Jobs cards */
  .jobsGrid{ display:grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
  .jobCard{
    grid-column: span 12;
    border: 1px solid var(--line);
    border-radius: 16px;
    background: rgba(0,0,0,.12);
    overflow:hidden;
  }
  [data-theme="light"] .jobCard{ background: rgba(0,0,0,.02); }
  .jobTop{
    padding: 12px 12px;
    border-bottom: 1px solid var(--line);
    display:flex;
    align-items:flex-start;
    justify-content: space-between;
    gap: 12px;
  }
  .jobName{ font-weight: 800; letter-spacing:.2px; }
  .jobMeta{ margin-top: 6px; color: var(--muted); font-size: 12px; line-height: 1.35; }
  .jobBody{ padding: 12px 12px; display:flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items:center; }
  .tagPill{
    border: 1px solid var(--line2);
    border-radius: 999px;
    padding: 6px 10px;
    font-size: 12px;
    color: var(--text);
    background: rgba(255,255,255,.03);
  }
  .tagPill.ok { border-color: rgba(34,197,94,.55); }
  .tagPill.off { opacity: .6; }

  table{ width:100%; border-collapse: collapse; overflow:hidden; border-radius: 14px; border: 1px solid var(--line); }
  th, td{ padding: 10px 10px; border-bottom: 1px solid var(--line); font-size: 13px; vertical-align: top; }
  th{
    text-align:left;
    color:#cbd5e1;
    background: rgba(255,255,255,.04);
    position: sticky;
    top: 0;
  }
  [data-theme="light"] th{ color:#111827; background: rgba(0,0,0,.03); }
  tr:hover td{ background: rgba(255,255,255,.02); }
  .tablewrap{ max-height: 420px; overflow:auto; border-radius: 14px; border: 1px solid var(--line); }

  /* Modal */
  .modalBack{
    position: fixed; inset: 0;
    background: rgba(0,0,0,.72);
    backdrop-filter: blur(6px);
    display:none;
    align-items:center;
    justify-content:center;
    z-index: 9999;
    padding: 18px;
  }
  .modal{
    width: min(720px, 100%);
    border: 1px solid var(--line);
    border-radius: 16px;
    background: var(--panel);
    box-shadow: var(--shadow);
    overflow:hidden;
  }
  .modal .mh{
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
    display:flex;
    align-items:center;
    justify-content: space-between;
    gap: 12px;
    background: rgba(0,0,0,.18);
  }
  [data-theme="light"] .modal .mh{ background: rgba(0,0,0,.03); }
  .modal .mh h3{ margin:0; font-size: 14px; letter-spacing: .2px; }
  .modal .mb{ padding: 14px 16px; }
  .modal .mf{
    padding: 14px 16px;
    border-top: 1px solid var(--line);
    display:flex;
    justify-content: flex-end;
    gap: 10px;
    background: rgba(0,0,0,.14);
  }
  [data-theme="light"] .modal .mf{ background: rgba(0,0,0,.02); }

  /* Toasts */
  .toastHost{
    position: fixed;
    right: 16px;
    bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    z-index: 99999;
    pointer-events: none;
    max-width: min(420px, calc(100vw - 32px));
  }
  .toast{
    pointer-events: auto;
    border: 1px solid var(--line2);
    background: linear-gradient(180deg, rgba(255,255,255,.06), rgba(255,255,255,.03));
    box-shadow: var(--shadow);
    border-radius: 14px;
    padding: 12px 12px;
    font-size: 13px;
    color: var(--text);
    opacity: 0;
    transform: translateY(10px);
    animation: toastIn .18s ease-out forwards, toastOut .25s ease-in forwards;
    animation-delay: 0s, 5s;
  }
  .toast.ok{ border-color: rgba(34,197,94,.55); }
  .toast.err{ border-color: rgba(239,68,68,.55); }
  @keyframes toastIn { to { opacity: 1; transform: translateY(0); } }
  @keyframes toastOut { to { opacity: 0; transform: translateY(10px); } }
</style>

<script>
  // ---------- Modal helpers ----------
  function showModal(id) {
    const back = document.getElementById(id);
    if (back) back.style.display = "flex";
  }
  function hideModal(id) {
    const back = document.getElementById(id);
    if (back) back.style.display = "none";
  }

  // Job modal should only close via Cancel/Save redirect:
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideModal("runNowBack"); // only this one
    }
  });

  function setVal(id, v) {
    const el = document.getElementById(id);
    if (el) el.value = v;
  }
  function setChecked(id, v) {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
  }

  // Ensure job tag select can show a value even if it's not in Radarr anymore
  function ensureSelectOption(selectId, value) {
    const sel = document.getElementById(selectId);
    if (!sel) return;
    const v = (value ?? "").toString();
    if (!v) return;

    for (const opt of sel.options) {
      if (opt.value === v) return;
    }

    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v + " (missing in Radarr)";
    sel.insertBefore(opt, sel.firstChild);
  }

  // ---------- Job modal ----------
  function openNewJob() {
    const form = document.getElementById("jobForm");
    if (!form) return;

    form.action = "/jobs/save";
    setVal("job_id", "");
    setVal("job_name", "New Job");
    setVal("job_enabled", "1");

    // tag starts empty until user picks
    setVal("job_tag", "");

    setVal("job_days", "30");
    setVal("job_day", "daily");
    setVal("job_hour", "3");
    setChecked("job_dry", true);
    setChecked("job_delete", true);
    setChecked("job_excl", false);

    const t = document.getElementById("jobTitle");
    if (t) t.textContent = "Add Job";
    showModal("jobBack");
  }

  function openEditJob(btn) {
    const form = document.getElementById("jobForm");
    if (!form || !btn) return;

    form.action = "/jobs/save";
    setVal("job_id", btn.getAttribute("data-id") || "");
    setVal("job_name", btn.getAttribute("data-name") || "Job");
    setVal("job_enabled", (btn.getAttribute("data-enabled") || "1"));

    const tag = btn.getAttribute("data-tag") || "";
    ensureSelectOption("job_tag", tag);
    setVal("job_tag", tag);

    setVal("job_days", btn.getAttribute("data-days") || "30");
    setVal("job_day", btn.getAttribute("data-day") || "daily");
    setVal("job_hour", btn.getAttribute("data-hour") || "3");
    setChecked("job_dry", (btn.getAttribute("data-dry") || "1") === "1");
    setChecked("job_delete", (btn.getAttribute("data-del") || "1") === "1");
    setChecked("job_excl", (btn.getAttribute("data-excl") || "0") === "1");

    const t = document.getElementById("jobTitle");
    if (t) t.textContent = "Edit Job";
    showModal("jobBack");
  }

  // ---------- Run Now confirm ----------
  function openRunNowConfirm(jobId) {
    const hid = document.getElementById("runNowJobId");
    if (hid) hid.value = jobId || "";
    showModal("runNowBack");
  }
  function runNowSubmitConfirm() {
    const form = document.getElementById("runNowFormConfirm");
    if (form) form.submit();
  }

  // ---------- Settings: dirty + save gating ----------
  function isDirty(settingsForm) {
    if (!settingsForm) return false;
    const els = settingsForm.querySelectorAll("input, select, textarea");
    for (const el of els) {
      const init = el.getAttribute("data-initial");
      if (init === null) continue;

      let cur;
      if (el.type === "checkbox") cur = el.checked ? "1" : "0";
      else cur = (el.value ?? "");

      if (cur !== init) return true;
    }
    return false;
  }

  function updateSaveState() {
    const settingsForm = document.getElementById("settingsForm");
    const saveBtn = document.getElementById("saveSettingsBtn");
    if (!settingsForm || !saveBtn) return;

    const radarrOk = settingsForm.getAttribute("data-radarr-ok") === "1";
    const sonarrOk = settingsForm.getAttribute("data-sonarr-ok") === "1";
    const dirty = isDirty(settingsForm);

    const sonarrUrl = (document.querySelector('input[name="SONARR_URL"]')?.value || "").trim();
    const sonarrKey = (document.querySelector('input[name="SONARR_API_KEY"]')?.value || "").trim();
    const sonarrConfigured = !!(sonarrUrl || sonarrKey);
    const sonarrReady = !sonarrConfigured || sonarrOk;

    saveBtn.disabled = !(radarrOk && sonarrReady && dirty);
    if (!radarrOk) saveBtn.title = "Test Radarr connection first";
    else if (!sonarrReady) saveBtn.title = "Test Sonarr connection first (or clear Sonarr fields)";
    else saveBtn.title = dirty ? "Save settings" : "No changes to save";
  }

  function onSettingsEdited(e) {
    const settingsForm = document.getElementById("settingsForm");
    if (!settingsForm) return;

    if (e.target && (e.target.name === "RADARR_URL" || e.target.name === "RADARR_API_KEY")) {
      settingsForm.setAttribute("data-radarr-ok", "0");
      const testBtn = document.getElementById("testRadarrBtn");
      if (testBtn) {
        testBtn.disabled = false;
        testBtn.title = "Test Radarr connection";
        testBtn.textContent = "Test Connection";
      }
    }

    if (e.target && (e.target.name === "SONARR_URL" || e.target.name === "SONARR_API_KEY")) {
      settingsForm.setAttribute("data-sonarr-ok", "0");
      const testBtn = document.getElementById("testSonarrBtn");
      if (testBtn) {
        testBtn.disabled = false;
        testBtn.title = "Test Sonarr connection";
        testBtn.textContent = "Test Connection";
      }
    }

    updateSaveState();
  }

  document.addEventListener("input", onSettingsEdited);
  document.addEventListener("change", onSettingsEdited);

  // ---------- Reopen job modal after server-side validation errors ----------
  document.addEventListener("DOMContentLoaded", () => {
    updateSaveState();

    const host = document.getElementById("toastHost");
    if (host) setTimeout(() => { try { host.remove(); } catch(e){} }, 6000);

    const params = new URLSearchParams(window.location.search);
    if (params.get("modal") === "job") {
      const jid = params.get("job_id") || "";
      const name = params.get("name") || "New Job";
      const enabled = params.get("enabled") || "1";
      const tag = params.get("TAG_LABEL") || "";
      const days = params.get("DAYS_OLD") || "30";
      const day = params.get("SCHED_DAY") || "daily";
      const hour = params.get("SCHED_HOUR") || "3";
      const dry = (params.get("DRY_RUN") || "1") === "1";
      const del = (params.get("DELETE_FILES") || "1") === "1";
      const excl = (params.get("ADD_IMPORT_EXCLUSION") || "0") === "1";

      const title = document.getElementById("jobTitle");
      if (title) title.textContent = jid ? "Edit Job" : "Add Job";

      setVal("job_id", jid);
      setVal("job_name", decodeURIComponent(name));
      setVal("job_enabled", enabled);

      const tagDecoded = decodeURIComponent(tag || "");
      ensureSelectOption("job_tag", tagDecoded);
      setVal("job_tag", tagDecoded);

      setVal("job_days", days);
      setVal("job_day", day);
      setVal("job_hour", hour);
      setChecked("job_dry", dry);
      setChecked("job_delete", del);
      setChecked("job_excl", excl);

      showModal("jobBack");
    }
  });
</script>
"""


def shell(page_title: str, active: str, body: str):
    cfg = load_config()
    theme = (cfg.get("UI_THEME") or "dark").lower()
    if theme not in ("dark", "light"):
        theme = "dark"

    def pill(name, href, key):
        cls = "pill active" if active == key else "pill"
        return f'<a class="{cls}" href="{href}">{name}</a>'

    theme_label = "Light" if theme == "dark" else "Dark"
    theme_btn = f"""
      <form method="post" action="/toggle-theme" style="margin:0;">
        <button class="pill" type="submit">Theme: {theme_label}</button>
      </form>
    """

    nav = (
        pill("Dashboard", "/dashboard", "dash")
        + pill("Jobs", "/jobs", "jobs")
        + pill("Settings", "/settings", "settings")
        + pill("Status", "/status", "status")
        + theme_btn
    )

    has_logo = find_logo_path() is not None
    logo_html = (
        '<div class="logoWrap"><img class="logoImg" src="/logo" alt="logo"></div>'
        if has_logo
        else '<div class="logoBadge"></div>'
    )

    toasts = render_toasts()

    return f"""
<!doctype html>
<html>
<head>
  <title>{page_title}</title>
  {BASE_HEAD}
</head>
<body data-theme="{theme}">
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        {logo_html}
        <div class="title">
          <h1>mediareaparr</h1>
          <div class="sub">Radarr tag + age cleanup • multi-job scheduler • WebUI</div>
        </div>
      </div>
      <div class="nav">{nav}</div>
    </div>

    {body}
  </div>

  {toasts}
</body>
</html>
"""


# --------------------------
# Routes
# --------------------------
@app.get("/")
def home():
    return redirect("/dashboard")


@app.get("/logo")
def logo():
    p = find_logo_path()
    if not p:
        return ("", 404)
    return send_file(p, mimetype=logo_mime(p), conditional=True)


@app.post("/toggle-theme")
def toggle_theme():
    cfg = load_config()
    cur = (cfg.get("UI_THEME") or "dark").lower()
    cfg["UI_THEME"] = "light" if cur != "light" else "dark"
    save_config(cfg)
    flash(f"Theme set to {cfg['UI_THEME']} ✔", "success")
    return redirect(request.referrer or "/dashboard")


# -------- Reset buttons --------
@app.post("/reset-radarr")
def reset_radarr():
    cfg = load_config()
    cfg["RADARR_URL"] = ""
    cfg["RADARR_API_KEY"] = ""
    cfg["RADARR_OK"] = False
    save_config(cfg)
    flash("Radarr settings cleared ✔", "success")
    return redirect("/settings")


@app.post("/reset-sonarr")
def reset_sonarr():
    cfg = load_config()
    cfg["SONARR_URL"] = ""
    cfg["SONARR_API_KEY"] = ""
    cfg["SONARR_OK"] = False
    save_config(cfg)
    flash("Sonarr settings cleared ✔", "success")
    return redirect("/settings")


# -------- Test connections (save values on success) --------
@app.post("/test-radarr")
def test_radarr():
    cfg = load_config()

    url = (request.form.get("RADARR_URL") or cfg.get("RADARR_URL") or "").rstrip("/")
    api_key = request.form.get("RADARR_API_KEY") or cfg.get("RADARR_API_KEY") or ""

    cfg["RADARR_OK"] = False
    save_config(cfg)

    if not url:
        flash("Radarr URL is empty.", "error")
        return redirect("/settings")
    if not api_key:
        flash("Radarr API Key is empty.", "error")
        return redirect("/settings")

    try:
        r = requests.get(
            url + "/api/v3/system/status",
            headers={"X-Api-Key": api_key},
            timeout=int(cfg.get("HTTP_TIMEOUT_SECONDS", 30)),
        )
        if r.status_code in (401, 403):
            flash("Radarr connection failed: Unauthorized (API key incorrect).", "error")
            return redirect("/settings")

        r.raise_for_status()

        cfg["RADARR_URL"] = url
        cfg["RADARR_API_KEY"] = api_key
        cfg["RADARR_OK"] = True
        save_config(cfg)

        flash("Radarr connected ✔", "success")
        return redirect("/settings")

    except requests.exceptions.ConnectTimeout:
        flash("Radarr connection failed: timeout connecting to the host.", "error")
    except requests.exceptions.ConnectionError:
        flash("Radarr connection failed: could not connect (URL/host/network).", "error")
    except Exception as e:
        flash(f"Radarr connection failed: {e}", "error")

    return redirect("/settings")


@app.post("/test-sonarr")
def test_sonarr():
    cfg = load_config()

    url = (request.form.get("SONARR_URL") or cfg.get("SONARR_URL") or "").rstrip("/")
    api_key = request.form.get("SONARR_API_KEY") or cfg.get("SONARR_API_KEY") or ""

    cfg["SONARR_OK"] = False
    save_config(cfg)

    if not url:
        flash("Sonarr URL is empty.", "error")
        return redirect("/settings")
    if not api_key:
        flash("Sonarr API Key is empty.", "error")
        return redirect("/settings")

    try:
        r = requests.get(
            url + "/api/v3/system/status",
            headers={"X-Api-Key": api_key},
            timeout=int(cfg.get("HTTP_TIMEOUT_SECONDS", 30)),
        )
        if r.status_code in (401, 403):
            flash("Sonarr connection failed: Unauthorized (API key incorrect).", "error")
            return redirect("/settings")

        r.raise_for_status()

        cfg["SONARR_URL"] = url
        cfg["SONARR_API_KEY"] = api_key
        cfg["SONARR_OK"] = True
        save_config(cfg)

        flash("Sonarr connected ✔", "success")
        return redirect("/settings")

    except requests.exceptions.ConnectTimeout:
        flash("Sonarr connection failed: timeout connecting to the host.", "error")
    except requests.exceptions.ConnectionError:
        flash("Sonarr connection failed: could not connect (URL/host/network).", "error")
    except Exception as e:
        flash(f"Sonarr connection failed: {e}", "error")

    return redirect("/settings")


@app.get("/settings")
def settings():
    cfg = load_config()

    radarr_ok = bool(cfg.get("RADARR_OK"))
    sonarr_ok = bool(cfg.get("SONARR_OK"))

    test_label = "Connected" if radarr_ok else "Test Connection"
    test_disabled_attr = "disabled" if radarr_ok else ""
    test_title = "Radarr connection is OK" if radarr_ok else "Test Radarr connection"

    sonarr_test_label = "Connected" if sonarr_ok else "Test Connection"
    sonarr_test_disabled_attr = "disabled" if sonarr_ok else ""
    sonarr_test_title = "Sonarr connection is OK" if sonarr_ok else "Test Sonarr connection"

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Settings</h2>
            <div class="btnrow">
              <a class="btn" href="/jobs">Manage Jobs</a>
              <form method="post" action="/apply-cron" style="margin:0;">
                <button class="btn warn" type="submit">Apply Cron</button>
              </form>
            </div>
          </div>

          <div class="bd">
            <form id="settingsForm"
                  method="post"
                  action="/save-settings"
                  data-radarr-ok="{ '1' if radarr_ok else '0' }"
                  data-sonarr-ok="{ '1' if sonarr_ok else '0' }"
                  style="margin:0;">

              <div class="card" style="box-shadow:none; margin-bottom:14px;">
                <div class="hd"><h2>Radarr setup</h2></div>
                <div class="bd">
                  <div class="form">
                    <div class="field">
                      <label>Radarr URL</label>
                      <input type="text" name="RADARR_URL"
                             value="{safe_html(cfg["RADARR_URL"])}"
                             data-initial="{safe_html(cfg["RADARR_URL"])}">
                    </div>
                    <div class="field">
                      <label>Radarr API Key</label>
                      <input type="password" name="RADARR_API_KEY"
                             value="{safe_html(cfg["RADARR_API_KEY"])}"
                             data-initial="{safe_html(cfg["RADARR_API_KEY"])}">
                    </div>
                  </div>

                  <div class="btnrow" style="margin-top:14px;">
                    <button id="testRadarrBtn"
                            class="btn good"
                            type="submit"
                            formaction="/test-radarr"
                            formmethod="post"
                            {test_disabled_attr}
                            title="{safe_html(test_title)}">{safe_html(test_label)}</button>

                    <button class="btn bad"
                            type="submit"
                            formaction="/reset-radarr"
                            formmethod="post"
                            onclick="return confirm('Clear Radarr URL/API key?');">Reset Radarr</button>
                  </div>
                </div>
              </div>

              <div class="card" style="box-shadow:none; margin-bottom:14px;">
                <div class="hd">
                  <h2>Sonarr setup</h2>
                  <div class="muted">Optional</div>
                </div>
                <div class="bd">
                  <div class="form">
                    <div class="field">
                      <label>Sonarr URL</label>
                      <input type="text" name="SONARR_URL"
                             value="{safe_html(cfg["SONARR_URL"])}"
                             data-initial="{safe_html(cfg["SONARR_URL"])}">
                    </div>
                    <div class="field">
                      <label>Sonarr API Key</label>
                      <input type="password" name="SONARR_API_KEY"
                             value="{safe_html(cfg["SONARR_API_KEY"])}"
                             data-initial="{safe_html(cfg["SONARR_API_KEY"])}">
                    </div>
                  </div>

                  <div class="btnrow" style="margin-top:14px;">
                    <button id="testSonarrBtn"
                            class="btn good"
                            type="submit"
                            formaction="/test-sonarr"
                            formmethod="post"
                            {sonarr_test_disabled_attr}
                            title="{safe_html(sonarr_test_title)}">{safe_html(sonarr_test_label)}</button>

                    <button class="btn bad"
                            type="submit"
                            formaction="/reset-sonarr"
                            formmethod="post"
                            onclick="return confirm('Clear Sonarr URL/API key?');">Reset Sonarr</button>

                    <div class="muted">Leave blank if you don’t use Sonarr.</div>
                  </div>
                </div>
              </div>

              <div class="card" style="box-shadow:none;">
                <div class="hd">
                  <h2>WebUI</h2>
                  <div class="muted">Global settings</div>
                </div>
                <div class="bd">
                  <div class="form">
                    <div class="field">
                      <label>HTTP Timeout Seconds</label>
                      <input type="number" min="5" name="HTTP_TIMEOUT_SECONDS"
                             value="{cfg["HTTP_TIMEOUT_SECONDS"]}"
                             data-initial="{cfg["HTTP_TIMEOUT_SECONDS"]}">
                    </div>

                    <div class="field">
                      <label>UI Theme</label>
                      <select name="UI_THEME" data-initial="{safe_html(cfg.get("UI_THEME","dark"))}">
                        <option value="dark" {"selected" if cfg.get("UI_THEME","dark")=="dark" else ""}>Dark</option>
                        <option value="light" {"selected" if cfg.get("UI_THEME","dark")=="light" else ""}>Light</option>
                      </select>
                    </div>
                  </div>

                  <div class="btnrow" style="margin-top:14px;">
                    <button id="saveSettingsBtn" class="btn primary" type="submit" disabled>Save Settings</button>
                  </div>
                </div>
              </div>

            </form>
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Settings", "settings", body))


@app.post("/save-settings")
def save_settings():
    old = load_config()
    cfg = load_config()

    cfg["RADARR_URL"] = (request.form.get("RADARR_URL") or "").rstrip("/")
    cfg["RADARR_API_KEY"] = request.form.get("RADARR_API_KEY") or ""
    cfg["SONARR_URL"] = (request.form.get("SONARR_URL") or "").rstrip("/")
    cfg["SONARR_API_KEY"] = request.form.get("SONARR_API_KEY") or ""

    cfg["HTTP_TIMEOUT_SECONDS"] = clamp_int(request.form.get("HTTP_TIMEOUT_SECONDS") or 30, 5, 300, 30)
    cfg["UI_THEME"] = (request.form.get("UI_THEME") or cfg.get("UI_THEME", "dark")).lower()
    if cfg["UI_THEME"] not in ("dark", "light"):
        cfg["UI_THEME"] = "dark"

    if old.get("RADARR_URL") != cfg["RADARR_URL"] or old.get("RADARR_API_KEY") != cfg["RADARR_API_KEY"]:
        cfg["RADARR_OK"] = False
    if old.get("SONARR_URL") != cfg["SONARR_URL"] or old.get("SONARR_API_KEY") != cfg["SONARR_API_KEY"]:
        cfg["SONARR_OK"] = False

    if not cfg.get("RADARR_OK", False):
        flash("Please click Test Connection for Radarr and make sure it shows Connected before saving.", "error")
        save_config(cfg)
        return redirect("/settings")

    sonarr_configured = bool((cfg.get("SONARR_URL") or "").strip() or (cfg.get("SONARR_API_KEY") or "").strip())
    if sonarr_configured and not cfg.get("SONARR_OK", False):
        flash("Please click Test Connection for Sonarr (or clear Sonarr fields) before saving.", "error")
        save_config(cfg)
        return redirect("/settings")

    save_config(cfg)
    flash("Settings saved ✔", "success")
    return redirect("/settings")


@app.get("/jobs")
def jobs_page():
    cfg = load_config()

    # Fetch Radarr tags for Tag Label dropdown
    labels = []
    if cfg.get("RADARR_URL") and cfg.get("RADARR_API_KEY") and cfg.get("RADARR_OK"):
        try:
            tags = radarr_get(cfg, "/api/v3/tag")
            labels = sorted({t.get("label") for t in (tags or []) if t.get("label")}, key=lambda x: str(x).lower())
        except Exception:
            labels = []

    tag_opts = '<option value="" selected disabled>-- Select a tag --</option>' + "".join(
        f'<option value="{safe_html(lbl)}">{safe_html(lbl)}</option>'
        for lbl in labels
    )

    hour_opts = "".join([f'<option value="{h}">{h:02d}:00</option>' for h in range(0, 24)])

    job_modal = f"""
    <div class="modalBack" id="jobBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="jobTitle">
        <div class="mh">
          <h3 id="jobTitle">Add Job</h3>
        </div>

        <form id="jobForm" method="post" action="/jobs/save" style="margin:0;">
          <div class="mb">
            <input type="hidden" name="job_id" id="job_id" value="">

            <div class="form">
              <div class="field">
                <label>Job Name</label>
                <input type="text" name="name" id="job_name" value="New Job" required>
              </div>

              <div class="field">
                <label>Enabled</label>
                <select name="enabled" id="job_enabled">
                  <option value="1">Enabled</option>
                  <option value="0">Disabled</option>
                </select>
              </div>

              <div class="field">
                <label>Tag Label</label>
                <select name="TAG_LABEL" id="job_tag" required>
                  {tag_opts}
                </select>
              </div>

              <div class="field">
                <label>Days Old</label>
                <input type="number" min="1" name="DAYS_OLD" id="job_days" value="30" required>
              </div>

              <div class="field">
                <label>Scheduler Day</label>
                <select name="SCHED_DAY" id="job_day">
                  <option value="daily">Daily</option>
                  <option value="mon">Monday</option>
                  <option value="tue">Tuesday</option>
                  <option value="wed">Wednesday</option>
                  <option value="thu">Thursday</option>
                  <option value="fri">Friday</option>
                  <option value="sat">Saturday</option>
                  <option value="sun">Sunday</option>
                </select>
              </div>

              <div class="field">
                <label>Scheduler Time</label>
                <select name="SCHED_HOUR" id="job_hour">
                  {hour_opts}
                </select>
              </div>
            </div>

            <div class="checks" style="margin-top:12px;">
              <label class="check">
                <input type="checkbox" id="job_dry" name="DRY_RUN" checked>
                <div>
                  <div style="font-weight:700;">Dry Run</div>
                  <div class="muted">Log only; no deletes.</div>
                </div>
              </label>

              <label class="check">
                <input type="checkbox" id="job_delete" name="DELETE_FILES" checked>
                <div>
                  <div style="font-weight:700;">Delete Files</div>
                  <div class="muted">Remove movie files from disk.</div>
                </div>
              </label>

              <label class="check">
                <input type="checkbox" id="job_excl" name="ADD_IMPORT_EXCLUSION">
                <div>
                  <div style="font-weight:700;">Add Import Exclusion</div>
                  <div class="muted">Prevents Radarr re-import.</div>
                </div>
              </label>
            </div>
          </div>

          <div class="mf">
            <button class="btn" type="button" onclick="hideModal('jobBack')">Cancel</button>
            <button class="btn primary" type="submit">Save Job</button>
          </div>
        </form>
      </div>
    </div>
    """

    run_confirm_modal = """
    <div class="modalBack" id="runNowBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="runNowTitle">
        <div class="mh">
          <h3 id="runNowTitle">Run Now confirmation</h3>
        </div>
        <div class="mb">
          <p><b>Dry Run is OFF.</b> This job may delete movie files via Radarr.</p>
          <p class="muted">If you’re not sure, edit the job and enable <b>Dry Run</b>, then use Preview.</p>
        </div>
        <div class="mf">
          <button class="btn" type="button" onclick="hideModal('runNowBack')">Cancel</button>
          <form id="runNowFormConfirm" method="post" action="/jobs/run-now" style="margin:0;">
            <input type="hidden" id="runNowJobId" name="job_id" value="">
            <button class="btn bad" type="button" onclick="runNowSubmitConfirm()">Yes, run now</button>
          </form>
        </div>
      </div>
    </div>
    """

    job_cards = []
    for j in cfg["JOBS"]:
        sched = schedule_label(j["SCHED_DAY"], j["SCHED_HOUR"])
        enabled_cls = "ok" if j["enabled"] else "off"
        enabled_text = "Enabled" if j["enabled"] else "Disabled"
        dry = "on" if j["DRY_RUN"] else "OFF"
        delete_files = "on" if j["DELETE_FILES"] else "off"

        if j["DRY_RUN"]:
            run_now_html = f"""
              <form method="post" action="/jobs/run-now" style="margin:0;">
                <input type="hidden" name="job_id" value="{safe_html(j["id"])}">
                <button class="btn good" type="submit">Run Now</button>
              </form>
            """
        else:
            run_now_html = f"""
              <button class="btn bad" type="button" onclick="openRunNowConfirm('{safe_html(j["id"])}')">Run Now</button>
            """

        job_cards.append(f"""
          <div class="jobCard">
            <div class="jobTop">
              <div>
                <div class="jobName">{safe_html(j["name"])}</div>
                <div class="jobMeta">
                  Tag: <code>{safe_html(j["TAG_LABEL"])}</code> • Older than <code>{j["DAYS_OLD"]}</code> days<br>
                  Schedule: <b>{safe_html(sched)}</b> • Dry-run: <b>{dry}</b> • Delete files: <b>{delete_files}</b>
                </div>
              </div>
              <div class="btnrow">
                {run_now_html}
                <a class="btn" href="/preview?job_id={safe_html(j["id"])}">Preview</a>
              </div>
            </div>

            <div class="jobBody">
              <div class="btnrow">
                <span class="tagPill {enabled_cls}">{enabled_text}</span>
                <span class="tagPill">ID: <code>{safe_html(j["id"])}</code></span>
              </div>

              <div class="btnrow">
                <button class="btn"
                        type="button"
                        onclick="openEditJob(this)"
                        data-id="{safe_html(j["id"])}"
                        data-name="{safe_html(j["name"])}"
                        data-enabled="{ '1' if j["enabled"] else '0' }"
                        data-tag="{safe_html(j["TAG_LABEL"])}"
                        data-days="{j["DAYS_OLD"]}"
                        data-day="{safe_html(j["SCHED_DAY"])}"
                        data-hour="{j["SCHED_HOUR"]}"
                        data-dry="{ '1' if j["DRY_RUN"] else '0' }"
                        data-del="{ '1' if j["DELETE_FILES"] else '0' }"
                        data-excl="{ '1' if j["ADD_IMPORT_EXCLUSION"] else '0' }">Edit</button>

                <form method="post" action="/jobs/delete" style="margin:0;"
                      onsubmit="return confirm('Are you sure you want to delete this job?');">
                  <input type="hidden" name="job_id" value="{safe_html(j["id"])}">
                  <button class="btn bad" type="submit">Delete</button>
                </form>
              </div>
            </div>
          </div>
        """)

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Jobs</h2>
            <div class="btnrow">
              <button class="btn primary" type="button" onclick="openNewJob()">Add Job</button>
              <form method="post" action="/apply-cron" style="margin:0;">
                <button class="btn warn" type="submit">Apply Cron</button>
              </form>
            </div>
          </div>

          <div class="bd">
            <div class="jobsGrid">
              {''.join(job_cards)}
            </div>
          </div>
        </div>
      </div>

      {job_modal}
      {run_confirm_modal}
    """
    return render_template_string(shell("mediareaparr • Jobs", "jobs", body))


@app.post("/jobs/save")
def jobs_save():
    cfg = load_config()
    try:
        job_id = (request.form.get("job_id") or "").strip()
        name = (request.form.get("name") or "Job").strip()
        enabled = (request.form.get("enabled") or "1").strip() == "1"

        tag_label = (request.form.get("TAG_LABEL") or "").strip()
        if not tag_label:
            raise ValueError("Please select a tag.")

        job = {
            "id": job_id or make_job_id(),
            "name": name,
            "enabled": enabled,
            "TAG_LABEL": tag_label,
            "DAYS_OLD": clamp_int(request.form.get("DAYS_OLD") or 30, 1, 36500, 30),
            "SCHED_DAY": (request.form.get("SCHED_DAY") or "daily").lower(),
            "SCHED_HOUR": clamp_int(request.form.get("SCHED_HOUR") or 3, 0, 23, 3),
            "DRY_RUN": checkbox("DRY_RUN"),
            "DELETE_FILES": checkbox("DELETE_FILES"),
            "ADD_IMPORT_EXCLUSION": checkbox("ADD_IMPORT_EXCLUSION"),
        }
        job = normalize_job(job)

        jobs = cfg.get("JOBS") or []
        replaced = False
        for i, j in enumerate(jobs):
            if str(j.get("id")) == job["id"]:
                jobs[i] = job
                replaced = True
                break
        if not replaced:
            jobs.append(job)

        cfg["JOBS"] = [normalize_job(j) for j in jobs]
        save_config(cfg)

        flash("Job saved ✔", "success")
        return redirect("/jobs")

    except Exception as e:
        flash(str(e), "error")
        from urllib.parse import urlencode
        qs = urlencode({
            "modal": "job",
            "job_id": request.form.get("job_id", ""),
            "name": request.form.get("name", ""),
            "enabled": request.form.get("enabled", "1"),
            "TAG_LABEL": request.form.get("TAG_LABEL", ""),
            "DAYS_OLD": request.form.get("DAYS_OLD", ""),
            "SCHED_DAY": request.form.get("SCHED_DAY", ""),
            "SCHED_HOUR": request.form.get("SCHED_HOUR", ""),
            "DRY_RUN": "1" if checkbox("DRY_RUN") else "0",
            "DELETE_FILES": "1" if checkbox("DELETE_FILES") else "0",
            "ADD_IMPORT_EXCLUSION": "1" if checkbox("ADD_IMPORT_EXCLUSION") else "0",
        }, doseq=False)
        return redirect(f"/jobs?{qs}")


@app.post("/jobs/delete")
def jobs_delete():
    cfg = load_config()
    job_id = (request.form.get("job_id") or "").strip()
    jobs = [j for j in (cfg.get("JOBS") or []) if str(j.get("id")) != job_id]
    if not jobs:
        j = job_defaults()
        j["name"] = "Default Job"
        j["TAG_LABEL"] = ""
        jobs = [normalize_job(j)]

    cfg["JOBS"] = [normalize_job(j) for j in jobs]
    save_config(cfg)
    flash("Job deleted ✔", "success")
    return redirect("/jobs")


@app.post("/jobs/run-now")
def jobs_run_now():
    job_id = (request.form.get("job_id") or "").strip()
    if not job_id:
        flash("Missing job id.", "error")
        return redirect("/jobs")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / f"run_now_{job_id}.flag").write_text(now_iso(), encoding="utf-8")
    flash("Run Now triggered ✔ (check logs/dashboard)", "success")
    return redirect("/dashboard")


@app.post("/apply-cron")
def apply_cron():
    cfg = load_config()
    jobs = cfg.get("JOBS") or []
    enabled_jobs = [j for j in jobs if j.get("enabled")]

    if not enabled_jobs:
        flash("No enabled jobs to schedule.", "error")
        return redirect(request.referrer or "/jobs")

    log_path = "/var/log/mediareaparr.log"
    lines = []
    for j in enabled_jobs:
        cron = cron_from_day_hour(j.get("SCHED_DAY", "daily"), int(j.get("SCHED_HOUR", 3)))
        jid = str(j.get("id"))
        lines.append(f"{cron} python /app/app.py --job-id {jid} >> {log_path} 2>&1")

    cron_text = "\n".join(lines) + "\n"

    try:
        with open("/etc/crontabs/root", "w", encoding="utf-8") as f:
            f.write(cron_text)
        os.kill(1, signal.SIGHUP)
        flash("Cron schedule applied successfully ✔", "success")
    except Exception as e:
        flash(f"Failed to apply cron: {e}", "error")

    return redirect(request.referrer or "/jobs")


@app.get("/preview")
def preview():
    cfg = load_config()
    job_id = (request.args.get("job_id") or "").strip()

    job = next((normalize_job(j) for j in (cfg.get("JOBS") or []) if str(j.get("id")) == job_id), None)
    if not job:
        job = normalize_job((cfg.get("JOBS") or [job_defaults()])[0])

    try:
        result = preview_candidates(cfg, job)
        error = result.get("error")
        candidates = result.get("candidates", [])
        cutoff = result.get("cutoff", "")

        if error:
            flash(error, "error")
            return redirect("/jobs")

        rows = ""
        for c in candidates[:500]:
            rows += f"""
              <tr>
                <td>{c["age_days"]}</td>
                <td>{safe_html(c.get("title",""))}</td>
                <td>{safe_html(str(c.get("year","")))}</td>
                <td><code>{safe_html(c.get("added",""))}</code></td>
                <td>{safe_html(str(c.get("id","")))}</td>
                <td class="muted">{safe_html(c.get("path","") or "")}</td>
              </tr>
            """

        if job["DRY_RUN"]:
            run_now_html = f"""
              <form method="post" action="/jobs/run-now" style="margin:0;">
                <input type="hidden" name="job_id" value="{safe_html(job["id"])}">
                <button class="btn good" type="submit">Run Now</button>
              </form>
            """
        else:
            run_now_html = f"""
              <button class="btn bad" type="button" onclick="openRunNowConfirm('{safe_html(job["id"])}')">Run Now</button>
            """

        run_confirm_modal = """
        <div class="modalBack" id="runNowBack">
          <div class="modal" role="dialog" aria-modal="true" aria-labelledby="runNowTitle">
            <div class="mh"><h3 id="runNowTitle">Run Now confirmation</h3></div>
            <div class="mb">
              <p><b>Dry Run is OFF.</b> This job may delete movie files via Radarr.</p>
              <p class="muted">If you’re not sure, edit the job and enable <b>Dry Run</b>, then use Preview.</p>
            </div>
            <div class="mf">
              <button class="btn" type="button" onclick="hideModal('runNowBack')">Cancel</button>
              <form id="runNowFormConfirm" method="post" action="/jobs/run-now" style="margin:0;">
                <input type="hidden" id="runNowJobId" name="job_id" value="">
                <button class="btn bad" type="button" onclick="runNowSubmitConfirm()">Yes, run now</button>
              </form>
            </div>
          </div>
        </div>
        """

        body = f"""
          <div class="grid">
            <div class="card">
              <div class="hd">
                <h2>Preview candidates</h2>
                <div class="btnrow">
                  <a class="btn" href="/jobs">Back to Jobs</a>
                  {run_now_html}
                </div>
              </div>
              <div class="bd">
                <div class="muted">
                  Job: <b>{safe_html(job["name"])}</b> • Tag <code>{safe_html(job["TAG_LABEL"])}</code> • Older than <code>{job["DAYS_OLD"]}</code> days
                </div>
                <div class="muted" style="margin-top:6px;">Found <b>{len(candidates)}</b> candidate(s). Preview only (no deletes).</div>
                <div class="muted" style="margin-top:6px;">Cutoff: <code>{safe_html(cutoff)}</code></div>

                <div class="tablewrap" style="margin-top:12px;">
                  <table>
                    <thead>
                      <tr>
                        <th>Age (days)</th>
                        <th>Title</th>
                        <th>Year</th>
                        <th>Added</th>
                        <th>ID</th>
                        <th>Path</th>
                      </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                  </table>
                </div>
                <div class="muted" style="margin-top:10px;">Showing up to 500.</div>
              </div>
            </div>
          </div>
          {run_confirm_modal}
        """
        return render_template_string(shell("mediareaparr • Preview", "jobs", body))

    except Exception as e:
        flash(f"Preview failed: {e}", "error")
        return redirect("/dashboard")


@app.get("/dashboard")
def dashboard():
    state = load_state()
    last_run = state.get("last_run")

    if not last_run:
        body = """
          <div class="grid">
            <div class="card">
              <div class="hd">
                <h2>Dashboard</h2>
                <div class="btnrow">
                  <a class="btn" href="/jobs">Jobs</a>
                  <a class="btn" href="/settings">Settings</a>
                </div>
              </div>
              <div class="bd">
                <div class="muted">No runs recorded yet.</div>
              </div>
            </div>
          </div>
        """
        return render_template_string(shell("mediareaparr • Dashboard", "dash", body))

    status_text = str(last_run.get("status") or "").upper()
    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Dashboard</h2>
            <div class="btnrow">
              <a class="btn" href="/jobs">Jobs</a>
              <a class="btn" href="/settings">Settings</a>
            </div>
          </div>
          <div class="bd">
            <div class="muted">Last run status: <b>{safe_html(status_text)}</b></div>
            <div class="muted" style="margin-top:6px;">Job: <b>{safe_html(str(last_run.get("job_name","")))}</b> (<code>{safe_html(str(last_run.get("job_id","")))}</code>)</div>
            <div class="muted" style="margin-top:6px;">Finished: <code>{safe_html(str(last_run.get("finished_at","")))}</code></div>
            <div class="muted" style="margin-top:6px;">Candidates: <b>{safe_html(str(last_run.get("candidates_found",0)))}</b></div>
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Dashboard", "dash", body))


@app.get("/status")
def status():
    cfg = load_config()
    state = load_state()

    def render_kv(d: Dict[str, Any]) -> str:
        rows = []
        for k, v in d.items():
            if k == "JOBS":
                rows.append(f"<tr><td><code>{safe_html(k)}</code></td><td class='muted'>[{len(v or [])} jobs]</td></tr>")
            else:
                rows.append(f"<tr><td><code>{safe_html(k)}</code></td><td class='muted'>{safe_html(str(v))}</td></tr>")
        return "".join(rows)

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd"><h2>Status</h2></div>
          <div class="bd">
            <div class="muted">Config file: <code>{safe_html(str(CONFIG_PATH))}</code> (exists: <b>{str(CONFIG_PATH.exists()).lower()}</b>)</div>
            <div class="muted" style="margin-top:8px;">State file: <code>{safe_html(str(STATE_PATH))}</code> (exists: <b>{str(STATE_PATH.exists()).lower()}</b>)</div>

            <div style="margin-top:14px;" class="tablewrap">
              <table>
                <thead><tr><th>Config Key</th><th>Value</th></tr></thead>
                <tbody>{render_kv(cfg)}</tbody>
              </table>
            </div>

            <div style="margin-top:14px;" class="tablewrap">
              <table>
                <thead><tr><th>State Key</th><th>Value</th></tr></thead>
                <tbody>{render_kv(state)}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Status", "status", body))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("WEBUI_PORT", "7575")))
    args = p.parse_args()
    app.run(host=args.host, port=args.port)
