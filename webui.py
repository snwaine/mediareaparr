import os
import json
import signal
from pathlib import Path
from datetime import datetime, timezone

import requests
from flask import (
    Flask, request, redirect, render_template_string,
    flash, get_flashed_messages
)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

app = Flask(__name__)
app.secret_key = "agregarr-cleanarr-secret"

def env_default(name: str, default: str = "") -> str:
    return os.environ.get(name, default)

def load_config():
    # Defaults from env
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
    }

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: data[k] for k in data.keys() if k in cfg})
        except Exception:
            pass

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
# Templates
# --------------------------
PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>agregarr-cleanarr</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 900px; }
    .row { display: flex; gap: 12px; margin-bottom: 12px; }
    label { width: 220px; font-weight: bold; }
    input[type=text], input[type=password], input[type=number] { flex: 1; padding: 8px; }
    .chk { display:flex; align-items:center; gap:10px; margin-bottom: 10px; }
    .btns { display:flex; gap:10px; margin-top: 18px; flex-wrap: wrap; }
    button { padding: 10px 14px; cursor: pointer; }
    .note { color: #444; margin-top: 12px; }
    code { background: #f3f3f3; padding: 2px 6px; }

    .alert { padding: 12px 14px; margin-bottom: 14px; border-radius: 4px; font-weight: bold; }
    .alert-success { background: #e6f4ea; color: #1e7e34; border: 1px solid #c3e6cb; }
    .alert-error { background: #f8d7da; color: #842029; border: 1px solid #f5c2c7; }
  </style>
</head>
<body>
  <h2>agregarr-cleanarr settings</h2>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, message in messages %}
      <div class="alert alert-{{category}}">
        {{message}}
      </div>
    {% endfor %}
  {% endwith %}

  <form method="post" action="/save">
    <div class="row"><label>Radarr URL</label><input type="text" name="RADARR_URL" value="{{cfg.RADARR_URL}}"></div>
    <div class="row"><label>Radarr API Key</label><input type="password" name="RADARR_API_KEY" value="{{cfg.RADARR_API_KEY}}"></div>

    <div class="row"><label>Tag Label</label><input type="text" name="TAG_LABEL" value="{{cfg.TAG_LABEL}}"></div>
    <div class="row"><label>Days Old</label><input type="number" name="DAYS_OLD" value="{{cfg.DAYS_OLD}}" min="1"></div>

    <div class="chk"><input type="checkbox" name="DRY_RUN" {% if cfg.DRY_RUN %}checked{% endif %}> <span>Dry Run (don’t delete, just log)</span></div>
    <div class="chk"><input type="checkbox" name="DELETE_FILES" {% if cfg.DELETE_FILES %}checked{% endif %}> <span>Delete Files</span></div>
    <div class="chk"><input type="checkbox" name="ADD_IMPORT_EXCLUSION" {% if cfg.ADD_IMPORT_EXCLUSION %}checked{% endif %}> <span>Add Import Exclusion</span></div>

    <div class="row"><label>Cron Schedule</label><input type="text" name="CRON_SCHEDULE" value="{{cfg.CRON_SCHEDULE}}"></div>
    <p class="note">After changing the schedule, click <b>Apply Cron</b> to activate it immediately.</p>

    <div class="chk"><input type="checkbox" name="RUN_ON_STARTUP" {% if cfg.RUN_ON_STARTUP %}checked{% endif %}> <span>Run once when container starts</span></div>

    <div class="row"><label>HTTP Timeout Seconds</label><input type="number" name="HTTP_TIMEOUT_SECONDS" value="{{cfg.HTTP_TIMEOUT_SECONDS}}" min="5"></div>

    <div class="btns">
      <button type="submit">Save</button>
      <button type="submit" formaction="/run-now" formmethod="post">Run Now</button>
      <button type="submit" formaction="/apply-cron" formmethod="post">Apply Cron</button>
    </div>

    <p class="note">
      Settings are saved to <code>/config/config.json</code>. Dashboard state in <code>/config/state.json</code>.
    </p>
  </form>

  <p>
    <a href="/dashboard">Dashboard</a> |
    <a href="/preview">Preview delete candidates</a> |
    <a href="/status">Status</a>
  </p>
</body>
</html>
"""

PREVIEW_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>agregarr-cleanarr - Preview</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 1100px; }
    table { border-collapse: collapse; width: 100%; margin-top: 12px; }
    th, td { border: 1px solid #ddd; padding: 8px; font-size: 14px; }
    th { background: #f3f3f3; text-align: left; }
    .err { color: #b00020; margin-top: 10px; }
    .top { display:flex; justify-content: space-between; align-items:center; }
    a { text-decoration: none; }
    .muted { color:#555; }
    code { background: #f3f3f3; padding: 2px 6px; }
  </style>
</head>
<body>
  <div class="top">
    <h2>Preview delete candidates</h2>
    <div><a href="/">← Back to Settings</a></div>
  </div>

  <div class="muted">
    Tag: <b>{{tag}}</b> | Days old: <b>{{days}}</b> | Cutoff: <code>{{cutoff}}</code>
  </div>

  {% if error %}
    <div class="err">{{error}}</div>
  {% else %}
    <p>Found <b>{{count}}</b> candidate(s). (Preview only — nothing is deleted here.)</p>

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
      <tbody>
        {% for c in candidates %}
          <tr>
            <td>{{c.age_days}}</td>
            <td>{{c.title}}</td>
            <td>{{c.year}}</td>
            <td>{{c.added}}</td>
            <td>{{c.id}}</td>
            <td>{{c.path}}</td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% endif %}
</body>
</html>
"""

DASHBOARD_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>agregarr-cleanarr - Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 24px; max-width: 1100px; }
    .top { display:flex; justify-content: space-between; align-items:center; }
    .cards { display:flex; gap:12px; margin-top: 14px; flex-wrap: wrap; }
    .card { border:1px solid #ddd; border-radius:6px; padding:12px 14px; min-width: 240px; }
    h3 { margin: 0 0 8px 0; }
    table { border-collapse: collapse; width: 100%; margin-top: 14px; }
    th, td { border: 1px solid #ddd; padding: 8px; font-size: 14px; }
    th { background: #f3f3f3; text-align: left; }
    .muted { color:#555; }
    .ok { font-weight:bold; color:#1e7e34; }
    .warn { font-weight:bold; color:#8a6d3b; }
    .bad { font-weight:bold; color:#842029; }
    code { background: #f3f3f3; padding: 2px 6px; }
    .btn { padding: 8px 12px; border: 1px solid #ccc; border-radius: 5px; background: #fff; cursor: pointer; }
    .alert { padding: 12px 14px; margin: 12px 0; border-radius: 4px; font-weight: bold; }
    .alert-success { background: #e6f4ea; color: #1e7e34; border: 1px solid #c3e6cb; }
    .alert-error { background: #f8d7da; color: #842029; border: 1px solid #f5c2c7; }
  </style>
</head>
<body>
  <div class="top">
    <h2>agregarr-cleanarr dashboard</h2>
    <div>
      <a href="/">Settings</a> |
      <a href="/preview">Preview</a> |
      <a href="/status">Status</a>
    </div>
  </div>

  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for category, message in messages %}
      <div class="alert alert-{{category}}">
        {{message}}
      </div>
    {% endfor %}
  {% endwith %}

  <form method="post" action="/clear-state" onsubmit="return confirm('Clear dashboard history/state?');">
    <button class="btn" type="submit">Clear state</button>
  </form>

  {% if not last_run %}
    <p class="muted">No runs recorded yet. Click <b>Run Now</b> from Settings.</p>
  {% else %}
    <div class="cards">
      <div class="card">
        <h3>Last run</h3>
        <div>Status: <span class="{{status_class}}">{{last_run.status}}</span></div>
        <div>Finished: <code>{{last_run.finished_at}}</code></div>
        <div class="muted">({{finished_ago}})</div>
        <div>Duration: <b>{{last_run.duration_seconds}}</b> sec</div>
      </div>

      <div class="card">
        <h3>Rule</h3>
        <div>Tag: <b>{{last_run.tag_label}}</b></div>
        <div>Older than: <b>{{last_run.days_old}}</b> days</div>
        <div>Dry run: <b>{{last_run.dry_run}}</b></div>
        <div>Delete files: <b>{{last_run.delete_files}}</b></div>
      </div>

      <div class="card">
        <h3>Results</h3>
        <div>Candidates: <b>{{last_run.candidates_found}}</b></div>
        <div>Deleted: <b>{{deleted_count}}</b></div>
        <div>Errors: <b>{{error_count}}</b></div>
      </div>
    </div>

    {% if error_count > 0 %}
      <div class="card" style="margin-top:12px;">
        <h3>Last errors</h3>
        <ul>
          {% for e in last_run.errors[-5:] %}
            <li>{{e}}</li>
          {% endfor %}
        </ul>
      </div>
    {% endif %}

    <h3 style="margin-top:18px;">Last deleted (most recent run)</h3>
    {% if last_run.deleted and last_run.deleted|length > 0 %}
      <table>
        <thead>
          <tr>
            <th>Deleted at</th>
            <th>Age (days)</th>
            <th>Title</th>
            <th>Year</th>
            <th>ID</th>
            <th>Path</th>
            <th>Dry-run?</th>
          </tr>
        </thead>
        <tbody>
          {% for d in last_run.deleted[:50] %}
            <tr>
              <td>{{d.deleted_at or ""}}</td>
              <td>{{d.age_days}}</td>
              <td>{{d.title}}</td>
              <td>{{d.year}}</td>
              <td>{{d.id}}</td>
              <td>{{d.path}}</td>
              <td>{{d.dry_run}}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      <p class="muted">Showing up to 50 entries from the last run.</p>
    {% else %}
      <p class="muted">No deletions recorded for the last run.</p>
    {% endif %}

    <h3 style="margin-top:18px;">Run history (latest {{history_limit}})</h3>
    {% if history and history|length > 0 %}
      <table>
        <thead>
          <tr>
            <th>Finished</th>
            <th>When</th>
            <th>Status</th>
            <th>Tag</th>
            <th>Days</th>
            <th>Candidates</th>
            <th>Deleted</th>
            <th>Dry run</th>
            <th>Duration (s)</th>
          </tr>
        </thead>
        <tbody>
          {% for r in history %}
            <tr>
              <td>{{r.finished_at or ""}}</td>
              <td>{{ago_map.get(r.finished_at, "")}}</td>
              <td>{{r.status}}</td>
              <td>{{r.tag_label}}</td>
              <td>{{r.days_old}}</td>
              <td>{{r.candidates_found}}</td>
              <td>{{ (r.deleted_count if not r.dry_run else (r.deleted|length)) }}</td>
              <td>{{r.dry_run}}</td>
              <td>{{r.duration_seconds}}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    {% else %}
      <p class="muted">No history yet.</p>
    {% endif %}
  {% endif %}
</body>
</html>
"""

# --------------------------
# Routes
# --------------------------
@app.get("/")
def index():
    cfg = load_config()
    return render_template_string(PAGE, cfg=type("C", (), cfg))

@app.post("/save")
def save():
    cfg = load_config()

    cfg["RADARR_URL"] = (request.form.get("RADARR_URL") or "").rstrip("/")
    cfg["RADARR_API_KEY"] = request.form.get("RADARR_API_KEY") or ""
    cfg["TAG_LABEL"] = request.form.get("TAG_LABEL") or "autodelete30"
    cfg["DAYS_OLD"] = int(request.form.get("DAYS_OLD") or "30")
    cfg["CRON_SCHEDULE"] = request.form.get("CRON_SCHEDULE") or "15 3 * * *"
    cfg["HTTP_TIMEOUT_SECONDS"] = int(request.form.get("HTTP_TIMEOUT_SECONDS") or "30")

    cfg["DRY_RUN"] = checkbox("DRY_RUN")
    cfg["DELETE_FILES"] = checkbox("DELETE_FILES")
    cfg["ADD_IMPORT_EXCLUSION"] = checkbox("ADD_IMPORT_EXCLUSION")
    cfg["RUN_ON_STARTUP"] = checkbox("RUN_ON_STARTUP")

    save_config(cfg)
    flash("Settings saved ✔", "success")
    return redirect("/")

@app.post("/run-now")
def run_now():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "run_now.flag").write_text("1", encoding="utf-8")
    flash("Run Now triggered ✔ (check dashboard/logs)", "success")
    return redirect("/")

@app.post("/apply-cron")
def apply_cron():
    cfg = load_config()
    schedule = (cfg.get("CRON_SCHEDULE") or "15 3 * * *").strip()
    log_path = "/var/log/agregarr-cleanarr.log"

    cron_line = f"{schedule} python /app/app.py >> {log_path} 2>&1\n"

    try:
        with open("/etc/crontabs/root", "w", encoding="utf-8") as f:
            f.write(cron_line)

        # BusyBox crond runs as PID 1 (entrypoint execs it)
        os.kill(1, signal.SIGHUP)

        flash("Cron schedule applied successfully ✔", "success")
    except Exception as e:
        flash(f"Failed to apply cron: {e}", "error")

    return redirect("/")

@app.get("/preview")
def preview():
    cfg = load_config()
    try:
        result = preview_candidates(cfg)
        return render_template_string(
            PREVIEW_PAGE,
            tag=cfg.get("TAG_LABEL", ""),
            days=int(cfg.get("DAYS_OLD", 30)),
            cutoff=result.get("cutoff", ""),
            error=result.get("error"),
            candidates=result.get("candidates", []),
            count=len(result.get("candidates", [])),
        )
    except Exception as e:
        return render_template_string(
            PREVIEW_PAGE,
            tag=cfg.get("TAG_LABEL", ""),
            days=int(cfg.get("DAYS_OLD", 30)),
            cutoff="",
            error=str(e),
            candidates=[],
            count=0,
        ), 500

@app.get("/dashboard")
def dashboard():
    state = load_state()
    last_run = state.get("last_run")
    history = state.get("run_history") or []

    if not last_run:
        return render_template_string(DASHBOARD_PAGE, last_run=None)

    status = (last_run.get("status") or "").lower()
    if status == "ok":
        status_class = "ok"
    elif status == "ok_with_errors":
        status_class = "warn"
    else:
        status_class = "bad"

    deleted_count = (
        len([d for d in (last_run.get("deleted") or []) if d.get("deleted_at")])
        if not last_run.get("dry_run") else len(last_run.get("deleted") or [])
    )
    error_count = len(last_run.get("errors") or [])
    finished_ago = time_ago(last_run.get("finished_at"))

    ago_map = {}
    for r in history:
        fa = r.get("finished_at")
        if fa and fa not in ago_map:
            ago_map[fa] = time_ago(fa)

    history_limit = int(os.environ.get("STATE_HISTORY_LIMIT", "20"))

    return render_template_string(
        DASHBOARD_PAGE,
        last_run=type("C", (), last_run),
        status_class=status_class,
        deleted_count=deleted_count,
        error_count=error_count,
        finished_ago=finished_ago,
        history=history,
        ago_map=ago_map,
        history_limit=history_limit,
    )

@app.post("/clear-state")
def clear_state():
    try:
        if STATE_PATH.exists():
            STATE_PATH.unlink()
        flash("State cleared ✔", "success")
    except Exception as e:
        flash(f"Failed to clear state: {e}", "error")
    return redirect("/dashboard")

@app.get("/status")
def status():
    cfg = load_config()
    state = load_state()
    return {
        "config_path": str(CONFIG_PATH),
        "config_exists": CONFIG_PATH.exists(),
        "config": cfg,
        "state_path": str(STATE_PATH),
        "state_exists": STATE_PATH.exists(),
        "state": state,
    }

if __name__ == "__main__":
    # Flask CLI args supported but not required; entrypoint passes host/port
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("WEBUI_PORT", "7575")))
    args = p.parse_args()
    app.run(host=args.host, port=args.port)
