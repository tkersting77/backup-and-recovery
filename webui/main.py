#!/usr/bin/env python3
"""
Datensicherung – Backend
FastAPI + LDAP-Auth (Samba AD) + Restic CLI Wrapper
"""

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from ldap3 import ALL_ATTRIBUTES, SUBTREE, Connection, Server
from ldap3.core.exceptions import LDAPException

# ---------------------------------------------------------------------------
# Konfiguration laden
# ---------------------------------------------------------------------------

CONFIG_FILE = os.environ.get(
    "WEBUI_CONFIG",
    Path(__file__).parent.parent / "config" / "webui.cfg"
)


def load_cfg(path: str) -> dict:
    cfg = {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {path}")
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


cfg = load_cfg(CONFIG_FILE)

# Repos: REPO_SAMBA=/pfad → {"samba": "/pfad"}
# Passwörter: REPO_SAMBA_PASSWORD_FILE=/pfad → {"samba": "/pfad"}
# Fallback: RESTIC_PASSWORD_FILE_DEFAULT
REPOS: dict[str, str] = {}
REPO_PASSWORDS: dict[str, str] = {}
DEFAULT_PASSWORD_FILE = cfg.get("RESTIC_PASSWORD_FILE_DEFAULT", "")

for key, val in cfg.items():
    if key.startswith("REPO_") and key.endswith("_PASSWORD_FILE"):
        name = key[5:-14].lower()
        REPO_PASSWORDS[name] = val
    elif key.startswith("REPO_") and not key.endswith("_PASSWORD_FILE"):
        name = key[5:].lower()
        REPOS[name] = val


def get_password_file(repo: str) -> str:
    """Gibt die Passwort-Datei fuer ein Repository zurueck."""
    pw = REPO_PASSWORDS.get(repo) or DEFAULT_PASSWORD_FILE
    if not pw:
        raise HTTPException(
            status_code=500,
            detail=f"Keine Passwort-Datei fuer Repository '{repo}' konfiguriert"
        )
    return pw
AD_SERVER            = cfg["AD_SERVER"]
AD_DOMAIN            = cfg["AD_DOMAIN"]
AD_BASE_DN           = cfg["AD_BASE_DN"]
AD_GROUP             = cfg["AD_GROUP"]
SECRET_KEY           = cfg["SECRET_KEY"]
SESSION_MINUTES      = int(cfg.get("SESSION_EXPIRE_MINUTES", "60"))
LOG_FILE             = cfg.get("LOG_FILE", "/var/log/restic-webui.log")

# In-Memory Session Store {token: {user, expires}}
sessions: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),          # weiterhin auch im Journal sichtbar
    ],
)
logger = logging.getLogger("restic_webui")

# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------

app = FastAPI(title="Datensicherung", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# ---------------------------------------------------------------------------
# Auth / LDAP
# ---------------------------------------------------------------------------

def ldap_authenticate(username: str, password: str) -> bool:
    """Prüft Credentials und Gruppenmitgliedschaft gegen Samba AD."""
    try:
        server = Server(AD_SERVER, get_info=None)
        bind_user = f"{username}@{AD_DOMAIN}"
        conn = Connection(server, user=bind_user, password=password, auto_bind=True)

        # Gruppen-DN auflösen (unabhaengig davon, in welcher OU die Gruppe liegt)
        conn.search(
            search_base=AD_BASE_DN,
            search_filter=f"(&(objectClass=group)(cn={AD_GROUP}))",
            search_scope=SUBTREE,
            attributes=["distinguishedName"],
        )
        if not conn.entries:
            logger.error(f"LDAP-Gruppe '{AD_GROUP}' unter {AD_BASE_DN} nicht gefunden")
            return False
        group_dn = conn.entries[0].entry_dn

        # Gruppenmitgliedschaft pruefen (inkl. verschachtelter Gruppen via
        # LDAP_MATCHING_RULE_IN_CHAIN – memberOf allein listet nur direkte Mitglieder)
        conn.search(
            search_base=AD_BASE_DN,
            search_filter=(
                f"(&(sAMAccountName={username})"
                f"(memberOf:1.2.840.113556.1.4.1941:={group_dn}))"
            ),
            search_scope=SUBTREE,
            attributes=ALL_ATTRIBUTES,
        )
        return len(conn.entries) > 0

    except LDAPException as exc:
        logger.error(f"LDAP-Fehler bei Login von {username}: {exc}")
        return False


def get_session(session_token: Optional[str] = Cookie(default=None)) -> str:
    if not session_token or session_token not in sessions:
        raise HTTPException(status_code=401, detail="Nicht angemeldet")
    session = sessions[session_token]
    if datetime.utcnow() > session["expires"]:
        del sessions[session_token]
        raise HTTPException(status_code=401, detail="Sitzung abgelaufen")
    return session["user"]


# ---------------------------------------------------------------------------
# Restic Wrapper
# ---------------------------------------------------------------------------

def run_restic(repo_path: str, password_file: str, args: list) -> str:
    cmd = [
        "restic",
        "--repo", repo_path,
        "--password-file", password_file,
        "--json",
    ] + args

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Restic Fehler: {result.stderr.strip()}"
        )
    return result.stdout


def get_repo_path(repo: str) -> str:
    if repo not in REPOS:
        raise HTTPException(status_code=404, detail=f"Repository '{repo}' nicht gefunden")
    return REPOS[repo]


# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def root(request: Request, session_token: Optional[str] = Cookie(default=None)):
    if session_token and session_token in sessions:
        s = sessions[session_token]
        if datetime.utcnow() < s["expires"]:
            return templates.TemplateResponse("index.html", {
                "request": request,
                "user": s["user"],
                "repos": list(REPOS.keys()),
            })
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.get("/login")
async def login_redirect():
    return Response(status_code=302, headers={"Location": "/"})


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    if not ldap_authenticate(username, password):
        logger.warning(f"Fehlgeschlagener Login-Versuch: {username} von {request.client.host}")
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Ungültige Zugangsdaten oder fehlende Berechtigung"},
            status_code=401,
        )

    token = str(uuid.uuid4())
    sessions[token] = {
        "user": username,
        "expires": datetime.utcnow() + timedelta(minutes=SESSION_MINUTES),
    }
    logger.info(f"Login erfolgreich: {username} von {request.client.host}")

    resp = templates.TemplateResponse("index.html", {
        "request": request,
        "user": username,
        "repos": list(REPOS.keys()),
    })
    resp.set_cookie(
        "session_token", token,
        httponly=True,
        max_age=SESSION_MINUTES * 60,
        samesite="lax",
    )
    return resp


@app.get("/logout")
async def logout_redirect():
    return Response(status_code=302, headers={"Location": "/"})


@app.post("/logout")
async def logout(response: Response, session_token: Optional[str] = Cookie(default=None)):
    if session_token and session_token in sessions:
        user = sessions[session_token]["user"]
        del sessions[session_token]
        logger.info(f"Logout: {user}")
    response = Response(status_code=302, headers={"Location": "/"})
    response.delete_cookie("session_token")
    return response


# ---------------------------------------------------------------------------
# Routes – Snapshots
# ---------------------------------------------------------------------------

@app.get("/api/repos")
async def list_repos(user: str = Depends(get_session)):
    return {"repos": list(REPOS.keys())}


@app.get("/api/{repo}/snapshots")
async def list_snapshots(repo: str, tag: Optional[str] = None, user: str = Depends(get_session)):
    repo_path = get_repo_path(repo)
    password_file = get_password_file(repo)
    args = ["snapshots"]
    if tag:
        args += ["--tag", tag]

    raw = run_restic(repo_path, password_file, args)

    # Restic gibt ein JSON-Array zurück
    try:
        snapshots = json.loads(raw)
    except json.JSONDecodeError:
        snapshots = []

    return {"repo": repo, "snapshots": snapshots}


# ---------------------------------------------------------------------------
# Routes – Dateibaum / Suche
# ---------------------------------------------------------------------------

@app.get("/api/{repo}/ls/{snapshot_id}")
async def list_files(
    repo: str,
    snapshot_id: str,
    path: str = "/",
    user: str = Depends(get_session),
):
    repo_path = get_repo_path(repo)
    password_file = get_password_file(repo)
    raw = run_restic(repo_path, password_file, ["ls", snapshot_id, path])

    norm_path = path.rstrip("/") or "/"

    entries = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Metadaten-Zeile und den abgefragten Ordner selbst ausfiltern –
        # restic ls gibt sonst das Verzeichnis als eigenen Eintrag mit zurueck
        if entry.get("struct_type") == "snapshot":
            continue
        entry_path = entry.get("path", "").rstrip("/") or "/"
        if entry_path == norm_path:
            continue

        entries.append(entry)

    return {"snapshot": snapshot_id, "path": path, "entries": entries}


@app.get("/api/{repo}/find")
async def find_files(
    repo: str,
    pattern: str,
    snapshot_id: Optional[str] = None,
    user: str = Depends(get_session),
):
    repo_path = get_repo_path(repo)
    password_file = get_password_file(repo)

    # restic find matcht als Glob-Muster, nicht als Substring – ohne eigene
    # Wildcards vom Nutzer automatisch zu einer Teilstring-Suche machen.
    search_pattern = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"

    args = ["find", search_pattern]
    if snapshot_id:
        args += ["--snapshot", snapshot_id]

    raw = run_restic(repo_path, password_file, args)

    # restic find --json liefert ein einziges JSON-Array (wie "snapshots"),
    # kein NDJSON wie "ls" – deshalb den gesamten Output als ein Dokument parsen.
    try:
        results = json.loads(raw)
    except json.JSONDecodeError:
        results = []

    return {"pattern": pattern, "results": results}


# ---------------------------------------------------------------------------
# Routes – Download (Datei oder Ordner als ZIP)
# ---------------------------------------------------------------------------

@app.get("/api/{repo}/download/{snapshot_id}")
async def download(
    repo: str,
    snapshot_id: str,
    path: str,
    user: str = Depends(get_session),
):
    repo_path = get_repo_path(repo)
    password_file = get_password_file(repo)

    logger.info(f"Download: user={user} repo={repo} snapshot={snapshot_id} path={path}")

    # Restic restore in temporäres Verzeichnis
    tmp_dir = tempfile.mkdtemp(prefix="restic-restore-")

    try:
        cmd = [
            "restic",
            "--repo", repo_path,
            "--password-file", password_file,
            "restore", snapshot_id,
            "--target", tmp_dir,
            "--include", path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Restore fehlgeschlagen: {result.stderr.strip()}"
            )

        # Wiederhergestellte Dateien in ZIP packen
        restore_root = Path(tmp_dir)
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            # Restic rekonstruiert den vollen Pfad im tmp_dir
            for file_path in restore_root.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(restore_root)
                    zf.write(file_path, arcname)

        zip_buffer.seek(0)

        # Dateiname für Download
        safe_name = Path(path).name or "restore"
        filename = f"{safe_name}_{snapshot_id[:8]}.zip"

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
