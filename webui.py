import os
import json
from pathlib import Path
from flask import Flask, request, redirect, render_template_string

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"

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

app = Flask(__name__)

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
    .btns { display:flex; gap:10px; margin-top: 18px; }
    button { padding: 10px 14px; cursor: pointer; }
    .note { color: #444; margin-top: 12px; }
    code { background: #f3f3f3; padding: 2px 6px; }
  </style>
</head>
<body>
  <h2>agregarr-cleanarr settings</h2>
  <form method="post" action="/save">
    <div class="row"><label>Radarr URL</label><input type="text" name="RADARR_URL" value="{{cfg.RADARR_URL}}"></div>
    <div class="row"><label>Radarr API Key</label><input type="password" name="RADARR_API_KEY" value="{{cfg.RADARR_API_KEY}}"></div>

    <div class="row"><label>Tag Label</label><input type="text" name="TAG_LABEL" value="{{cfg.TAG_LABEL}}"></div>
    <div class="row"><label>Days Old</label><input type="number" name="DAYS_OLD" value="{{cfg.DAYS_OLD}}" min="1"></div>

    <div class="chk"><input type="checkbox" name="DRY_RUN" {% if cfg.DRY_RUN %}checked{% endif %}> <span>Dry Run (don’t delete, just log)</span></div>
    <div class="chk"><input type="checkbox" name="DELETE_FILES" {% if cfg.DELETE_FILES %}checked{% endif %}> <span>Delete Files</span></div>
    <div class="chk"><input type="checkbox" name="ADD_IMPORT_EXCLUSION" {% if cfg.ADD_IMPORT_EXCLUSION %}checked{% endif %}> <span>Add Import Exclusion</span></div>

    <div class="row"><label>Cron Schedule</label><input type="text" name="CRON_SCHEDULE" value="{{cfg.CRON_SCHEDULE}}"></div>
    <div class="chk"><input type="checkbox" name="RUN_ON_STARTUP" {% if cfg.RUN_ON_STARTUP %}checked{% endif %}> <span>Run once when container starts</span></div>

    <div class="row"><label>HTTP Timeout Seconds</label><input type="number" name="HTTP_TIMEOUT_SECONDS" value="{{cfg.HTTP_TIMEOUT_SECONDS}}" min="5"></div>

    <div class="btns">
      <button type="submit">Save</button>
      <button type="submit" formaction="/run-now" formmethod="post">Run Now</button>
    </div>

    <p class="note">
      Settings are saved to <code>/config/config.json</code>. Cron uses <code>CRON_SCHEDULE</code>.
    </p>
  </form>

  <p><a href="/status">Status</a></p>
</body>
</html>
"""

@app.get("/")
def index():
    cfg = load_config()
    return render_template_string(PAGE, cfg=type("C", (), cfg))

def checkbox(name: str) -> bool:
    return request.form.get(name) == "on"

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
    return redirect("/")

@app.get("/status")
def status():
    cfg = load_config()
    return {
        "config_path": str(CONFIG_PATH),
        "config_exists": CONFIG_PATH.exists(),
        "config": cfg
    }

@app.post("/run-now")
def run_now():
    # touch a flag file that entrypoint watches for to trigger an immediate run
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "run_now.flag").write_text("1", encoding="utf-8")
    return redirect("/")
