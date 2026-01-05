import os
import json
import signal
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

import requests
from flask import (
    Flask, request, redirect, render_template_string,
    flash, get_flashed_messages, send_file
)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

# Logo files (put one of these in /config or /config/logo)
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
# Config / State
# --------------------------
def env_default(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def load_config():
    cfg = {
        "RADARR_URL": env_default("RADARR_URL", "http://radarr:7878").rstrip("/"),
        "RADARR_API_KEY": env_default("RADARR_API_KEY", ""),
        "TAG_LABEL": env_default("TAG_LABEL", "autodelete30"),
        "DAYS_OLD": int(env_default("DAYS_OLD", "30")),
        "DRY_RUN": env_default("DRY_RUN", "true").lower() == "true",
        "DELETE_FILES": env_default("DELETE_FILES", "true").lower() == "true",
        "ADD_IMPORT_EXCLUSION": env_default("ADD_IMPORT_EXCLUSION", "false").lower() == "true",
        "CRON_SCHEDULE": env_default("CRON_SCHEDULE", "15 3 * * *"),
        "RUN_ON_STARTUP": env_default("RUN_ON_STARTUP", "false").lower() == "true",
        "HTTP_TIMEOUT_SECONDS": int(env_default("HTTP_TIMEOUT_SECONDS", "30")),
        "UI_THEME": env_default("UI_THEME", "dark"),  # "dark" or "light"
        "RADARR_OK": False,  # must pass Test to enable Save
    }

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: data[k] for k in data.keys() if k in cfg})
        except Exception:
            pass

    # Normalize theme
    t = (cfg.get("UI_THEME") or "dark").lower()
    cfg["UI_THEME"] = t if t in ("dark", "light") else "dark"
    cfg["RADARR_OK"] = bool(cfg.get("RADARR_OK", False))
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_state():
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def checkbox(name: str) -> bool:
    return request.form.get(name) == "on"


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
# Radarr preview helpers
# --------------------------
def radarr_headers(cfg):
    return {"X-Api-Key": cfg.get("RADARR_API_KEY", "")}


def radarr_get(cfg, path: str):
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
    return dt


def preview_candidates(cfg):
    tag_label = cfg.get("TAG_LABEL", "autodelete30")
    days_old = int(cfg.get("DAYS_OLD", 30))

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
# Dashboard helpers
# --------------------------
def parse_iso(dt_str: str):
    if not dt_str:
        return None
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def time_ago(dt_str: str) -> str:
    dt = parse_iso(dt_str)
    if not dt:
        return ""
    now = datetime.now(timezone.utc)
    delta = now - dt.astimezone(timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 48:
        return f"{hrs}h ago"
    days = hrs // 24
    return f"{days}d ago"


# --------------------------
# Toast (popup) messages (bottom-right)
# --------------------------
def render_toasts() -> str:
    msgs = get_flashed_messages(with_categories=True)
    if not msgs:
        return ""

    items = []
    for cat, msg in msgs:
        t = "ok" if cat == "success" else "err"
        safe = (msg or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        items.append(f'<div class="toast {t}">{safe}</div>')

    return f'<div id="toastHost" class="toastHost">{"".join(items)}</div>'


# --------------------------
# Stylish UI + Light/Dark Theme + Modal + No Jump Scroll Restore + Toasts
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
    --accent:#7c3aed;
    --accent2:#22c55e;
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
    --accent2:#16a34a;
    --warn:#d97706;
    --bad:#dc2626;
    --shadow: 0 12px 30px rgba(0,0,0,.08);
  }

  * { box-sizing: border-box; }
  html { scroll-behavior: auto; }
  body{
    margin:0;
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Apple Color Emoji","Segoe UI Emoji";
    background: radial-gradient(1200px 700px at 20% 0%, rgba(124,58,237,.18), transparent 60%),
                radial-gradient(900px 600px at 100% 10%, rgba(34,197,94,.12), transparent 55%),
                var(--bg);
    color: var(--text);
  }

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
    background: linear-gradient(135deg, rgba(124,58,237,.9), rgba(34,197,94,.6));
    box-shadow: 0 10px 24px rgba(124,58,237,.18);
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
    border-color: rgba(124,58,237,.65);
    box-shadow: 0 0 0 3px rgba(124,58,237,.18);
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

  .kpi{ display:grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
  .k{
    grid-column: span 12;
    border: 1px solid var(--line);
    border-radius: 14px;
    background: rgba(0,0,0,.18);
    padding: 12px 12px;
  }
  [data-theme="light"] .k{ background: rgba(0,0,0,.03); }
  .k .l{ color: var(--muted); font-size: 12px; }
  .k .v{ margin-top: 6px; font-size: 18px; font-weight: 700; }

  @media(min-width: 900px){
    .k { grid-column: span 4; }
    .half { grid-column: span 6; }
  }

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
  .btn:hover{ border-color: rgba(124,58,237,.55); }
  .btn:disabled{
    opacity: .45;
    cursor: not-allowed;
    filter: grayscale(0.35);
  }
  .btn.primary{
    border-color: rgba(124,58,237,.55);
    background: linear-gradient(135deg, rgba(124,58,237,.28), rgba(124,58,237,.10));
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
  .field input:focus, .field select:focus{
    border-color: rgba(124,58,237,.65);
    box-shadow: 0 0 0 3px rgba(124,58,237,.15);
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
    background: rgba(0,0,0,.65);
    backdrop-filter: blur(6px);
    display:none;
    align-items:center;
    justify-content:center;
    z-index: 9999;
    padding: 18px;
  }
  .modal{
    width: min(520px, 100%);
    border: 1px solid var(--line);
    border-radius: 16px;
    background: linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
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
  .modal .mb p{ margin: 0 0 10px 0; color: var(--text); }
  .modal .mb .muted{ color: var(--muted); }
  .modal .mf{
    padding: 14px 16px;
    border-top: 1px solid var(--line);
    display:flex;
    justify-content: flex-end;
    gap: 10px;
    background: rgba(0,0,0,.14);
  }
  [data-theme="light"] .modal .mf{ background: rgba(0,0,0,.02); }
  .xbtn{
    border: 1px solid var(--line2);
    background: rgba(255,255,255,.03);
    color: var(--text);
    width: 34px; height: 34px;
    border-radius: 12px;
    cursor: pointer;
  }

  /* Toasts (bottom-right popups) */
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
  @keyframes toastIn {
    to { opacity: 1; transform: translateY(0); }
  }
  @keyframes toastOut {
    to { opacity: 0; transform: translateY(10px); }
  }
</style>

<script>
  function openRunNowModal() {
    const back = document.getElementById("runNowBack");
    if (back) back.style.display = "flex";
  }
  function closeRunNowModal() {
    const back = document.getElementById("runNowBack");
    if (back) back.style.display = "none";
  }
  function runNowSubmit() {
    const form = document.getElementById("runNowFormConfirm");
    if (form) form.submit();
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeRunNowModal();
  });

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
    const dirty = isDirty(settingsForm);

    saveBtn.disabled = !(radarrOk && dirty);
    saveBtn.title = !radarrOk
      ? "Test Radarr connection first"
      : (dirty ? "Save settings" : "No changes to save");
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

    updateSaveState();
  }

  document.addEventListener("input", onSettingsEdited);
  document.addEventListener("change", onSettingsEdited);

  (function () {
    const KEY = "agregarr_scroll_y";
    let t = null;

    window.addEventListener("scroll", () => {
      if (t) return;
      t = setTimeout(() => {
        sessionStorage.setItem(KEY, String(window.scrollY || 0));
        t = null;
      }, 80);
    }, { passive: true });

    window.addEventListener("beforeunload", () => {
      sessionStorage.setItem(KEY, String(window.scrollY || 0));
    });

    document.addEventListener("DOMContentLoaded", () => {
      updateSaveState();

      const y = parseInt(sessionStorage.getItem(KEY) || "0", 10);
      if (!isNaN(y) && y > 0) {
        requestAnimationFrame(() => {
          requestAnimationFrame(() => window.scrollTo(0, y));
        });
      }

      // Auto-remove toast host after animations complete (~6s)
      const host = document.getElementById("toastHost");
      if (host) {
        setTimeout(() => { try { host.remove(); } catch(e){} }, 6000);
      }
    });
  })();
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
        + pill("Settings", "/settings", "settings")
        + pill("Preview", "/preview", "preview")
        + pill("Status", "/status", "status")
        + theme_btn
    )

    has_logo = find_logo_path() is not None
    logo_html = (
        '<div class="logoWrap"><img class="logoImg" src="/logo" alt="logo"></div>'
        if has_logo
        else '<div class="logoBadge"></div>'
    )

    modal = """
    <div class="modalBack" id="runNowBack" onclick="if(event.target.id==='runNowBack'){ closeRunNowModal(); }">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="runNowTitle">
        <div class="mh">
          <h3 id="runNowTitle">Run Now confirmation</h3>
          <button class="xbtn" type="button" onclick="closeRunNowModal()">✕</button>
        </div>
        <div class="mb">
          <p><b>Dry Run is OFF.</b> This run may delete movie files via Radarr.</p>
          <p class="muted">If you’re not sure, turn on <b>Dry Run</b> and use <b>Preview</b> first.</p>
        </div>
        <div class="mf">
          <button class="btn" type="button" onclick="closeRunNowModal()">Cancel</button>
          <form id="runNowFormConfirm" method="post" action="/run-now" style="margin:0;">
            <button class="btn bad" type="button" onclick="runNowSubmit()">Yes, run now</button>
          </form>
        </div>
      </div>
    </div>
    """

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
          <div class="sub">Radarr tag + age cleanup • WebUI • cron apply • dashboard</div>
        </div>
      </div>
      <div class="nav">{nav}</div>
    </div>

    {body}
  </div>

  {modal}
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

        cfg["RADARR_OK"] = True
        save_config(cfg)
        return redirect("/settings")

    except requests.exceptions.ConnectTimeout:
        flash("Radarr connection failed: timeout connecting to the host.", "error")
    except requests.exceptions.ConnectionError:
        flash("Radarr connection failed: could not connect (URL/host/network).", "error")
    except Exception as e:
        flash(f"Radarr connection failed: {e}", "error")

    return redirect("/settings")


@app.get("/settings")
def settings():
    cfg = load_config()

    run_now_btn = (
        '<form method="post" action="/run-now"><button class="btn good" type="submit">Run Now</button></form>'
        if cfg.get("DRY_RUN", True)
        else '<button class="btn bad" type="button" onclick="openRunNowModal()">Run Now</button>'
    )

    radarr_ok = bool(cfg.get("RADARR_OK"))
    test_label = "Connected" if radarr_ok else "Test Connection"
    test_disabled_attr = "disabled" if radarr_ok else ""
    test_title = "Radarr connection is OK" if radarr_ok else "Test Radarr connection"

    body = f"""
      <div class="grid">

        <div class="card">
          <div class="hd">
            <h2>Settings</h2>
            <div class="btnrow">
              {run_now_btn}
              <form method="post" action="/apply-cron"><button class="btn warn" type="submit">Apply Cron</button></form>
            </div>
          </div>
          <div class="bd">

            <form id="settingsForm"
                  method="post"
                  action="/save"
                  data-radarr-ok="{ '1' if radarr_ok else '0' }"
                  style="margin-top:0px;">

              <!-- Radarr setup group -->
              <div class="card" style="box-shadow:none; margin-bottom:14px;">
                <div class="hd">
                  <h2>Radarr setup</h2>
                </div>
                <div class="bd">
                  <div class="form">
                    <div class="field">
                      <label>Radarr URL</label>
                      <input type="text"
                             name="RADARR_URL"
                             value="{cfg["RADARR_URL"]}"
                             data-initial="{cfg["RADARR_URL"]}">
                    </div>
                    <div class="field">
                      <label>Radarr API Key</label>
                      <input type="password"
                             name="RADARR_API_KEY"
                             value="{cfg["RADARR_API_KEY"]}"
                             data-initial="{cfg["RADARR_API_KEY"]}">
                    </div>
                  </div>

                  <div class="btnrow" style="margin-top:14px;">
                    <button id="testRadarrBtn"
                            class="btn good"
                            type="submit"
                            formaction="/test-radarr"
                            formmethod="post"
                            {test_disabled_attr}
                            title="{test_title}">{test_label}</button>
                  </div>
                </div>
              </div>

              <!-- Cleanup + schedule group -->
              <div class="card" style="box-shadow:none; margin-bottom:14px;">
                <div class="hd">
                  <h2>Cleanup rules</h2>
                  <div class="muted">What gets deleted</div>
                </div>
                <div class="bd">
                  <div class="form">
                    <div class="field">
                      <label>Tag Label</label>
                      <input type="text"
                             name="TAG_LABEL"
                             value="{cfg["TAG_LABEL"]}"
                             data-initial="{cfg["TAG_LABEL"]}">
                    </div>
                    <div class="field">
                      <label>Days Old</label>
                      <input type="number"
                             min="1"
                             name="DAYS_OLD"
                             value="{cfg["DAYS_OLD"]}"
                             data-initial="{cfg["DAYS_OLD"]}">
                    </div>

                    <div class="field">
                      <label>Cron Schedule</label>
                      <input type="text"
                             name="CRON_SCHEDULE"
                             value="{cfg["CRON_SCHEDULE"]}"
                             data-initial="{cfg["CRON_SCHEDULE"]}">
                    </div>
                    <div class="field">
                      <label>HTTP Timeout Seconds</label>
                      <input type="number"
                             min="5"
                             name="HTTP_TIMEOUT_SECONDS"
                             value="{cfg["HTTP_TIMEOUT_SECONDS"]}"
                             data-initial="{cfg["HTTP_TIMEOUT_SECONDS"]}">
                    </div>

                    <div class="field">
                      <label>UI Theme</label>
                      <select name="UI_THEME" data-initial="{cfg.get("UI_THEME","dark")}">
                        <option value="dark" {"selected" if cfg.get("UI_THEME","dark")=="dark" else ""}>Dark</option>
                        <option value="light" {"selected" if cfg.get("UI_THEME","dark")=="light" else ""}>Light</option>
                      </select>
                    </div>
                  </div>

                  <div class="checks" style="margin-top:12px;">
                    <label class="check">
                      <input type="checkbox"
                             name="DRY_RUN"
                             {"checked" if cfg["DRY_RUN"] else ""}
                             data-initial="{ '1' if cfg['DRY_RUN'] else '0' }">
                      <div>
                        <div style="font-weight:700;">Dry Run</div>
                        <div class="muted">Log only; no deletes.</div>
                      </div>
                    </label>

                    <label class="check">
                      <input type="checkbox"
                             name="DELETE_FILES"
                             {"checked" if cfg["DELETE_FILES"] else ""}
                             data-initial="{ '1' if cfg['DELETE_FILES'] else '0' }">
                      <div>
                        <div style="font-weight:700;">Delete Files</div>
                        <div class="muted">Remove movie files from disk.</div>
                      </div>
                    </label>

                    <label class="check">
                      <input type="checkbox"
                             name="ADD_IMPORT_EXCLUSION"
                             {"checked" if cfg["ADD_IMPORT_EXCLUSION"] else ""}
                             data-initial="{ '1' if cfg['ADD_IMPORT_EXCLUSION'] else '0' }">
                      <div>
                        <div style="font-weight:700;">Add Import Exclusion</div>
                        <div class="muted">Prevents Radarr re-import.</div>
                      </div>
                    </label>

                    <label class="check">
                      <input type="checkbox"
                             name="RUN_ON_STARTUP"
                             {"checked" if cfg["RUN_ON_STARTUP"] else ""}
                             data-initial="{ '1' if cfg['RUN_ON_STARTUP'] else '0' }">
                      <div>
                        <div style="font-weight:700;">Run on startup</div>
                        <div class="muted">Run once when container starts.</div>
                      </div>
                    </label>
                  </div>
                </div>
              </div>

              <div class="btnrow" style="margin-top:14px;">
                <button id="saveSettingsBtn"
                        class="btn primary"
                        type="submit"
                        disabled
                        title="No changes to save">Save Settings</button>
                <a class="btn" href="/preview" style="display:inline-flex; align-items:center;">Preview Candidates</a>
              </div>
            </form>
          </div>
        </div>

      </div>
    """
    return render_template_string(shell("mediareaparr • Settings", "settings", body))


@app.post("/save")
def save():
    old = load_config()
    cfg = load_config()

    cfg["RADARR_URL"] = (request.form.get("RADARR_URL") or "").rstrip("/")
    cfg["RADARR_API_KEY"] = request.form.get("RADARR_API_KEY") or ""
    cfg["TAG_LABEL"] = request.form.get("TAG_LABEL") or "autodelete30"
    cfg["DAYS_OLD"] = int(request.form.get("DAYS_OLD") or "30")
    cfg["CRON_SCHEDULE"] = request.form.get("CRON_SCHEDULE") or "15 3 * * *"
    cfg["HTTP_TIMEOUT_SECONDS"] = int(request.form.get("HTTP_TIMEOUT_SECONDS") or "30")
    cfg["UI_THEME"] = (request.form.get("UI_THEME") or cfg.get("UI_THEME", "dark")).lower()
    if cfg["UI_THEME"] not in ("dark", "light"):
        cfg["UI_THEME"] = "dark"

    cfg["DRY_RUN"] = checkbox("DRY_RUN")
    cfg["DELETE_FILES"] = checkbox("DELETE_FILES")
    cfg["ADD_IMPORT_EXCLUSION"] = checkbox("ADD_IMPORT_EXCLUSION")
    cfg["RUN_ON_STARTUP"] = checkbox("RUN_ON_STARTUP")

    if old.get("RADARR_URL") != cfg["RADARR_URL"] or old.get("RADARR_API_KEY") != cfg["RADARR_API_KEY"]:
        cfg["RADARR_OK"] = False

    if not cfg.get("RADARR_OK", False):
        flash("Please click Test Connection and make sure it shows Connected before saving.", "error")
        return redirect("/settings")

    save_config(cfg)
    flash("Settings saved ✔", "success")
    return redirect("/settings")


@app.post("/run-now")
def run_now():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "run_now.flag").write_text("1", encoding="utf-8")
    flash("Run Now triggered ✔ (check Dashboard/logs)", "success")
    return redirect("/dashboard")


@app.post("/apply-cron")
def apply_cron():
    cfg = load_config()
    schedule = (cfg.get("CRON_SCHEDULE") or "15 3 * * *").strip()
    log_path = "/var/log/mediareaparr.log"

    cron_line = f"{schedule} python /app/app.py >> {log_path} 2>&1\n"

    try:
        with open("/etc/crontabs/root", "w", encoding="utf-8") as f:
            f.write(cron_line)

        os.kill(1, signal.SIGHUP)
        flash("Cron schedule applied successfully ✔", "success")
    except Exception as e:
        flash(f"Failed to apply cron: {e}", "error")

    return redirect("/settings")


@app.get("/preview")
def preview():
    cfg = load_config()

    run_now_btn = (
        '<form method="post" action="/run-now"><button class="btn good" type="submit">Run Now</button></form>'
        if cfg.get("DRY_RUN", True)
        else '<button class="btn bad" type="button" onclick="openRunNowModal()">Run Now</button>'
    )

    try:
        result = preview_candidates(cfg)
        error = result.get("error")
        candidates = result.get("candidates", [])
        cutoff = result.get("cutoff", "")

        rows = ""
        for c in candidates[:500]:
            rows += f"""
              <tr>
                <td>{c["age_days"]}</td>
                <td>{c.get("title","")}</td>
                <td>{c.get("year","")}</td>
                <td><code>{c.get("added","")}</code></td>
                <td>{c.get("id","")}</td>
                <td class="muted">{(c.get("path","") or "")}</td>
              </tr>
            """

        if error:
            flash(error, "error")
            return redirect("/settings")

        content = f"""
          <div class="muted">Found <b>{len(candidates)}</b> candidate(s). Preview only (no deletes).</div>
          <div class="muted" style="margin-top:6px;">Cutoff: <code>{cutoff}</code></div>
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
        """

        body = f"""
          <div class="grid">
            <div class="card">
              <div class="hd">
                <h2>Preview candidates</h2>
                <div class="btnrow">
                  <a class="btn" href="/settings">Adjust settings</a>
                  {run_now_btn}
                </div>
              </div>
              <div class="bd">
                {content}
              </div>
            </div>
          </div>
        """
        return render_template_string(shell("mediareaparr • Preview", "preview", body))

    except Exception as e:
        flash(f"Preview failed: {e}", "error")
        return redirect("/dashboard")


@app.get("/dashboard")
def dashboard():
    state = load_state()
    last_run = state.get("last_run")
    cfg = load_config()

    run_now_btn = (
        '<form method="post" action="/run-now"><button class="btn good" type="submit">Run Now</button></form>'
        if cfg.get("DRY_RUN", True)
        else '<button class="btn bad" type="button" onclick="openRunNowModal()">Run Now</button>'
    )

    if not last_run:
        body = f"""
          <div class="grid">
            <div class="card">
              <div class="hd">
                <h2>Dashboard</h2>
                <div class="btnrow">
                  <a class="btn" href="/settings">Settings</a>
                  <a class="btn" href="/preview">Preview</a>
                  {run_now_btn}
                </div>
              </div>
              <div class="bd">
                <div class="muted">No runs recorded yet.</div>
                <div class="muted" style="margin-top:8px;">
                  Start with <b>Dry Run</b> enabled, use <a href="/preview">Preview</a>, then disable Dry Run.
                </div>
              </div>
            </div>
          </div>
        """
        return render_template_string(shell("mediareaparr • Dashboard", "dash", body))

    status = (last_run.get("status") or "").lower()
    if status == "ok":
        status_text = "OK"
    elif status == "ok_with_errors":
        status_text = "OK (with errors)"
    else:
        status_text = "FAILED"

    finished_ago = time_ago(last_run.get("finished_at"))
    deleted_count = (
        len([d for d in (last_run.get("deleted") or []) if d.get("deleted_at")])
        if not last_run.get("dry_run") else len(last_run.get("deleted") or [])
    )

    kpis = f"""
      <div class="kpi">
        <div class="k">
          <div class="l">Status</div>
          <div class="v">{status_text}</div>
        </div>
        <div class="k">
          <div class="l">Candidates</div>
          <div class="v">{last_run.get("candidates_found", 0)}</div>
        </div>
        <div class="k">
          <div class="l">Deleted (or would delete)</div>
          <div class="v">{deleted_count}</div>
        </div>
      </div>
    """

    details = f"""
      <div class="kpi" style="margin-top:12px;">
        <div class="k half">
          <div class="l">Finished</div>
          <div class="v" style="font-size:14px;">
            <code>{last_run.get("finished_at","")}</code>
            <div class="muted" style="margin-top:6px;">{finished_ago}</div>
          </div>
        </div>
        <div class="k half">
          <div class="l">Rule</div>
          <div class="v" style="font-size:14px; font-weight:600;">
            Tag <code>{last_run.get("tag_label","")}</code> • older than <code>{last_run.get("days_old",0)}</code> days
            <div class="muted" style="margin-top:6px;">
              Dry-run: <b>{str(last_run.get("dry_run", False)).lower()}</b> • Delete files: <b>{str(last_run.get("delete_files", False)).lower()}</b>
            </div>
          </div>
        </div>
      </div>
    """

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Dashboard</h2>
            <div class="btnrow">
              <a class="btn" href="/preview">Preview</a>
              <a class="btn" href="/settings">Settings</a>
              {run_now_btn}
            </div>
          </div>
          <div class="bd">
            {kpis}
            {details}
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Dashboard", "dash", body))


@app.get("/status")
def status():
    cfg = load_config()
    state = load_state()

    cfg_rows = "".join([f"<tr><td><code>{k}</code></td><td class='muted'>{str(v)}</td></tr>" for k, v in cfg.items()])
    state_rows = "".join([f"<tr><td><code>{k}</code></td><td class='muted'>{str(v)[:500]}</td></tr>" for k, v in state.items()])

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd"><h2>Status</h2></div>
          <div class="bd">
            <div class="muted">Config file: <code>{str(CONFIG_PATH)}</code> (exists: <b>{str(CONFIG_PATH.exists()).lower()}</b>)</div>
            <div class="muted" style="margin-top:8px;">State file: <code>{str(STATE_PATH)}</code> (exists: <b>{str(STATE_PATH.exists()).lower()}</b>)</div>

            <div style="margin-top:14px;" class="tablewrap">
              <table>
                <thead><tr><th>Config Key</th><th>Value</th></tr></thead>
                <tbody>{cfg_rows}</tbody>
              </table>
            </div>

            <div style="margin-top:14px;" class="tablewrap">
              <table>
                <thead><tr><th>State Key</th><th>Value</th></tr></thead>
                <tbody>{state_rows}</tbody>
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
