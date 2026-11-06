"""Premier lancement : telechargement et verification de StarBreaker et de Blender portable.

Les deux sont epingles sur la version testee avec l'outil, et verifies par leur empreinte SHA-256
officielle (GitHub pour StarBreaker, blender.org pour Blender) avant d'etre decompresses.
"""
import hashlib
import shutil
import threading
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import ttk

import sc_stl
from version import __version__

USER_AGENT = f"SC_Extractor/{__version__}"
TOOLS = [
    {
        "name": "StarBreaker 0.3.2 (lecture des modeles du jeu)",
        "url": "https://github.com/diogotr7/StarBreaker/releases/download/v0.3.2/"
               "starbreaker-cli-v0.3.2-windows-x86_64.zip",
        "sha256": "fafd65ca002b9c3bb88ce32199b2345affcdb358655c68c41d0e93574a5d8a3d",
        "dest": "starbreaker",
        "ready": lambda: sc_stl.SB.is_file(),
    },
    {
        "name": "Blender 5.2.3 portable (reparation et apercus)",
        "url": "https://download.blender.org/release/Blender5.2/blender-5.2.3-windows-x64.zip",
        "sha256": "723f08a4401502561eb763cdeacf8745b31e5f3f154d287d3830d97cf75dc2fb",
        "dest": "blender",
        "ready": lambda: sc_stl.find_blender() is not None,
    },
]


def missing_tools():
    return [t for t in TOOLS if not t["ready"]()]


def download(url, path, on_progress=None, expected_sha256=None):
    """Telecharge url vers path par blocs ; verifie l'empreinte si fournie. Retourne le SHA-256."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if on_progress:
                on_progress(done, total)
    digest = h.hexdigest()
    if expected_sha256 and digest != expected_sha256.lower():
        Path(path).unlink(missing_ok=True)
        raise sc_stl.ConvertError(f"empreinte incorrecte pour {url} : fichier refuse")
    return digest


def safe_extract(zip_path, dest):
    """Decompresse en refusant tout chemin qui sortirait du dossier de destination."""
    dest = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if dest not in target.parents and target != dest:
                raise sc_stl.ConvertError(f"archive suspecte (chemin {m.filename}) : refusee")
        z.extractall(dest)


def install(tool, on_progress=None):
    sc_stl.TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = sc_stl.TOOLS_DIR / tool["dest"]
    tmp = sc_stl.TOOLS_DIR / (tool["dest"] + ".zip.part")
    download(tool["url"], tmp, on_progress, tool["sha256"])
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    safe_extract(tmp, dest)
    tmp.unlink(missing_ok=True)


class SetupDialog:
    """Fenetre du premier lancement ; appelle on_done() quand tout est pret (ou on_fail(message))."""

    def __init__(self, root, on_done, on_fail):
        self.root, self.on_done, self.on_fail = root, on_done, on_fail
        self.todo = missing_tools()
        self.win = tk.Toplevel(root)
        self.win.title("SC Extractor : premier lancement")
        self.win.geometry("560x220")
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)  # on attend la fin (ou l'erreur)
        f = ttk.Frame(self.win, padding=12)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Telechargement des outils necessaires (une seule fois) :",
                  font=("", 10, "bold")).pack(anchor="w")
        for t in self.todo:
            ttk.Label(f, text="  - " + t["name"]).pack(anchor="w")
        self.bar = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.bar.pack(fill="x", pady=(10, 4))
        self.status = ttk.Label(f, text="")
        self.status.pack(anchor="w")
        self.state = {"pct": 0, "msg": "", "end": None}
        threading.Thread(target=self.run, daemon=True).start()
        self.win.after(150, self.poll)

    def run(self):
        try:
            for i, t in enumerate(self.todo):
                def cb(done, total, i=i, t=t):
                    part = done / total if total else 0
                    self.state["pct"] = 100 * (i + part) / len(self.todo)
                    self.state["msg"] = f"{t['name']} : {done / 1e6:.0f} / {total / 1e6:.0f} Mo"
                install(t, cb)
                self.state["msg"] = f"{t['name']} : installe"
            self.state["end"] = ("ok", "")
        except Exception as exc:  # noqa: BLE001
            self.state["end"] = ("fail", str(exc))

    def poll(self):
        self.bar.config(value=self.state["pct"])
        self.status.config(text=self.state["msg"])
        end = self.state["end"]
        if end:
            self.win.destroy()
            (self.on_done() if end[0] == "ok" else self.on_fail(end[1]))
            return
        self.win.after(150, self.poll)
