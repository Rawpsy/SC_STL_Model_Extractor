"""Mises a jour depuis les Releases GitHub du projet.

Une version publiee = une Release GitHub nommee vX.Y.Z contenant SC_Extractor.exe. L'outil compare
ce numero au sien, telecharge le nouvel .exe, verifie son empreinte (fournie par GitHub), puis le met
a la place de l'ancien au redemarrage (un .exe en cours d'execution ne peut pas etre remplace).
"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import sc_setup
import sc_stl
from version import __version__

UPDATE_REPO = "Rawpsy/SC_STL_Model_Extractor"
ASSET_NAME = "SC_Extractor.exe"


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def latest_release():
    """{'version', 'url', 'sha256', 'notes'} de la derniere Release, ou leve ConvertError."""
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
                                 headers={"User-Agent": sc_setup.USER_AGENT,
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise sc_stl.ConvertError("aucune version publiee accessible (depot prive ou aucune Release)")
        raise sc_stl.ConvertError(f"GitHub a repondu {exc.code}")
    except (urllib.error.URLError, OSError) as exc:
        raise sc_stl.ConvertError(f"pas de connexion a GitHub ({exc})")
    asset = next((a for a in data.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if asset is None:
        raise sc_stl.ConvertError(f"la derniere version ne contient pas {ASSET_NAME}")
    digest = (asset.get("digest") or "").removeprefix("sha256:")
    return {"version": data.get("tag_name", "").lstrip("v"), "url": asset["browser_download_url"],
            "sha256": digest, "notes": (data.get("body") or "").strip()}


def is_newer(remote):
    return _vtuple(remote) > _vtuple(__version__)


def download_update(info, on_progress=None):
    """Telecharge et verifie le nouvel .exe ; retourne son chemin."""
    if not info["sha256"]:
        raise sc_stl.ConvertError("GitHub ne fournit pas d'empreinte pour ce fichier : mise a jour refusee")
    d = sc_stl.DATA_DIR / "update"
    d.mkdir(parents=True, exist_ok=True)
    path = d / ASSET_NAME
    sc_setup.download(info["url"], path, on_progress, info["sha256"])
    return path


def apply_update(new_exe):
    """Remplace l'.exe en cours au redemarrage, puis relance l'outil. Ne revient pas."""
    if not sc_stl.FROZEN:
        raise sc_stl.ConvertError("mise a jour automatique disponible seulement dans SC_Extractor.exe")
    cur = Path(sys.executable)
    bat = sc_stl.DATA_DIR / "update" / "appliquer.bat"
    bat.write_text(
        "@echo off\r\n"
        f"set PID={os.getpid()}\r\n"
        ":attente\r\n"
        'tasklist /FI "PID eq %PID%" | find "%PID%" >nul && (timeout /t 1 /nobreak >nul & goto attente)\r\n'
        f'move /y "{new_exe}" "{cur}" >nul\r\n'
        f'start "" "{cur}"\r\n'
        'del "%~f0"\r\n', encoding="ascii")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=subprocess.CREATE_NO_WINDOW | 0x00000008)
    os._exit(0)
