"""Reperes 3D cliquables pour le web : coque legere + positions des composants, portes, sieges...

    python sc_markers.py <entite | liste.txt> --out <dossier> [--lod N] [--tris 150000]
                         [--compress draco|meshopt|none] [--interior] [--force] [--p4k <Data.p4k>]

Pour chaque entite (ex. AEGS_Redeemer), dans <out>/<entite>/ :
  hull.glb      coque fusionnee et decimee (sans textures, compressee), repere glTF : metres, Y en haut
  markers.json  reperes {kind, name, item, size, pos, rot} dans ce meme repere
  report.txt    comptes par type + noms non classes les plus frequents (pour affiner les regles)
Le GLB source est exporte par StarBreaker AVEC l'interieur et les equipements (sans --no-interior ni
--no-attachments) : ce sont eux qui portent les points d'attache, les portes, les sieges...
Mode lot : une entite par ligne dans un .txt ; les entites deja faites sont sautees (sauf --force),
les echecs sont notes dans <out>/errors.log sans arreter le lot. L'export STL n'est pas modifie.
"""
import argparse
import collections
import json
import math
import re
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))  # lancement direct : modules du projet
import sc_stl  # noqa: E402

SUFFIX_RE = re.compile(r"(_[0-9a-f]{6}|\.\d{3})$", re.IGNORECASE)  # suffixes ajoutes par StarBreaker / Blender
SIZE_RE = re.compile(r"(?:^|_)s(\d{1,2})(?:_|$)", re.IGNORECASE)

# type d'apres la classe de l'objet monte (prioritaire : c'est ce qui est reellement installe)
ITEM_RULES = [
    (r"^powr_", "powerplant"), (r"^cool_", "cooler"), (r"^shld_", "shield"), (r"^qdrv_", "quantum"),
    (r"^(msl|misl|mrck|bomb|torp)|missile|torpedo|bomb_rack", "missile"),
    (r"turret", "turret"),
    (r"^mount_gimbal|^mount_|cannon|repeater|gatling|scattergun|_laser|ballistic|distortion|neutron|tachyon",
     "weapon"),
    (r"bed|bunk", "bed"), (r"seat|chair", "seat"), (r"door", "door"), (r"elevator|lift", "elevator"),
    (r"airlock", "airlock"), (r"ladder", "ladder"), (r"cargogrid|cargo", "cargo"),
    (r"screen|display|terminal|console|mfd|dashboard", "terminal"),
]
# type d'apres le nom du point d'attache
PORT_RULES = [
    (r"weapon_storage|_storage|locker", "other"),  # rateliers d'armes, rangements : pas des armes du vaisseau
    (r"power_?plant", "powerplant"), (r"cooler", "cooler"), (r"shield_gen", "shield"),
    (r"quantum_drive", "quantum"), (r"turret", "turret"), (r"missile|bomb|torpedo", "missile"),
    (r"weapon_(gun|wing|nose|pilot|tail|left|right)|_gun_|gimbal|hardpoint_weapon(?!locker|_regen)", "weapon"),
    (r"seat(?!_access)", "seat"), (r"bed", "bed"), (r"(?<!lg_)door", "door"), (r"elevator|lift", "elevator"),
    (r"airlock", "airlock"), (r"ladder", "ladder"), (r"ramp", "ramp"), (r"cargo", "cargo"),
    (r"screen|display|terminal|console|mfd|dashboard", "terminal"),
]
# points d'attache virtuels (aucun objet visible) : ignores sauf s'ils portent un maillage
VIRTUAL_PORT = re.compile(r"controller_|self_?destruct|seat_access|_radar|relay|ping|scanner|transponder|"
                          r"life_support|paint|armor|flair|audio|_lights?$|weapon_regen|fuel_intake|"
                          r"fuel_tank|_mec$|_pow$|_qig$|weaponlocker|_controller$", re.IGNORECASE)
# ces types viennent du point d'attache meme si l'objet monte dit autre chose (la rampe porte une "Door_...")
PORT_WINS = {"ramp", "elevator", "airlock", "ladder", "other"}
# noeuds ordinaires (hors points d'attache) : on garde le plus haut noeud de chaque ensemble
NODE_RULES = [
    (r"(?<![a-z])(lg|landing_?gear)|fuel_?port|locker", None),  # trappes de train / carburant, armoires
    (r"door_?frame|_light|light_|glow|label|decal|emissive|_lamp", None),  # cadres, lumieres, etiquettes
    (r"elevator|(?<![a-z])lift(?!er)", "elevator"), (r"airlock", "airlock"), (r"ladder", "ladder"),
    (r"(?<![a-z])door", "door"), (r"(?<![a-z])ramp(?!.*(piston|glass|proximity))", "ramp"),
    (r"(?<![a-z])bed(?![a-z])|bed_|bunk|mattress", "bed"),
    (r"screen|monitor|terminal|console|mfd|control_?panel", "terminal"),
    (r"cargo_?grid", "cargo"), (r"(?<![a-z])(seat|chair)(?!_access)", "seat"),
]
ROOM_RE = re.compile(r"cockpit|bridge|habitation|(?<![a-z])hab(?![a-z])|galley|kitchen|toilet|shower|"
                     r"medbay|med_bay|armou?ry|engineering|lounge|corridor|hangar|(?<![a-z])room", re.IGNORECASE)
NOT_ROOMS = {"ui", "common", "shared", "lights", "lighting", "props"}  # dossiers de l'interieur qui ne sont pas des pieces
KINDS = ["powerplant", "cooler", "shield", "quantum", "weapon", "turret", "missile", "door", "elevator",
         "airlock", "seat", "ladder", "ramp", "cargo", "bed", "terminal", "other"]


def clean(name):
    prev = None
    while prev != name:
        prev, name = name, SUFFIX_RE.sub("", name)
    return name


def first_kind(rules, text):
    for rx, kind in rules:
        if re.search(rx, text, re.IGNORECASE):
            return kind or "__skip__"
    return None


# ---------- lecture du GLB : transformations monde et boites englobantes sans charger les maillages ----------
def read_glb(path):
    """JSON du GLB, avec le bloc binaire attache en '_bin' (utile aux boites englobantes exactes)."""
    data = Path(path).read_bytes()
    n = struct.unpack("<I", data[12:16])[0]
    g = json.loads(data[20:20 + n])
    off = 20 + n
    if off + 8 <= len(data):
        blen = struct.unpack("<I", data[off:off + 4])[0]
        g["_bin"] = memoryview(data)[off + 8:off + 8 + blen]
    return g


_COMP = {5121: np.uint8, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def accessor(g, idx):
    acc = g["accessors"][idx]
    bv = g["bufferViews"][acc["bufferView"]]
    dt, nc = np.dtype(_COMP[acc["componentType"]]), _NCOMP[acc["type"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or dt.itemsize * nc
    raw = np.frombuffer(g["_bin"], dtype=np.uint8, count=stride * (acc["count"] - 1) + dt.itemsize * nc,
                        offset=start)
    if stride == dt.itemsize * nc:
        return raw.view(dt).reshape(acc["count"], nc)
    return np.lib.stride_tricks.as_strided(raw.view(dt), shape=(acc["count"], nc),
                                           strides=(stride, dt.itemsize))


def local_matrix(nd):
    if "matrix" in nd:
        return np.array(nd["matrix"], float).reshape(4, 4).T
    x, y, z, w = nd.get("rotation", [0.0, 0.0, 0.0, 1.0])
    r = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                  [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                  [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = r * np.array(nd.get("scale", [1.0, 1.0, 1.0]))
    m[:3, 3] = nd.get("translation", [0.0, 0.0, 0.0])
    return m


def quat(m):
    """Quaternion (x, y, z, w) de la rotation d'une matrice monde (echelle retiree)."""
    u, _s, vt = np.linalg.svd(m[:3, :3])
    r = u @ vt
    if np.linalg.det(r) < 0:
        r = -r
    t = np.trace(r)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        q = [(r[2, 1] - r[1, 2]) / s, (r[0, 2] - r[2, 0]) / s, (r[1, 0] - r[0, 1]) / s, 0.25 * s]
    else:
        i = int(np.argmax(np.diag(r)))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = math.sqrt(1.0 + r[i, i] - r[j, j] - r[k, k]) * 2
        q = [0.0, 0.0, 0.0, (r[k, j] - r[j, k]) / s]
        q[i] = 0.25 * s
        q[j] = (r[j, i] + r[i, j]) / s
        q[k] = (r[k, i] + r[i, k]) / s
    return [round(float(v), 5) for v in q]


class Scene:
    def __init__(self, g):
        self.g, self.nodes = g, g["nodes"]
        self.parent = {c: i for i, nd in enumerate(self.nodes) for c in nd.get("children", [])}
        self._world, self._bbox = {}, {}
        # boites de chaque maillage a partir des sommets REELLEMENT utilises : StarBreaker partage un tampon
        # de sommets entre pieces, et les min/max du glTF couvrent alors tout le tampon (boites geantes)
        self.mesh_box, cache = {}, {}
        for mi, mesh in enumerate(g.get("meshes", [])):
            lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
            for prim in mesh.get("primitives", []):
                key = (prim["attributes"]["POSITION"], prim.get("indices"))
                if key not in cache:
                    try:
                        pos = accessor(g, key[0])
                        if key[1] is not None:
                            pos = pos[np.unique(accessor(g, key[1]).ravel())]
                        cache[key] = (pos.min(axis=0), pos.max(axis=0)) if len(pos) else None
                    except (KeyError, ValueError):  # pas de bloc binaire lisible : bornes du glTF
                        acc = g["accessors"][key[0]]
                        cache[key] = (np.array(acc["min"]), np.array(acc["max"])) if "min" in acc else None
                if cache[key] is not None:
                    lo, hi = np.minimum(lo, cache[key][0]), np.maximum(hi, cache[key][1])
            if np.all(np.isfinite(lo)):
                self.mesh_box[mi] = (lo, hi)

    room = {}   # noeud -> zone de l'interieur (dossier du .cgf), rempli par name_interiors
    names = {}  # noeud -> nom deduit pour les objets d'interieur sans nom dans le GLB

    def name(self, i):
        return self.nodes[i].get("name", "") or self.names.get(i, "")

    def name_interiors(self, hierarchy, tol=0.05):
        """Nomme les objets d'interieur (sans nom dans le GLB) d'apres le JSON de hierarchie de StarBreaker.

        Chaque objet du conteneur 'interior_<x>' est associe a l'entree du JSON la plus proche (position
        du jeu, Z en haut, convertie en glTF, Y en haut) : ecart mesure de 5 a 9 mm sur le Redeemer.
        """
        self.room, self.names = {}, {}
        named = 0
        for inter in hierarchy.get("interiors", []):
            meshes = inter.get("meshes", [])
            ci = next((k for k in range(len(self.nodes)) if self.nodes[k].get("name") == "interior_" + inter["container"]),
                      None)
            if ci is None or not meshes:
                continue
            jp = np.array([(m["pos"][0], m["pos"][2], -m["pos"][1]) for m in meshes], float)
            for k in self.subtree(ci):
                if k == ci or self.nodes[k].get("name") or "mesh" not in self.nodes[k]:
                    continue
                d = np.linalg.norm(jp - self.world(k)[:3, 3], axis=1)
                j = int(d.argmin())
                if d[j] > tol:
                    continue
                cgf = meshes[j]["cgf"].replace("\\", "/")
                self.names[k] = cgf.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                parts = cgf.lower().split("/")
                if "interior" in parts and parts.index("interior") + 1 < len(parts) - 1:
                    self.room[k] = parts[parts.index("interior") + 1]
                named += 1
        return named

    def world(self, i):
        if i not in self._world:
            p = self.parent.get(i)
            self._world[i] = (self.world(p) if p is not None else np.eye(4)) @ local_matrix(self.nodes[i])
        return self._world[i]

    def bbox(self, i):
        """Boite englobante monde de tout le sous-arbre (None s'il n'y a aucun maillage)."""
        if i in self._bbox:
            return self._bbox[i]
        lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
        nd = self.nodes[i]
        if nd.get("mesh") in self.mesh_box:
            mlo, mhi = self.mesh_box[nd["mesh"]]
            corners = np.array([[x, y, z, 1.0] for x in (mlo[0], mhi[0]) for y in (mlo[1], mhi[1])
                                for z in (mlo[2], mhi[2])])
            pts = (self.world(i) @ corners.T).T[:, :3]
            lo, hi = pts.min(axis=0), pts.max(axis=0)
        for c in nd.get("children", []):
            b = self.bbox(c)
            if b is not None:
                lo, hi = np.minimum(lo, b[0]), np.maximum(hi, b[1])
        self._bbox[i] = (lo, hi) if np.all(np.isfinite(lo)) else None
        return self._bbox[i]

    def subtree(self, i):
        stack, out = [i], []
        while stack:
            k = stack.pop()
            out.append(k)
            stack.extend(self.nodes[k].get("children", []))
        return out


# ---------- classement ----------
def item_of(scene, i):
    """Classe de l'objet monte sur un point d'attache : nom du premier enfant (ex. POWR_AEGS_S03_Centurion_SCItem)."""
    kids = scene.nodes[i].get("children", [])
    return clean(scene.name(kids[0])) if kids else ""


def build_markers(scene):
    markers, covered = [], set()
    ignored_ports, unclassified = collections.Counter(), collections.Counter()
    counter = collections.Counter()

    def add(i, kind, name, item=""):
        b = scene.bbox(i)
        w = scene.world(i)
        pos = ((b[0] + b[1]) / 2) if b is not None else w[:3, 3]
        size = None
        for src in (item, name):
            m = SIZE_RE.search(src or "")
            if m:
                size = int(m.group(1))
                break
        counter[kind] += 1
        mk = {"id": f"{kind}-{counter[kind]:03d}", "kind": kind, "name": name, "item": item or None,
              "size": size, "pos": [round(float(v), 3) for v in pos], "rot": quat(w)}
        room = scene.room.get(i)
        if room:
            mk["room"] = room
        if b is not None:
            mk["extent"] = [round(float(v), 3) for v in (b[1] - b[0])]
        markers.append(mk)
        covered.update(scene.subtree(i))

    # 1. points d'attache : classe de l'objet monte, sinon nom du point
    for i in range(len(scene.nodes)):
        nm = clean(scene.name(i))
        if not nm.lower().startswith("hardpoint_") or i in covered:
            continue
        item = item_of(scene, i)
        port_kind = first_kind(PORT_RULES, nm)
        kind = port_kind if port_kind in PORT_WINS else ((first_kind(ITEM_RULES, item) if item else None)
                                                         or port_kind)
        has_mesh = scene.bbox(i) is not None
        if nm.lower().endswith("_controller") or (VIRTUAL_PORT.search(nm) and not has_mesh):
            ignored_ports[nm] += 1  # points virtuels (controleurs, radars...) : pas de repere
            continue
        if kind in (None, "__skip__"):
            if VIRTUAL_PORT.search(nm) or not has_mesh:
                ignored_ports[nm] += 1
                continue
            kind = "other"
        add(i, kind, nm, item)

    # 2. autres noeuds (portes, rampes, lits, ecrans, pieces...) : le plus haut noeud de chaque ensemble
    for i in range(len(scene.nodes)):
        if i in covered:
            continue
        nm = clean(scene.name(i))
        kind = first_kind(NODE_RULES, nm)
        if kind == "__skip__":
            covered.update(scene.subtree(i))
            continue
        if kind is None and ROOM_RE.search(nm) and i not in scene.room:
            kind = "other"  # piece nommee hors interieur (ex. cockpit du vaisseau)
        if kind is None:
            if "mesh" in scene.nodes[i]:
                unclassified[nm] += 1
            continue
        p = scene.parent.get(i)
        if p is not None and first_kind(NODE_RULES, clean(scene.name(p))) == kind:
            continue  # le parent est deja un repere du meme type
        if scene.bbox(i) is None and kind != "other":
            continue
        add(i, kind, nm)

    # 3. un repere par zone d'interieur (habitation, bridge...) : centre de tous ses objets
    zones = collections.defaultdict(list)
    for i, room in scene.room.items():
        if room in NOT_ROOMS:
            continue
        b = scene.bbox(i)
        if b is not None:
            zones[room].append(b)
    for room, boxes in sorted(zones.items()):
        lo = np.min([b[0] for b in boxes], axis=0)
        hi = np.max([b[1] for b in boxes], axis=0)
        counter["other"] += 1
        markers.append({"id": f"other-{counter['other']:03d}", "kind": "other", "name": f"zone:{room}",
                        "item": None, "size": None, "pos": [round(float(v), 3) for v in (lo + hi) / 2],
                        "rot": [0.0, 0.0, 0.0, 1.0], "room": room,
                        "extent": [round(float(v), 3) for v in hi - lo]})
    return markers, ignored_ports, unclassified


# ---------- traitement d'une entite ----------
def export_glb(entity, glb, p4k, lod):
    args = ["entity", "export", entity, str(glb), "--p4k", str(p4k), "--materials", "none",
            "--lod", str(lod), "--no-lights"]
    r = sc_stl._run(args, timeout=3600)
    if r.returncode != 0 or not Path(glb).is_file():
        err = (r.stderr or r.stdout).strip().splitlines()
        raise sc_stl.ConvertError(err[-1] if err else "entity export a echoue")


def read_hierarchy(entity, p4k, work):
    """JSON de hierarchie de StarBreaker (--dump-hierarchy) ; tolere les virgules finales qu'il laisse."""
    path = Path(work) / "hierarchie.json"
    r = sc_stl._run(["entity", "export", entity, str(path), "--p4k", str(p4k), "--dump-hierarchy",
                     "--no-lights"], timeout=600)
    if r.returncode != 0 or not path.is_file():
        return {}
    return json.loads(re.sub(r",(\s*[\]}])", r"\1", path.read_text(encoding="utf-8")))


def process(entity, outdir, p4k, lod=1, tris=150000, compress="draco", interior=False, keep_glb=False, log=print):
    dest = Path(outdir) / sc_stl.safe_name(entity)
    dest.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="sc_glb_"))
    t0 = time.time()
    try:
        glb = work / "complet.glb"
        log(f"  export StarBreaker (lod {lod}, interieur et equipements inclus)...")
        export_glb(entity, glb, p4k, lod)
        scene = Scene(read_glb(glb))
        named = scene.name_interiors(read_hierarchy(entity, p4k, work))
        markers, ignored, unclassified = build_markers(scene)
        root = scene.g["scenes"][scene.g.get("scene", 0)]["nodes"]
        boxes = [scene.bbox(r) for r in root if scene.bbox(r) is not None]
        lo = np.min([b[0] for b in boxes], axis=0)
        hi = np.max([b[1] for b in boxes], axis=0)
        doc = {"entity": entity, "units": "m", "up": "y",
               "bbox": [[round(float(v), 3) for v in lo], [round(float(v), 3) for v in hi]],
               "markers": markers}
        (dest / "markers.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        log(f"  {len(markers)} reperes ; coque web...")
        hull = sc_stl.run_blender(["webhull", glb, dest / "hull.glb", "--tris", tris, "--interior",
                                   1 if interior else 0, "--compress", compress], timeout=3600)
        if "error" in hull:
            raise sc_stl.ConvertError(hull["error"])
        counts = collections.Counter(m["kind"] for m in markers)
        unclassified.pop("", None)
        lines = [f"Entite : {entity}", f"Noeuds dans le GLB : {len(scene.nodes)} "
                 f"(dont {named} objets d'interieur nommes d'apres la hierarchie)",
                 f"Coque : {hull['tris_in']} -> {hull['tris_out']} triangles, {hull['bytes'] / 1e6:.2f} Mo "
                 f"({hull['compress']}{', interieur inclus' if hull['interior'] else ''})",
                 f"Duree : {time.time() - t0:.0f} s", "", "Reperes par type :"]
        lines += [f"  {k:10s} {counts[k]}" for k in KINDS if counts[k]]
        lines += ["", "Points d'attache ignores (virtuels ou vides) :"]
        lines += [f"  {n} x{c}" for n, c in ignored.most_common(60)]
        lines += ["", "Noeuds avec maillage non classes les plus frequents (pour affiner les regles) :"]
        lines += [f"  {n} x{c}" for n, c in unclassified.most_common(80)]
        (dest / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if keep_glb:
            shutil.copy2(glb, dest / "complet.glb")
        return {"entity": entity, "dir": str(dest), "markers": len(markers), "counts": dict(counts),
                "hull_mb": round(hull["bytes"] / 1e6, 2), "tris": hull["tris_out"], "seconds": round(time.time() - t0)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_batch(entities, outdir, p4k, force=False, log=print, **kw):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    done, failed, skipped = [], [], []
    for n, ent in enumerate(entities, 1):
        dest = outdir / sc_stl.safe_name(ent)
        if not force and (dest / "markers.json").is_file() and (dest / "hull.glb").is_file():
            skipped.append(ent)
            log(f"[{n}/{len(entities)}] {ent} : deja fait, saute")
            continue
        log(f"[{n}/{len(entities)}] {ent}")
        try:
            r = process(ent, outdir, p4k, log=log, **kw)
            done.append(r)
            log(f"  ok : {r['markers']} reperes, coque {r['hull_mb']} Mo, {r['seconds']} s")
        except Exception as exc:  # noqa: BLE001  un vaisseau qui echoue n'arrete pas le lot
            failed.append(ent)
            with open(outdir / "errors.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{ent}\t{exc}\n")
            log(f"  ECHEC : {exc}")
    return done, failed, skipped


def read_entities(arg):
    p = Path(arg)
    if p.suffix.lower() == ".txt" and p.is_file():
        return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    return [arg]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sc_markers", description=__doc__.split("\n")[0])
    ap.add_argument("entities", help="entite (ex. AEGS_Redeemer) ou fichier .txt, une entite par ligne")
    ap.add_argument("--out", required=True)
    ap.add_argument("--lod", type=int, default=1)
    ap.add_argument("--tris", type=int, default=150000)
    ap.add_argument("--compress", choices=["draco", "meshopt", "none"], default="draco")
    ap.add_argument("--interior", action="store_true", help="garder l'interieur dans hull.glb")
    ap.add_argument("--force", action="store_true", help="refaire les entites deja faites")
    ap.add_argument("--keep-glb", action="store_true", help="garder le GLB complet de StarBreaker")
    ap.add_argument("--p4k", default=None)
    a = ap.parse_args(argv)
    p4k = a.p4k or sc_stl.load_config().get("p4k")
    if not p4k or not Path(p4k).is_file():
        sys.exit("Data.p4k introuvable : utilisez --p4k")
    ents = read_entities(a.entities)
    done, failed, skipped = run_batch(ents, a.out, p4k, force=a.force, lod=a.lod, tris=a.tris,
                                      compress=a.compress, interior=a.interior, keep_glb=a.keep_glb)
    print(f"\nTermine : {len(done)} faits, {len(skipped)} deja faits, {len(failed)} echecs"
          + (f" (voir {Path(a.out) / 'errors.log'})" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
