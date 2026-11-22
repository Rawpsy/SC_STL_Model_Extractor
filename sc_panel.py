"""Envoi des exports "Reperes web" vers le panel (onglet Salvage).

API : POST <panel_url>/outils/salvage/ships/<ENTITE>/<FICHIER>
      FICHIER = hull.glb | markers.json | report.txt, corps brut (application/octet-stream), 40 Mo max,
      en-tete Authorization: Bearer <panel_token>.
Reglages dans config.json uniquement (jamais dans le code ni dans git) :
      "panel_url" (defaut https://bot.raw-psy.com), "panel_token", "panel_upload" (envoi auto, defaut oui).
"""
import re
import time
from pathlib import Path

import sc_stl

DEFAULT_URL = "https://bot.raw-psy.com"
FILES = ("markers.json", "hull.glb", "report.txt")  # ordre d'envoi demande
MAX_BYTES = 40 * 1024 * 1024
ENTITY_RE = re.compile(r"^[A-Za-z0-9_]+$")
MESSAGES = {401: "cle refusee", 400: "fichier refuse (GLB/JSON invalide)", 413: "fichier trop gros"}


def settings():
    cfg = sc_stl.load_config()
    return (cfg.get("panel_url") or DEFAULT_URL).rstrip("/"), (cfg.get("panel_token") or "").strip()


def enabled():
    """Envoi automatique : seulement si une cle est reglee et que l'envoi n'a pas ete desactive."""
    cfg = sc_stl.load_config()
    return bool((cfg.get("panel_token") or "").strip()) and cfg.get("panel_upload", True) not in (False, 0, "0")


def upload_dir(entity_dir, log=print):
    """Envoie markers.json, hull.glb puis report.txt d'un dossier d'export. Retourne (ok, detail)."""
    import requests
    entity_dir = Path(entity_dir)
    entity = entity_dir.name
    url, token = settings()
    if not token:
        return False, "aucune cle de panel reglee (panel_token)"
    if not ENTITY_RE.match(entity):
        return False, f"nom d'entite refuse par le panel : {entity!r}"
    sent = []
    for name in FILES:
        path = entity_dir / name
        if not path.is_file():
            if name == "report.txt":
                continue  # facultatif
            return False, f"{name} manquant"
        data = path.read_bytes()
        if len(data) > MAX_BYTES:
            return False, f"{name} fait {len(data) / 1e6:.1f} Mo (40 Mo max)"
        last = ""
        for attempt in (1, 2):  # 2 essais ; pas de nouvel essai si le panel refuse explicitement
            try:
                r = requests.post(f"{url}/outils/salvage/ships/{entity}/{name}", data=data, timeout=60,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Content-Type": "application/octet-stream"})
            except requests.RequestException as exc:
                last = f"{name} : connexion impossible ({exc.__class__.__name__})"
                time.sleep(2)
                continue
            if r.status_code == 200:
                last = ""
                break
            body = r.text.strip().replace("\n", " ")[:300]
            last = f"{name} : HTTP {r.status_code} {MESSAGES.get(r.status_code, '')} - reponse : {body}".strip()
            if r.status_code in MESSAGES:
                break
            time.sleep(2)
        if last:
            return False, last
        sent.append(f"{name} HTTP 200" + (f" ({len(data) / 1e6:.2f} Mo)" if len(data) > 1e5 else ""))
    return True, ", ".join(sent)


def upload_all(outdir, log=print):
    """Envoie tous les sous-dossiers deja exportes (ceux qui ont hull.glb ET markers.json)."""
    outdir = Path(outdir)
    ok, failed, skipped = [], [], []
    dirs = sorted(d for d in outdir.iterdir() if d.is_dir()) if outdir.is_dir() else []
    for d in dirs:
        if not ((d / "hull.glb").is_file() and (d / "markers.json").is_file()):
            skipped.append(d.name)
            log(f"{d.name} : ignore (pas de hull.glb + markers.json)")
            continue
        good, detail = upload_dir(d, log)
        (ok if good else failed).append(d.name)
        log(f"{d.name} : {'envoye' if good else 'ECHEC'} - {detail}")
        if not good:
            note_error(outdir, d.name, f"envoi au panel : {detail}")
    return ok, failed, skipped


def note_error(outdir, entity, msg):
    with open(Path(outdir) / "errors.log", "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{entity}\t{msg}\n")
