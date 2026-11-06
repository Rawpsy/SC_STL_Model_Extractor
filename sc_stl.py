"""Conversion d'assets Star Citizen vers STL.

Chaine : Data.p4k -> (starbreaker) GLB -> (trimesh) STL.
 - asset .cga rattache a une entite du jeu (vaisseau, arme...) : `entity export`
 - asset .cgf / .skin                                          : `skin export`
"""
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent  # code de l'outil (dans le .exe : dossier d'extraction temporaire)
FROZEN = getattr(sys, "frozen", False)  # True quand l'outil tourne depuis SC_Extractor.exe
if FROZEN:
    # le .exe s'extrait ailleurs a chaque lancement : reglages, caches et outils vont dans un dossier stable
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SC_Extractor"
    OUTPUT_DIR = Path.home() / "Documents" / "SC_Extractor"
else:
    DATA_DIR = HERE
    OUTPUT_DIR = HERE
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOOLS_DIR = DATA_DIR / "tools"
SB = TOOLS_DIR / "starbreaker" / "starbreaker.exe"
ENTITY_QUERY = "EntityClassDefinition.Components[SGeometryResourceParams].Geometry.Geometry.Geometry.path"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ConvertError(Exception):
    pass


# ---------- garde-fou memoire et arret des processus ----------
import ctypes
import threading
from ctypes import wintypes

# MEM_GUARD = False : aucune limite de RAM, les calculs peuvent utiliser toute la memoire (choix de
# l'utilisateur). Mettre True pour reactiver le garde-fou avec les seuils ci-dessous.
MEM_GUARD = False
MEM_MIN_FREE_GB = 1.0      # en dessous de cette RAM libre, le processus de l'outil est arrete
MEM_MAX_SHARE = 0.92       # un processus de l'outil ne doit pas depasser 92 % de la RAM totale
MEM_JOB_SHARE = 0.94       # plafond Windows pour l'ensemble des processus de l'outil


class _MEMSTATUS(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


try:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMSTATUS)]
except (AttributeError, OSError):  # hors Windows : pas de garde-fou
    _k32 = None


def system_memory():
    """(RAM totale, RAM libre) en octets, ou (0, 0) si inconnu."""
    if _k32 is None:
        return 0, 0
    m = _MEMSTATUS()
    m.dwLength = ctypes.sizeof(m)
    if not _k32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return 0, 0
    return m.ullTotalPhys, m.ullAvailPhys


def process_memory(pid):
    """Memoire utilisee par un processus (max de la memoire physique et de la memoire privee)."""
    if _k32 is None:
        return 0
    h = _k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return 0
    try:
        c = _PMC()
        c.cb = ctypes.sizeof(c)
        if not _k32.K32GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return 0
        return max(c.WorkingSetSize, c.PagefileUsage)
    finally:
        _k32.CloseHandle(h)


_tls = threading.local()
_active = {}  # groupe -> ensemble de Popen en cours
_active_lock = threading.Lock()


def set_group(name):
    """Rattache les processus lances depuis ce thread a un groupe (ex. 'convert', 'preview')."""
    _tls.group = name


def stop_group(name):
    """Arrete immediatement tous les processus en cours du groupe."""
    with _active_lock:
        procs = list(_active.get(name, ()))
    for p in procs:
        p._sc_reason = "arrete a la demande"
        try:
            p.kill()
        except OSError:
            pass
    return len(procs)


def _watch(proc, stop_evt):
    """Thread de surveillance : arrete le processus s'il met la machine en danger."""
    if not MEM_GUARD:
        return
    total, _ = system_memory()
    while not stop_evt.wait(0.5):
        if proc.poll() is not None:
            return
        used = process_memory(proc.pid)
        _tot, avail = system_memory()
        if not total:
            continue
        if used > MEM_MAX_SHARE * total or (avail < MEM_MIN_FREE_GB * 1e9 and used > 1e9):
            proc._sc_reason = (f"memoire insuffisante : le processus utilisait {used / 1e9:.1f} Go, "
                               f"il restait {avail / 1e9:.1f} Go libres sur {total / 1e9:.0f} Go")
            try:
                proc.kill()
            except OSError:
                pass
            return


class _BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                   "OtherOperationCount", "ReadTransferCount",
                                                   "WriteTransferCount", "OtherTransferCount")]


class _EXT_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


JOB_KILL_ON_CLOSE = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
JOB_MEMORY = 0x200           # JOB_OBJECT_LIMIT_JOB_MEMORY
_job = None


def _get_job():
    """Job Windows unique pour l'outil : ses processus meurent avec lui, et leur memoire totale est plafonnee."""
    global _job
    if _job is not None or _k32 is None:
        return _job
    try:
        _k32.CreateJobObjectW.restype = wintypes.HANDLE
        _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = _k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXT_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_KILL_ON_CLOSE
        total, _ = system_memory()
        if total and MEM_GUARD:  # filet de securite systeme pour l'ensemble des processus de l'outil
            info.BasicLimitInformation.LimitFlags |= JOB_MEMORY
            info.JobMemoryLimit = int(total * MEM_JOB_SHARE)
        if not _k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            _k32.CloseHandle(job)
            return None
        _job = job
    except (AttributeError, OSError):
        _job = None
    return _job


def stop_all():
    """Arrete tous les processus lances par l'outil (a la fermeture)."""
    with _active_lock:
        groups = list(_active)
    return sum(stop_group(g) for g in groups)


def _child_env():
    """Environnement propre pour Blender et StarBreaker quand l'outil tourne depuis le .exe.

    Le .exe (PyInstaller) dirige la recherche de bibliotheques Windows vers son dossier d'extraction ;
    un programme lance en heritant de ce reglage charge de mauvaises DLL (Blender : erreur 14001).
    """
    if not FROZEN:
        return None
    try:
        ctypes.windll.kernel32.SetDllDirectoryW(None)  # retablit la recherche de DLL standard
    except (AttributeError, OSError):
        pass
    meipass = os.path.normcase(getattr(sys, "_MEIPASS", ""))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("_MEI", "_PYI", "TCL_", "TK_"))
           and k not in ("PYTHONHOME", "PYTHONPATH")}
    if meipass:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if p and not os.path.normcase(p).startswith(meipass))
    return env


def _popen_guarded(cmd, **kw):
    proc = subprocess.Popen(cmd, creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace",
                            env=_child_env(), **kw)
    proc._sc_reason = None
    job = _get_job()
    if job:
        try:
            _k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
        except (AttributeError, OSError):
            pass
    group = getattr(_tls, "group", None) or "default"
    with _active_lock:
        _active.setdefault(group, set()).add(proc)
    evt = threading.Event()
    threading.Thread(target=_watch, args=(proc, evt), daemon=True).start()

    def done():
        evt.set()
        with _active_lock:
            _active.get(group, set()).discard(proc)
        if proc._sc_reason:
            raise ConvertError("Arrete : " + proc._sc_reason)
    return proc, done


def _run(args, timeout=1800):
    """StarBreaker sous surveillance memoire ; meme interface que subprocess.run."""
    proc, done = _popen_guarded([str(SB), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    finally:
        pass
    done()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def norm(p):
    return p.replace("\\", "/").lower().lstrip("/")


def load_entity_map(p4k_path, cache_file):
    """geometrie (chemin en minuscules, sans 'data/') -> liste de noms d'entites."""
    if cache_file.is_file() and cache_file.stat().st_mtime > Path(p4k_path).stat().st_mtime:
        import json
        return json.loads(cache_file.read_text(encoding="utf-8"))
    r = _run(["dcb", "query", ENTITY_QUERY, "--filter", "*", "--p4k", str(p4k_path)], timeout=600)
    if r.returncode != 0:
        raise ConvertError(f"dcb query a echoue : {r.stderr.strip()[-400:]}")
    out = {}
    for line in r.stdout.splitlines():
        if "\t" not in line or not line.startswith("EntityClassDefinition."):
            continue
        name, geom = line.split("\t", 1)
        geom = norm(geom.strip())
        if not geom:
            continue
        if geom.startswith("data/"):
            geom = geom[5:]
        out.setdefault(geom, []).append(name.split(".", 1)[1])
    for v in out.values():
        v.sort(key=lambda s: (len(s), s))
    import json
    cache_file.write_text(json.dumps(out), encoding="utf-8")
    return out


def entity_for_asset(entity_map, asset_path, exts):
    """Retourne le nom d'entite (le plus court) dont la geometrie est cet asset, ou None."""
    base = norm(asset_path)
    if base.startswith("data/"):
        base = base[5:]
    for ext in ("cdf", "cga", "cgf", "skin", "chr"):
        if ext in exts:
            names = entity_map.get(f"{base}.{ext}")
            if names:
                return names[0]
    return None


def to_glb_entity(entity, out_glb, p4k, lod=0, no_interior=True, no_attachments=False, no_lights=True):
    args = ["entity", "export", entity, str(out_glb), "--p4k", str(p4k),
            "--materials", "none", "--lod", str(lod)]
    if no_interior:
        args.append("--no-interior")
    if no_attachments:
        args.append("--no-attachments")
    if no_lights:
        args.append("--no-lights")
    r = _run(args)
    if r.returncode != 0 or not Path(out_glb).is_file():
        raise ConvertError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "entity export a echoue")


def to_glb_skin(asset_path, exts, out_glb, p4k):
    """asset_path : chemin p4k sans extension."""
    geom_ext = "skinm" if "skin" in exts or "skinm" in exts else "cgfm"
    # StarBreaker compare les chemins du p4k avec des separateurs Windows (Data\Objects\...)
    pattern = f"{asset_path}.{geom_ext}".replace("/", "\\")
    r = _run(["skin", "export", pattern, str(out_glb), "--p4k", str(p4k)])
    if r.returncode != 0 or not Path(out_glb).is_file():
        raise ConvertError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "skin export a echoue")


def glb_to_stl(glb, stl, scale=1.0, z_up=False):
    """Fusionne la scene (transformations appliquees) en un seul maillage STL.

    scale : 1.0 = metres (unite du jeu) ; 1000.0 = millimetres.
    z_up  : convertit le Y-up de glTF en Z-up (convention des trancheurs).
    """
    import numpy as np
    import trimesh

    scene = trimesh.load(str(glb), force="scene")
    mesh = scene.dump(concatenate=True)
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        raise ConvertError("le GLB ne contient aucune surface")
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    if z_up:
        mesh.apply_transform(np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=float))
    if scale != 1.0:
        mesh.apply_scale(scale)
    mesh.export(str(stl))
    return {
        "faces": int(len(mesh.faces)),
        "extents": [round(float(x), 3) for x in mesh.extents],
        "watertight": bool(mesh.is_watertight),
    }


BLENDER_SCRIPT = HERE / "blender_tasks.py"
CONFIG_FILE = DATA_DIR / "config.json"


def cache_path(p4k_path, name):
    """Fichier de cache propre a un Data.p4k (LIVE, PTU... ne se melangent pas)."""
    import hashlib
    full = os.path.abspath(str(p4k_path))
    key = hashlib.sha1(full.lower().encode("utf-8")).hexdigest()[:10]
    d = DATA_DIR / "cache" / f"{safe_name(Path(full).parent.name) or 'p4k'}_{key}"
    d.mkdir(parents=True, exist_ok=True)
    return d / name


TEMP_PREFIXES = ("sc_glb_", "sc_prev_")


def purge_temp(max_age_hours=12):
    """Supprime les dossiers temporaires de l'outil laisses par une session interrompue.

    Retourne (nombre de dossiers, octets liberes). Seuls les dossiers de l'outil (prefixes connus),
    plus vieux que max_age_hours, sont touches : ceux d'une session encore ouverte sont epargnes.
    """
    import shutil
    import tempfile
    import time
    root = Path(tempfile.gettempdir())
    n, freed, limit = 0, 0, time.time() - max_age_hours * 3600
    for d in root.iterdir():
        if not d.is_dir() or not d.name.startswith(TEMP_PREFIXES):
            continue
        try:
            if d.stat().st_mtime > limit:
                continue
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)
            n, freed = n + 1, freed + size
        except OSError:
            pass
    return n, freed


def load_config():
    import json
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(**values):
    import json
    cfg = load_config()
    cfg.update(values)
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def find_blender():
    """blender.exe : variable SC_BLENDER, sinon le Blender portable du dossier d'outils."""
    env = os.environ.get("SC_BLENDER")
    if env and Path(env).is_file():
        return Path(env)
    for base in (TOOLS_DIR / "blender",):
        if base.is_dir():
            found = sorted(base.glob("**/blender.exe"))
            if found:
                return found[0]
    return None


def run_blender(task_args, timeout=1800, on_progress=None):
    """Execute blender_tasks.py en headless ; retourne le dict de la ligne 'RESULT:'.

    on_progress(pourcentage, message) est appele a chaque ligne 'PROGRESS:' emise par Blender.
    """
    import json
    exe = find_blender()
    if exe is None:
        raise ConvertError("Blender introuvable (attendu dans tools/blender ou variable SC_BLENDER)")
    proc, done = _popen_guarded([str(exe), "--background", "--factory-startup", "--python", str(BLENDER_SCRIPT),
                                 "--", *[str(a) for a in task_args]],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    result, tail = None, []
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line.startswith("RESULT: "):
                result = json.loads(line[8:])
            elif line.startswith("PROGRESS: "):
                if on_progress:
                    pct, _, msg = line[10:].partition(" ")
                    try:
                        on_progress(int(pct), msg)
                    except ValueError:
                        pass
            elif line.strip():
                tail = (tail + [line])[-3:]
        proc.wait()
    finally:
        killer.cancel()
    done()  # leve ConvertError si le processus a ete arrete (memoire, bouton Arreter, fermeture)
    if result is not None:
        return result
    raise ConvertError("Blender a echoue : " + " | ".join(tail))


def render_views(stl, prefix, size=640, on_progress=None):
    return run_blender(["render", stl, prefix, "--size", size], on_progress=on_progress)


# ---------- estimation de la duree d'une reparation ----------
TIMING_FILE = DATA_DIR / "repair_timings.json"
VOXEL_STEPS = {72, 90}  # etapes qui n'existent qu'avec un remaillage voxel
NO_VOXEL_STEPS = {55}   # fermeture des trous : sautee quand un voxel est utilise


def stl_face_count(path):
    """Nombre de triangles d'un STL binaire (lu dans l'en-tete), ou None."""
    import struct
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(84)
        n = struct.unpack("<I", head[80:84])[0]
        return n if size == 84 + 50 * n else None
    except (OSError, struct.error):
        return None


def _load_timings():
    import json
    try:
        data = json.loads(TIMING_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record_repair_timing(res, wall, faces=None):
    """Ajoute une reparation reussie a l'historique : l'estimation s'affine avec l'usage.

    faces : nombre de faces lu dans le STL source (meme mesure que celle utilisee pour estimer).
    """
    import json
    if not res.get("steps"):
        return
    recs = _load_timings()
    recs.append({"faces": faces or res["before"]["faces"], "faces_out": res["after"]["faces"],
                 "grid": res.get("grid", 0), "steps": res["steps"], "wall": round(wall, 2)})
    try:
        TIMING_FILE.write_text(json.dumps(recs[-300:], indent=1), encoding="utf-8")
    except OSError:
        pass


def _features(faces, grid):
    return [faces / 1e6, (grid / 1000.0) ** 2, 1.0]


def estimate_repair(faces, grid):
    """Duree estimee (s) par etape et au total, d'apres l'historique. None sans historique.

    Modele par etape : duree = a * millions de faces + b * (grille / 1000)^2 + c,
    ajuste par moindres carres sur les reparations deja faites sur cette machine.
    """
    import numpy as np
    recs = _load_timings()
    if not recs or not faces:
        return None
    steps = {}
    for p in sorted({p for r in recs for p, _d in r["steps"]}):
        if (p in VOXEL_STEPS and grid <= 0) or (p in NO_VOXEL_STEPS and grid > 0):
            continue
        rows = [(_features(r["faces"], r["grid"]), d) for r in recs for q, d in r["steps"] if q == p
                and (p not in VOXEL_STEPS or r["grid"] > 0) and (p not in NO_VOXEL_STEPS or r["grid"] == 0)]
        if not rows:
            continue
        x = np.array([f for f, _d in rows])
        y = np.array([d for _f, d in rows])
        coef = np.clip(np.linalg.lstsq(x, y, rcond=None)[0], 0, None)
        steps[p] = float(np.dot(coef, _features(faces, grid)))
    overhead = sorted(max(r["wall"] - sum(d for _p, d in r["steps"]), 0) for r in recs)
    startup = overhead[len(overhead) // 2]
    total = startup + sum(steps.values())
    # precision annoncee : plus l'historique est fourni et proche du cas demande, meilleure elle est
    near = [r for r in recs if r["grid"] > 0] if grid > 0 else [r for r in recs if r["grid"] == 0]
    biggest = max((r["grid"] for r in near), default=0) if grid > 0 else max((r["faces"] for r in near), default=0)
    extrapolated = (grid > 0 and grid > 1.3 * biggest) or (grid <= 0 and faces > 1.3 * biggest)
    return {"steps": steps, "startup": startup, "total": total, "n": len(recs), "extrapolated": extrapolated}


def scale_stl(src, dst, factor):
    """Redimensionne un STL binaire du meme facteur sur les 3 axes (proportions conservees).

    Les normales sont inchangees par un facteur uniforme. Retourne les dimensions du resultat.
    """
    import numpy as np
    if not os.path.isfile(src):
        raise ConvertError(f"fichier introuvable : {src}")
    n = stl_face_count(src)
    if n is None:
        raise ConvertError("STL non binaire ou abime : redimensionnement impossible")
    rec = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    with open(src, "rb") as f:
        header = f.read(80)
        f.read(4)
        tri = np.fromfile(f, dtype=rec, count=n)
    tri["v"] *= np.float32(factor)
    pts = tri["v"].reshape(-1, 3)
    ext = (pts.max(axis=0) - pts.min(axis=0)).tolist() if n else [0, 0, 0]
    with open(dst, "wb") as f:
        f.write(header[:80].ljust(80, b" "))
        f.write(np.uint32(n).tobytes())
        tri.tofile(f)
    return [round(e, 3) for e in ext]


# ---------- prevision de la taille du resultat voxel ----------
PROBE_GRIDS = (500, 1000)
PROBE_FILE = DATA_DIR / "probe_cache.json"
RAM_BASE_GB, RAM_PER_MFACE_GB = 3.0, 0.65  # mesure : Bengal 96,8 M faces -> 62 Go au maximum


def _probe_key(stl, adapt):
    st = os.stat(stl)
    return f"{os.path.abspath(stl).lower()}|{st.st_size}|{int(st.st_mtime)}|{adapt:.2f}"


def probe_model(stl, adapt=0.0, on_progress=None):
    """Deux essais voxel rapides (grilles 500 et 1000) -> loi faces = f1000 * (grille / 1000) ^ exposant.

    Precis pour viser une taille proche de ces grilles (essai Bengal : 9,1 M faces pour 10 M visees) ;
    surestime quand on extrapole tres loin (ce qui reste prudent). Resultat mis en cache par fichier.
    """
    import json
    import math
    key = _probe_key(stl, adapt)
    try:
        cache = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    res = []
    for i, g in enumerate(PROBE_GRIDS):
        if on_progress:
            on_progress(int(100 * i / len(PROBE_GRIDS)), f"essai rapide {i + 1}/{len(PROBE_GRIDS)} ({g} cubes)")
        res.append(run_blender(["probe", stl, "--grid", g, "--adapt", adapt]))
    f1, f2 = max(res[0]["faces"], 1), max(res[1]["faces"], 1)
    data = {"f_ref": f2, "g_ref": PROBE_GRIDS[1], "exponent": max(math.log(f2 / f1) / math.log(2), 1.5),
            "extent": res[1]["extent"], "faces_in": res[1]["faces_in"], "adapt": adapt}
    cache[key] = data
    try:
        PROBE_FILE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError:
        pass
    return data


def predict_result(probe, grid):
    """Faces, taille du STL (octets) et RAM maximale (octets) prevues pour une grille donnee."""
    faces = probe["f_ref"] * (grid / probe["g_ref"]) ** probe["exponent"]
    return {"faces": faces, "stl_bytes": 84 + 50 * faces,
            "ram_bytes": (RAM_BASE_GB + RAM_PER_MFACE_GB * faces / 1e6) * 1e9}


def grid_for_stl_size(probe, max_bytes):
    """Grille (cubes sur la plus grande dimension) donnant un STL d'environ max_bytes."""
    faces = max((max_bytes - 84) / 50, 1)
    return probe["g_ref"] * (faces / probe["f_ref"]) ** (1 / probe["exponent"])


def fmt_bytes(b):
    return f"{b / 1e9:.1f} Go" if b >= 1e9 else f"{b / 1e6:.0f} Mo"


def fmt_duration(s):
    s = max(0, int(round(s)))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def heatmap_views(original, repaired, prefix, threshold, size=640, on_progress=None):
    return run_blender(["heatmap", original, repaired, prefix, "--threshold", threshold, "--size", size],
                       on_progress=on_progress)


def nice_scale(need):
    """Plus petite echelle 'ronde' 1:N avec N >= need (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8 x 10^k)."""
    import math
    if need <= 1:
        return 1
    mag = 10 ** math.floor(math.log10(need))
    for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * mag >= need - 1e-9:
            return int(round(step * mag))
    return int(math.ceil(need))


def hollow_stl(src, dst, wall, on_progress=None):
    """Evidement a la taille d'impression : cavite a 'wall' mm de toutes les surfaces (SDF)."""
    return run_blender(["hollow", src, dst, "--wall", wall], on_progress=on_progress)


def split_stl(src, outdir, factor, size, on_progress=None, pins=None):
    """pins : (rayon, profondeur) en mm des trous d'alignement, ou None."""
    args = ["split", src, outdir, "--factor", factor, "--size", *size]
    if pins:
        args += ["--pins", pins[0], pins[1]]
    return run_blender(args, timeout=4 * 3600, on_progress=on_progress)


def _split_stl_old(src, outdir, factor, size, on_progress=None):
    """Decoupe en morceaux rebouches, a la taille d'impression (factor : unite du STL -> mm).

    size : (X, Y, Z) volume d'impression en mm. Ecrit les morceaux et assemblage.txt dans outdir.
    """
    return run_blender(["split", src, outdir, "--factor", factor, "--size", *size], timeout=4 * 3600,
                       on_progress=on_progress)


def precise_stl(src, dst, thick, weld=0.01, on_progress=None):
    """Reparation precise : pieces fermees intactes, toles ouvertes epaissies de 'thick' (unite du STL)."""
    return run_blender(["precise", src, dst, "--thick", thick, "--weld", weld], on_progress=on_progress)


def repair_stl(src, dst, weld=0.01, hole=64, voxel=0.0, min_island=0.0, on_progress=None, adapt=0.0):
    """adapt (0 a 1) : simplifie les zones plates apres le voxel ; reduit fortement taille et memoire."""
    return run_blender(["repair", src, dst, "--weld", weld, "--hole", hole, "--voxel", voxel,
                        "--min-island", min_island, "--adapt", adapt], on_progress=on_progress)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def convert_asset(asset_path, exts, entity_map, p4k, out_dir, tmp_dir, lod=0, scale=1.0, z_up=False,
                  no_interior=True, no_attachments=False, remove=()):
    """Convertit un asset en STL. Retourne (chemin_stl, infos, description_methode)."""
    name = asset_path.rsplit("/", 1)[-1]
    entity = entity_for_asset(entity_map, asset_path, exts) if "cga" in exts or entity_map else None
    glb = Path(tmp_dir) / f"{safe_name(name)}.glb"
    stl = Path(out_dir) / f"{safe_name(name)}.stl"
    if entity:
        to_glb_entity(entity, glb, p4k, lod, no_interior, no_attachments)
        method = f"entite {entity}"
    elif exts & {"cgf", "cgfm", "skin", "skinm"}:
        to_glb_skin(asset_path, exts, glb, p4k)
        method = "geometrie directe"
    elif exts <= {"chr", "chrm"}:
        raise ConvertError("fichier .chr seul : c'est un squelette sans geometrie ; prenez le .cdf ou le "
                           ".skin du meme dossier")
    elif "cdf" in exts:
        raise ConvertError("assemblage .cdf sans entite du jeu associee : convertissez ses pieces .skin")
    else:
        raise ConvertError("piece .cga sans entite associee : selectionnez plutot le vaisseau ou l'objet complet")
    info = glb_to_stl_fast(glb, stl, scale, z_up, remove)
    try:
        glb.unlink()
    except OSError:
        pass
    return stl, info, method


# elements d'un vaisseau qu'on peut retirer a l'export (reperes par le nom de leur point d'attache)
REMOVE_LABELS = [("gear", "Train d'atterrissage"), ("weapons", "Armes et tourelles"),
                 ("missiles", "Missiles et bombes"), ("thrusters", "Propulseurs"),
                 ("countermeasures", "Contre-mesures")]


def glb_to_stl_fast(glb, stl, scale=1.0, z_up=False, remove=()):
    """Blender si disponible (environ 2x plus rapide et bien moins de memoire), sinon trimesh.

    remove : groupes d'elements a retirer (cles de REMOVE_LABELS), uniquement avec Blender.
    """
    if find_blender() is not None:
        args = ["glb2stl", glb, stl, "--scale", scale, "--zup", 1 if z_up else 0]
        if remove:
            args += ["--remove", ",".join(remove)]
        res = run_blender(args)
        if "error" in res:
            raise ConvertError(res["error"])
        return res
    return glb_to_stl(glb, stl, scale, z_up)


def convert_entity(entity, p4k, out_dir, tmp_dir, lod=0, scale=1.0, z_up=False,
                   no_interior=True, no_attachments=False, remove=()):
    """Convertit directement une entite du jeu (vaisseau, vehicule) en STL."""
    glb = Path(tmp_dir) / f"{safe_name(entity)}.glb"
    stl = Path(out_dir) / f"{safe_name(entity)}.stl"
    to_glb_entity(entity, glb, p4k, lod, no_interior, no_attachments)
    info = glb_to_stl_fast(glb, stl, scale, z_up, remove)
    try:
        glb.unlink()
    except OSError:
        pass
    return stl, info, f"entite {entity}"


# ---------- objets (armes, gadgets, equipements...) ----------
OBJECT_CACHE_VERSION = 1
OBJECT_TYPE_LABELS = {
    "WeaponPersonal": "Armes personnelles", "WeaponAttachment": "Accessoires d'armes et munitions",
    "FPS_Consumable": "Consommables (MedPen, stylos...)", "Gadget": "Gadgets", "WeaponGun": "Armes de vaisseau",
    "Missile": "Missiles et torpilles", "MissileLauncher": "Lance-missiles", "Turret": "Tourelles",
    "TurretBase": "Tourelles", "Shield": "Boucliers", "PowerPlant": "Generateurs", "Cooler": "Refroidisseurs",
    "QuantumDrive": "Moteurs quantiques", "MainThruster": "Propulseurs", "ManneuverThruster": "Propulseurs",
    "Container": "Conteneurs", "Cargo": "Caisses de cargo", "Usable": "Objets utilisables (sieges, lits...)",
    "Door": "Portes", "Misc": "Divers", "Food": "Nourriture", "Drink": "Boissons", "Bottle": "Boissons",
    "Paints": "Peintures", "SeatAccess": "Acces aux sieges", "NOITEM_Player": "Personnages",
    "UNDEFINED": "Sans type", "Light": "Eclairages", "MiningModifier": "Modules de minage",
    "Seat": "Sieges", "ShopDisplay": "Presentoirs de boutique", "ControlPanel": "Panneaux de commande",
    "FlightController": "Commandes de vol", "SeatDashboard": "Tableaux de bord", "WeaponDefensive": "Contre-mesures",
    "Display": "Ecrans", "Decal": "Decalcomanies", "Flair_Surface": "Decorations", "AttachedPart": "Pieces attachees",
}
OBJECT_CACHE_VERSION = 2  # incremente a chaque changement des noms ou des categories


def _dcb_column(prop_path, p4k_path):
    """{entite: valeur} pour une propriete DataCore de toutes les entites."""
    r = _run(["dcb", "query", prop_path, "--filter", "*", "--p4k", str(p4k_path)], timeout=600)
    if r.returncode != 0:
        raise ConvertError(f"dcb query a echoue : {r.stderr.strip()[-300:]}")
    out = {}
    for line in r.stdout.splitlines():
        if "\t" in line and line.startswith("EntityClassDefinition."):
            name, val = line.split("\t", 1)
            out[name.split(".", 1)[1]] = val.strip()
    return out


def _localization(p4k):
    """Traductions du jeu {cle en minuscules: texte} : francais, complete par l'anglais."""
    loc = {}
    for lang in ("english", "french_(france)"):  # le francais ecrase l'anglais quand il existe
        try:
            with p4k.open(f"Data/Localization/{lang}/global.ini") as f:
                raw = f.read()
        except (KeyError, OSError):
            continue
        for bline in raw.splitlines():
            try:  # le fichier melange UTF-8 et Windows-1252 selon les lignes
                line = bline.decode("utf-8")
            except UnicodeDecodeError:
                line = bline.decode("cp1252", errors="replace")
            k, sep, v = line.lstrip("﻿").partition("=")
            v = v.strip()
            if not sep or not v or v.startswith("!") or "TRANSLATION NOT FOUND" in v:
                continue  # textes de remplacement du jeu : on garde l'anglais
            loc[k.strip().lower()] = v
    return loc


def object_category(kind):
    if kind.startswith("Char_Armor"):
        return "Armures"
    if kind.startswith("Char_Clothing") or kind.startswith("Char_Accessory"):
        return "Vetements et accessoires"
    if not kind:
        return "Decor et accessoires (sans type)"
    return OBJECT_TYPE_LABELS.get(kind, kind)


def load_objects(p4k_path, p4k, entity_map, cache_file):
    """Liste des objets avec un modele 3D (hors vehicules) : [(entite, nom affiche, categorie, modele)]."""
    import json
    if cache_file.is_file() and cache_file.stat().st_mtime > Path(p4k_path).stat().st_mtime:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("v") == OBJECT_CACHE_VERSION:
            return [tuple(x) for x in data["list"]]
    kinds = _dcb_column("EntityClassDefinition.Components[SAttachableComponentParams].AttachDef.Type", p4k_path)
    keys = _dcb_column("EntityClassDefinition.Components[SAttachableComponentParams].AttachDef.Localization.Name",
                       p4k_path)
    loc = _localization(p4k)
    out = []
    for geom, ents in entity_map.items():
        for ent in ents:
            kind = kinds.get(ent, "")
            if kind == "NOITEM_Vehicle":
                continue  # deja dans l'onglet Vaisseaux
            key = keys.get(ent, "").lstrip("@").lower()
            name = loc.get(key, "")
            if not name or "placeholder" in name.lower() or name.startswith("@"):
                name = ent
            out.append((ent, name, object_category(kind), geom))
    out.sort(key=lambda x: (x[2], x[1].lower()))
    cache_file.write_text(json.dumps({"v": OBJECT_CACHE_VERSION, "list": out}), encoding="utf-8")
    return out


# jamais gardes : gabarits, debris, tests, objets de mission...
def convert_socpak(path, p4k, out_dir, tmp_dir, lod=0, scale=1.0, z_up=False, no_attachments=False, **_ignored):
    """Convertit un conteneur d'objets (.socpak : lieu, batiment, interieur) en STL.

    path : chemin complet dans le p4k (Data/ObjectContainers/...). Les options vaisseau (no_interior)
    sont ignorees : pour un lieu, l'interieur fait partie du contenu.
    """
    parts = path.replace("\\", "/").split("/")
    name = safe_name(f"{parts[-2]}_{parts[-1].rsplit('.', 1)[0]}")  # ex. BrokenMoon_global
    glb = Path(tmp_dir) / f"{name}.glb"
    stl = Path(out_dir) / f"{name}.stl"
    args = ["socpak", "export", path.replace("/", "\\"), str(glb), "--p4k", str(p4k),
            "--materials", "none", "--lod", str(lod), "--no-lights"]
    if no_attachments:
        args.append("--no-attachments")
    r = _run(args)
    if r.returncode != 0 or not glb.is_file():
        err = (r.stderr or r.stdout).strip().splitlines()
        raise ConvertError(err[-1] if err else "socpak export a echoue")
    info = glb_to_stl_fast(glb, stl, scale, z_up)
    try:
        glb.unlink()
    except OSError:
        pass
    return stl, info, "conteneur"


VEHICLE_EXCLUDE = re.compile(
    r"template|salvageabledebris|_shop|test|_wreck|derelict|_tutorial|_dummy|_display|_mission"
    r"|_exterior$|^ea|^probe|^orbital_sentry",
    re.IGNORECASE)
# variantes IA / PNJ : gardees seulement si aucune autre entite n'utilise le meme modele
# (ex. le Bengal ou l'Aurora classique n'existent qu'en version IA)
VEHICLE_SECONDARY = re.compile(
    r"_pu_ai|_ai_|_unmanned|_npc|_hijacked|_boarded|_s42|_ea_|_pirate|_crim|_civ", re.IGNORECASE)
VEHICLE_CACHE_VERSION = 2


def load_vehicles(p4k_path, entity_map, cache_file):
    """Liste des vehicules : [(entite, geometrie)], triee. Mis en cache a cote du projet."""
    import json
    if cache_file.is_file() and cache_file.stat().st_mtime > Path(p4k_path).stat().st_mtime:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("v") == VEHICLE_CACHE_VERSION:
            return [tuple(x) for x in data["list"]]
    r = _run(["dcb", "query", "EntityClassDefinition.Components[SAttachableComponentParams].AttachDef.Type",
              "--filter", "*", "--p4k", str(p4k_path)], timeout=600)
    if r.returncode != 0:
        raise ConvertError(f"dcb query a echoue : {r.stderr.strip()[-400:]}")
    geom_of = {e: g for g, es in entity_map.items() for e in es}
    primary, secondary = {}, {}
    for line in r.stdout.splitlines():
        if "\t" not in line:
            continue
        name, kind = line.split("\t", 1)
        if kind.strip() != "NOITEM_Vehicle" or not name.startswith("EntityClassDefinition."):
            continue
        ent = name.split(".", 1)[1]
        if ent not in geom_of or VEHICLE_EXCLUDE.search(ent):
            continue
        bucket = secondary if VEHICLE_SECONDARY.search(ent) else primary
        bucket.setdefault(geom_of[ent], []).append(ent)
    out = [(e, g) for g, es in primary.items() for e in es]
    out += [(e, g) for g, es in secondary.items() if g not in primary for e in es]
    out.sort(key=lambda x: x[0].lower())
    cache_file.write_text(json.dumps({"v": VEHICLE_CACHE_VERSION, "list": out}), encoding="utf-8")
    return out
