"""Navigateur de Data.p4k (Star Citizen) : liste des modeles 3D avec poids,
selection par arborescence et extraction de la selection.

Lancement : python sc_browser.py [chemin\\vers\\Data.p4k]
Test sans interface : python sc_browser.py --selftest
"""
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import sc_preview
import sc_setup
import sc_stl
import sc_update
from version import __version__
from sc_p4k import P4KFile

P4K_DEFAULT = r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Data.p4k"  # emplacement du lanceur RSI

# extensions de modeles ; les fichiers *m contiennent la geometrie de leur compagnon
# .cdf : assemblage de pieces (objets portes : MedPen, armures...) ; c'est lui que reference l'entite du jeu
MODEL_EXT = {"cgf", "cga", "skin", "chr", "cdf", "cgfm", "cgam", "skinm", "chrm"}
MAIN_EXT = {"cgf", "cga", "skin", "chr", "cdf"}
KIND = {
    "cga": "Geometrie articulee (cga)",
    "cgf": "Geometrie statique (cgf)",
    "skin": "Mesh skinne (skin)",
    "chr": "Personnage / rig (chr)",
    "cdf": "Assemblage de pieces (cdf)",
}
MAX_CHILDREN = 3000

DIR_DESC = {
    "data": "Racine des donnees",
    "data/objects": "Tous les objets 3D du jeu",
    "data/objects/spaceships": "Vaisseaux et equipements de vaisseaux",
    "data/objects/spaceships/ships": "Vaisseaux jouables, par constructeur",
    "data/objects/spaceships/derelicts": "Epaves de vaisseaux",
    "data/objects/spaceships/holoviewer_ships": "Vaisseaux du holoviewer (presentation)",
    "data/objects/spaceships/weapons": "Armes de vaisseaux",
    "data/objects/spaceships/turrets": "Tourelles",
    "data/objects/spaceships/seats": "Sieges",
    "data/objects/spaceships/thrusters": "Propulseurs",
    "data/objects/spaceships/power_plants": "Generateurs",
    "data/objects/spaceships/shields": "Boucliers",
    "data/objects/spaceships/coolers": "Refroidisseurs",
    "data/objects/spaceships/quantum_drives": "Moteurs quantiques",
    "data/objects/spaceships/missile_racks": "Supports de missiles",
    "data/objects/spaceships/bomb_racks": "Supports de bombes",
    "data/objects/spaceships/weapon_mounts": "Supports d'armes",
    "data/objects/buildingsets": "Kits de batiments, stations et lieux",
    "data/objects/characters": "Personnages, vetements, equipements",
    "data/objects/vehicles": "Vehicules terrestres",
    "data/objects/fps_weapons": "Armes FPS",
    "data/objects/planets": "Elements de planetes",
    "data/objects/planet": "Elements de planetes",
    "data/objects/space": "Objets spatiaux",
    "data/objects/props": "Accessoires de decor",
    "data/objects/organics": "Vegetation et organique",
}
MANUFACTURERS = {
    "aegs": "Aegis Dynamics", "anvl": "Anvil Aerospace", "argo": "Argo Astronautics",
    "banu": "Banu", "cnou": "Consolidated Outland", "crus": "Crusader Industries",
    "drak": "Drake Interplanetary", "espr": "Esperia", "krig": "Kruger Intergalactic",
    "misc": "Musashi Industrial & Starflight Concern", "orig": "Origin Jumpworks",
    "rsi": "Roberts Space Industries", "tmbl": "Tumbril", "xian": "Xi'an",
    "vncl": "Vanduul", "mrai": "Mirai", "grin": "Greycat Industrial", "aopoa": "Aopoa",
}
SUBDIR_DESC = {
    "interior": "Interieur", "exterior": "Exterieur", "lod": "Niveaux de detail",
    "ports": "Points d'attache",
}


PLACE_DIR_DESC = {
    "pu": "Univers persistant",
    "pu/loc": "Lieux de l'univers",
    "pu/loc/flagship": "Grandes villes",
    "pu/shops": "Boutiques",
    "pu/derelict": "Epaves",
    "pu/surfaceop": "Avant-postes de surface",
    "pu/station": "Stations spatiales",
    "pu/system": "Systemes stellaires (planetes, stations)",
    "pu/hangars": "Hangars",
    "pu/props": "Accessoires",
    "pu/asteroidcluster": "Champs d'asteroides",
    "pu/jumppoint": "Points de saut",
    "pu/racing": "Circuits de course",
    "pu/cityset_buildings": "Batiments de ville",
    "pu/hijackedships": "Vaisseaux detournes (missions)",
    "ships": "Interieurs de vaisseaux",
    "ea": "Cartes Arena Commander / Star Marine",
    "ac": "Cartes Arena Commander",
    "sm": "Cartes Star Marine",
    "setup": "Elements techniques (ascenseurs, apparition)",
    "frontend": "Menus du jeu",
}
CITY_NAMES = {"a18": "Area18 (ArcCorp)", "lorville": "Lorville (Hurston)", "newbab": "New Babbage (microTech)",
              "orison": "Orison (Crusader)", "levski": "Levski (Delamar, Nyx)"}


CITY_RX = [(name, re.compile(r"(^|[/_])(" + pat + r")([/_.0-9]|$)", re.IGNORECASE)) for name, pat in (
    ("Area18 (ArcCorp)", r"a18|area18"),
    ("Lorville (Hurston)", r"lorville|lorv"),
    ("New Babbage (microTech)", r"newbab|new_babbage|newbabbage"),
    ("Orison (Crusader)", r"orison"),
    ("Levski (Delamar, Nyx)", r"levski"),
)]
CITY_SUBGROUP = [("pu/loc", "Ville et quartiers"), ("pu/cityset_buildings", "Batiments"), ("pu/shops", "Boutiques"),
                 ("setup", "Ascenseurs, transports, apparition"), ("pu/system", "Environs")]
PLACE_CATEGORIES = [
    ("pu/station", "Stations spatiales"), ("pu/shops", "Boutiques"), ("pu/surfaceop", "Avant-postes de surface"),
    ("pu/derelict", "Epaves"), ("pu/hangars", "Hangars"), ("pu/cityset_buildings", "Batiments de ville"),
    ("pu/system", "Systemes stellaires (planetes, lunes)"), ("pu/asteroidcluster", "Champs d'asteroides"),
    ("pu/jumppoint", "Points de saut"), ("pu/racing", "Circuits de course"), ("pu/loc", "Autres lieux"),
    ("pu", "Univers persistant (divers)"), ("ships", "Interieurs de vaisseaux"),
    ("ea", "Cartes Arena Commander / Star Marine"), ("ac", "Cartes Arena Commander / Star Marine"),
    ("sm", "Cartes Arena Commander / Star Marine"), ("setup", "Elements techniques"),
]
PLACE_ORDER = ["Villes"] + list(dict.fromkeys(label for _p, label in PLACE_CATEGORIES)) + ["Divers"]
CITY_ORDER = [name for name, _rx in CITY_RX]


def place_display_parts(path):
    """Chemin d'affichage d'un conteneur : Villes > ville > type > ... ou categorie > ..."""
    rel = path.split("/")[2:]  # sans Data/ObjectContainers
    low = [c.lower() for c in rel]
    lowpath = "/".join(low)
    for name, rx in CITY_RX:
        if rx.search(lowpath):
            sub = next((label for pre, label in CITY_SUBGROUP if lowpath.startswith(pre + "/")), "Divers")
            folders = rel[:-1]
            idx = next((i for i, c in enumerate(folders) if rx.search(c)), None)
            rest = folders[idx + 1:] if idx is not None else []
            return ["Villes", name, sub, *rest, rel[-1]]
    for prefix, label in PLACE_CATEGORIES:
        pp = prefix.split("/")
        if low[:len(pp)] == pp:
            return [label, *rel[len(pp):]]
    return ["Divers", *rel]


def place_sort_key(display):
    top = PLACE_ORDER.index(display[0]) if display[0] in PLACE_ORDER else len(PLACE_ORDER)
    city = sub = 0
    if display[0] == "Villes":
        city = CITY_ORDER.index(display[1]) if display[1] in CITY_ORDER else len(CITY_ORDER)
        subs = [label for _p, label in CITY_SUBGROUP]
        sub = subs.index(display[2]) if display[2] in subs else len(subs)
    return (top, city, sub, [c.lower() for c in display])


def place_desc(parts):
    """Description d'un dossier ou d'un conteneur, parts = chemin relatif a ObjectContainers."""
    low = [p.lower() for p in parts]
    if low[-1].endswith(".socpak"):
        stem = low[-1][:-7]
        parent = low[-2] if len(low) > 1 else ""
        heavy = stem in (parent, "global") or stem.endswith("_all") or stem == parent.split("_")[0]
        if heavy:
            return "Ensemble complet du lieu : tres lourd"
        tags = []
        if "_int" in stem:
            tags.append("Interieur")
        if "_ext" in stem:
            tags.append("Exterieur")
        if "setup" in stem:
            tags.append("Technique (peu de geometrie)")
        city = next((CITY_NAMES[k] for k in CITY_NAMES if k in low[:-1]), None)
        if city:
            tags.insert(0, city)
        return ", ".join(tags)
    key = "/".join(low)
    if key in PLACE_DIR_DESC:
        return PLACE_DIR_DESC[key]
    for k, v in CITY_NAMES.items():
        if low[-1].startswith(k):
            return "Ville : " + v
    return ""


class Node:
    __slots__ = ("name", "path", "parent", "kids", "is_asset", "infos", "exts",
                 "nfiles", "size", "selfiles", "selsize", "hit")

    def __init__(self, name, path, parent, is_asset=False):
        self.name, self.path, self.parent = name, path, parent
        self.kids = {}
        self.is_asset = is_asset
        self.infos = []
        self.exts = set()
        self.nfiles = self.size = self.selfiles = self.selsize = 0
        self.hit = 0

    def describe(self):
        if self.is_asset:
            main = next((e for e in ("cdf", "cga", "cgf", "skin", "chr") if e in self.exts), None)
            kind = KIND.get(main, "Donnees de geometrie seules")
            return f"{kind} [{', '.join(sorted(self.exts))}]"
        low = self.path.lower()
        if low in DIR_DESC:
            return DIR_DESC[low]
        parts = low.split("/")
        if len(parts) >= 5 and parts[:4] == ["data", "objects", "spaceships", "ships"]:
            mfr = MANUFACTURERS.get(parts[4], parts[4].upper())
            if len(parts) == 5:
                return f"Constructeur : {mfr}"
            if len(parts) == 6:
                return f"Vaisseau ({mfr})"
        return SUBDIR_DESC.get(parts[-1], "")


def human(n):
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unit == "To":
            return f"{n:.0f} {unit}" if unit == "o" else f"{n:.1f} {unit}"
        n /= 1024


def build_model(p4k):
    root = Node("", "", None)
    for info in p4k.filelist:
        fn = info.filename
        if fn.endswith("/"):
            continue
        name = fn.rsplit("/", 1)[-1]
        if "." not in name:
            continue
        ext = name.rsplit(".", 1)[-1].lower()
        if ext not in MODEL_EXT:
            continue
        base = name[: -len(ext) - 1]
        parts = fn.split("/")[:-1]
        node, path = root, ""
        for d in parts:
            path = f"{path}/{d}" if path else d
            key = d.lower()
            nxt = node.kids.get(key)
            if nxt is None:
                nxt = node.kids[key] = Node(d, path, node)
            node = nxt
        akey = "\0" + base.lower()  # evite toute collision avec un dossier du meme nom
        a = node.kids.get(akey)
        if a is None:
            a = node.kids[akey] = Node(base, f"{path}/{base}", node, True)
        a.infos.append(info)
        a.exts.add(ext)
        n = a
        while n is not None:
            n.nfiles += 1
            n.size += info.file_size
            n = n.parent
    return root


class App:
    def __init__(self, tkroot, p4k_path):
        self.tk = tkroot
        self.p4k_path = p4k_path
        self.p4k = None
        self.root_node = None
        self.iid_node = {}
        self.sort_by = "size"
        self.filter_stamp = 0
        self.filter_text = ""
        self.q = queue.Queue()
        self.cancel = False

        self.alive = True
        tkroot.title("Star Citizen - navigateur de modeles 3D")
        tkroot.protocol("WM_DELETE_WINDOW", self.on_close)
        if not tkroot.winfo_ismapped():
            tkroot.geometry("1150x760")

        pbar = ttk.Frame(tkroot, padding=(6, 6, 6, 0))
        pbar.pack(fill="x")
        ttk.Label(pbar, text="Data.p4k :").pack(side="left")
        self.p4k_var = tk.StringVar(value=str(p4k_path))
        pe = ttk.Entry(pbar, textvariable=self.p4k_var)
        pe.pack(side="left", fill="x", expand=True, padx=4)
        pe.bind("<Return>", lambda _e: self.change_p4k(self.p4k_var.get()))
        ttk.Button(pbar, text="Parcourir...", command=self.browse_p4k).pack(side="left")
        ttk.Button(pbar, text="Charger", command=lambda: self.change_p4k(self.p4k_var.get())).pack(side="left", padx=4)
        ttk.Button(pbar, text="Mises a jour", command=self.check_update).pack(side="left", padx=(12, 0))
        tkroot.title(f"SC Extractor {__version__} - modeles 3D de Star Citizen")
        threading.Thread(target=self._bg_update_check, daemon=True).start()

        top = ttk.Frame(tkroot, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Filtre :").pack(side="left")
        self.filter_var = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.filter_var, width=40)
        e.pack(side="left", padx=4)
        e.bind("<Return>", lambda _e: self.apply_filter())
        ttk.Button(top, text="Appliquer", command=self.apply_filter).pack(side="left")
        ttk.Button(top, text="Effacer", command=self.clear_filter).pack(side="left", padx=4)
        ttk.Label(top, text="(ex : aurora, drak, thruster ; Entree pour appliquer)").pack(side="left", padx=8)

        self.ship_sel = set()
        self.ship_children = {}
        self.vehicles = []
        self.nb = ttk.Notebook(tkroot)
        self.nb.pack(fill="both", expand=True, padx=6)
        self.nb.add(self.build_ship_tab(self.nb), text="  Vaisseaux  ")
        self.obj_sel = set()
        self.obj_children = {}
        self.objects = []
        self.nb.add(self.build_object_tab(self.nb), text="  Objets  ")
        self.place_sel = set()
        self.place_children = {}
        self.places = []
        self.nb.add(self.build_place_tab(self.nb), text="  Lieux et batiments  ")
        mid = ttk.Frame(self.nb)
        self.nb.add(mid, text="  Tous les fichiers  ")
        top.pack_forget()  # la barre de filtre ne concerne que l'onglet "Tous les fichiers"

        def on_tab(_e, top=top, mid=mid):
            if self.nb.select() == str(mid):
                top.pack(fill="x", before=self.nb)
            else:
                top.pack_forget()

        self.nb.bind("<<NotebookTabChanged>>", on_tab)
        cols = ("sel", "desc", "files", "size")
        self.tree = ttk.Treeview(mid, columns=cols, selectmode="browse")
        self.tree.heading("#0", text="Nom (clic = trier par nom)", command=lambda: self.set_sort("name"))
        self.tree.heading("sel", text="Sel.")
        self.tree.heading("desc", text="Correspond a")
        self.tree.heading("files", text="Fichiers")
        self.tree.heading("size", text="Taille (clic = trier)", command=lambda: self.set_sort("size"))
        self.tree.column("#0", width=420)
        self.tree.column("sel", width=50, anchor="center", stretch=False)
        self.tree.column("desc", width=380)
        self.tree.column("files", width=80, anchor="e", stretch=False)
        self.tree.column("size", width=100, anchor="e", stretch=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewOpen>>", self.on_open)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", self.on_space)

        bot = ttk.Frame(tkroot, padding=6)
        bot.pack(fill="x")
        self.sel_label = ttk.Label(bot, text="Chargement de l'index...")
        self.sel_label.pack(side="left")
        ttk.Button(bot, text="Tout decocher", command=self.clear_selection).pack(side="right")
        self.extract_btn = ttk.Button(bot, text="Extraire la selection...", command=self.extract, state="disabled")
        self.extract_btn.pack(side="right", padx=6)
        self.convert_btn = ttk.Button(bot, text="Convertir en STL...", command=self.convert, state="disabled")
        self.convert_btn.pack(side="right")
        ttk.Button(bot, text="Apercu / reparer un STL...", command=self.open_preview).pack(side="right", padx=6)
        ttk.Button(bot, text="Preparer pour l'impression...", command=self.open_wizard).pack(side="right")
        self.cancel_btn = ttk.Button(bot, text="Annuler la conversion", command=self.cancel_conversion,
                                     state="disabled")
        self.cancel_btn.pack(side="right")

        opt = ttk.LabelFrame(tkroot, text="Options STL", padding=6)
        opt.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(opt, text="Niveau de detail (0 = max) :").pack(side="left")
        self.lod_var = tk.StringVar(value="0")
        ttk.Combobox(opt, textvariable=self.lod_var, values=["0", "1", "2", "3"], width=3,
                     state="readonly").pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="Unite :").pack(side="left")
        self.unit_var = tk.StringVar(value="millimetres")
        ttk.Combobox(opt, textvariable=self.unit_var, values=["metres", "millimetres"], width=11,
                     state="readonly").pack(side="left", padx=(4, 12))
        self.zup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Axe Z vers le haut", variable=self.zup_var).pack(side="left", padx=6)
        self.noint_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Sans interieur", variable=self.noint_var).pack(side="left", padx=6)
        self.noatt_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Sans aucun accessoire", variable=self.noatt_var).pack(side="left", padx=6)

        rem = ttk.LabelFrame(tkroot, text="Retirer du vaisseau (le train ne peut pas etre mis en position rentree)",
                             padding=6)
        rem.pack(fill="x", padx=6, pady=(0, 4))
        self.remove_vars = {}
        for key, label in sc_stl.REMOVE_LABELS:
            v = tk.BooleanVar(value=False)
            ttk.Checkbutton(rem, text=label, variable=v).pack(side="left", padx=6)
            self.remove_vars[key] = v
        from sc_preview import remember
        for var, key in ((self.lod_var, "lod"), (self.unit_var, "unit"), (self.zup_var, "zup"),
                         (self.noint_var, "no_interior"), (self.noatt_var, "no_attachments")):
            remember(var, key)
        for key, v in self.remove_vars.items():
            remember(v, "remove_" + key)

        self.bar =ttk.Progressbar(tkroot, mode="determinate")
        self.bar.pack(fill="x", padx=6, pady=(0, 6))
        self.status = ttk.Label(tkroot, text="", anchor="w")
        self.status.pack(fill="x", padx=6, pady=(0, 4))

        threading.Thread(target=self.load, daemon=True).start()
        self.poll()

    # ---------- chargement ----------
    def load(self):
        try:
            n, freed = sc_stl.purge_temp()
            if n:
                self.q.put(("status", f"Nettoyage : {n} dossiers temporaires supprimes ({human(freed)})."))
        except OSError:
            pass
        try:
            self.q.put(("status", "Lecture de l'index du p4k..."))
            self.p4k = P4KFile(self.p4k_path)
            self.q.put(("status", "Construction de l'arborescence..."))
            self.root_node = build_model(self.p4k)
            self.q.put(("places", sorted((i.filename, i.file_size) for i in self.p4k.filelist
                                         if i.filename.lower().endswith(".socpak"))))
            self.q.put(("loaded", None))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("error", f"Impossible de lire {self.p4k_path}\n{exc}"))
            return
        try:
            self.q.put(("status", "Lecture de la liste des vaisseaux..."))
            self.emap = sc_stl.load_entity_map(self.p4k_path, sc_stl.cache_path(self.p4k_path, "entity_map.json"))
            vehicles = sc_stl.load_vehicles(self.p4k_path, self.emap, sc_stl.cache_path(self.p4k_path, "vehicles.json"))
            self.q.put(("ships", vehicles))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("status", f"Liste des vaisseaux indisponible : {exc}"))
            return
        try:
            self.q.put(("status", "Lecture de la liste des objets et de leurs noms..."))
            objects = sc_stl.load_objects(self.p4k_path, self.p4k, self.emap, sc_stl.cache_path(self.p4k_path, "objects.json"))
            self.q.put(("objects", objects))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("status", f"Liste des objets indisponible : {exc}"))

    # ---------- onglet objets ----------
    OBJ_MAX_ROWS = 3000

    def build_object_tab(self, parent):
        f = ttk.Frame(parent)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="Rechercher :").pack(side="left")
        self.obj_filter = tk.StringVar()
        ttk.Entry(top, textvariable=self.obj_filter, width=30).pack(side="left", padx=4)
        self._obj_after = None
        self.obj_filter.trace_add("write", lambda *_a: self._debounce_objects())
        ttk.Label(top, text="Nom du jeu ou nom technique (ex : medpen, P4-AR, casque, multi-tool).").pack(
            side="left", padx=8)
        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True)
        self.objtree = ttk.Treeview(mid, columns=("sel", "ent", "geom"), selectmode="browse")
        self.objtree.heading("#0", text="Categorie / nom dans le jeu")
        self.objtree.heading("sel", text="Sel.")
        self.objtree.heading("ent", text="Nom technique")
        self.objtree.heading("geom", text="Fichier du modele")
        self.objtree.column("#0", width=360)
        self.objtree.column("sel", width=50, anchor="center", stretch=False)
        self.objtree.column("ent", width=280)
        self.objtree.column("geom", width=420)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.objtree.yview)
        self.objtree.configure(yscrollcommand=sb.set)
        self.objtree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.objtree.insert("", "end", text="Chargement de la liste des objets...")
        self.objtree.bind("<Button-1>", self.on_obj_click)
        self.objtree.bind("<space>", lambda _e: self.toggle_obj(self.objtree.focus()))
        self.objtree.bind("<<TreeviewOpen>>", self.on_obj_open)
        return f

    def _debounce_objects(self):
        if self._obj_after:
            self.tk.after_cancel(self._obj_after)
        self._obj_after = self.tk.after(350, self.fill_objects)

    def fill_objects(self):
        if not self.objects:
            return
        text = self.obj_filter.get().strip().lower()
        t = self.objtree
        t.delete(*t.get_children())
        self.obj_children = {}
        self.obj_rows = {}  # categorie -> lignes (affichage paresseux)
        for row in self.objects:
            ent, name, cat, geom = row
            if text and text not in name.lower() and text not in ent.lower() and text not in geom:
                continue
            self.obj_rows.setdefault(cat, []).append(row)
        total = sum(len(v) for v in self.obj_rows.values())
        open_all = bool(text) and total <= 400
        for cat in sorted(self.obj_rows, key=lambda c: (-len(self.obj_rows[c]), c)):
            rows = self.obj_rows[cat]
            cid = "c:" + cat
            t.insert("", "end", iid=cid, text=cat, values=("", f"{len(rows)} objets", ""), open=open_all)
            self.obj_children[cid] = [r[0] for r in rows]
            if open_all:
                self._populate_obj(cid)
            else:
                t.insert(cid, "end", iid=cid + "#dummy", text="")
        self.refresh_obj_boxes()

    def _populate_obj(self, cid):
        t = self.objtree
        kids = t.get_children(cid)
        if kids and kids[0].endswith("#dummy"):
            t.delete(kids[0])
        if t.get_children(cid):
            return
        rows = self.obj_rows.get(cid[2:], [])
        for ent, name, _cat, geom in rows[:self.OBJ_MAX_ROWS]:
            eid = "o:" + ent
            if not t.exists(eid):
                t.insert(cid, "end", iid=eid, text=name, values=("", ent, geom))
                self.obj_children[eid] = [ent]
        if len(rows) > self.OBJ_MAX_ROWS:
            t.insert(cid, "end", text=f"... {len(rows) - self.OBJ_MAX_ROWS} autres (utilisez la recherche)")
        self.refresh_obj_boxes()

    def on_obj_open(self, _e):
        iid = self.objtree.focus()
        if iid.startswith("c:"):
            self._populate_obj(iid)

    def refresh_obj_boxes(self):
        t = self.objtree
        for iid, ents in self.obj_children.items():
            if t.exists(iid):
                n = sum(1 for e in ents if e in self.obj_sel)
                t.set(iid, "sel", "[ ]" if n == 0 else ("[x]" if n == len(ents) else "[-]"))
        self.update_totals()

    def on_obj_click(self, ev):
        t = self.objtree
        if t.identify_region(ev.x, ev.y) == "cell" and t.identify_column(ev.x) == "#1":
            self.toggle_obj(t.identify_row(ev.y))
            return "break"

    def toggle_obj(self, iid):
        ents = self.obj_children.get(iid)
        if not ents:
            return
        if all(e in self.obj_sel for e in ents):
            self.obj_sel.difference_update(ents)
        else:
            self.obj_sel.update(ents)
        self.refresh_obj_boxes()

    # ---------- onglet lieux ----------
    def build_place_tab(self, parent):
        f = ttk.Frame(parent)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="Rechercher :").pack(side="left")
        self.place_filter = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.place_filter, width=30)
        e.pack(side="left", padx=4)
        self._place_after = None
        self.place_filter.trace_add("write", lambda *_a: self._debounce_places())
        ttk.Label(top, text="(ex : lorville, a18, orison, hospital, bar). Evitez les ensembles complets, "
                            "tres lourds.").pack(side="left", padx=8)
        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True)
        self.placetree = ttk.Treeview(mid, columns=("sel", "desc", "size"), selectmode="browse")
        self.placetree.heading("#0", text="Dossier / conteneur")
        self.placetree.heading("sel", text="Sel.")
        self.placetree.heading("desc", text="Correspond a")
        self.placetree.heading("size", text="Taille du conteneur")
        self.placetree.column("#0", width=420)
        self.placetree.column("sel", width=50, anchor="center", stretch=False)
        self.placetree.column("desc", width=420)
        self.placetree.column("size", width=130, anchor="e", stretch=False)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.placetree.yview)
        self.placetree.configure(yscrollcommand=sb.set)
        self.placetree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.placetree.insert("", "end", text="Chargement de la liste des lieux...")
        self.placetree.bind("<Button-1>", self.on_place_click)
        self.placetree.bind("<space>", lambda _e: self.toggle_place(self.placetree.focus()))
        return f

    def _debounce_places(self):
        if self._place_after:
            self.tk.after_cancel(self._place_after)
        self._place_after = self.tk.after(350, self.fill_places)

    def fill_places(self):
        if not self.places:
            return
        text = self.place_filter.get().strip().lower()
        tree = self.placetree
        tree.delete(*tree.get_children())
        self.place_children = {}
        sizes = {}
        hits = [(p, s, d) for p, s, d in self.places
                if not text or text in p.lower() or text in " ".join(d).lower()]
        open_all = bool(text) and len(hits) <= 400
        for path, size, parts in hits:
            parent = ""
            for depth in range(len(parts) - 1):
                fid = "d:" + "/".join(parts[:depth + 1]).lower()
                if not tree.exists(fid):
                    # la categorie "Villes" est ouverte d'emblee pour montrer les villes
                    is_open = open_all or (depth == 0 and parts[0] == "Villes")
                    tree.insert(parent, "end", iid=fid, text=parts[depth], open=is_open, values=("", "", ""))
                    self.place_children[fid] = []
                self.place_children[fid].append(path)
                sizes[fid] = sizes.get(fid, 0) + size
                parent = fid
            lid = "p:" + path
            tree.insert(parent, "end", iid=lid, text=parts[-1],
                        values=("", place_desc(path.split("/")[2:]), human(size)))
            self.place_children[lid] = [path]
        for fid, s in sizes.items():
            n = len(self.place_children[fid])
            tree.set(fid, "size", human(s))
            tree.set(fid, "desc", f"{n} conteneur{'s' if n > 1 else ''}")
        self.refresh_place_boxes()

    def refresh_place_boxes(self):
        for iid, paths in self.place_children.items():
            n = sum(1 for p in paths if p in self.place_sel)
            self.placetree.set(iid, "sel", "[ ]" if n == 0 else ("[x]" if n == len(paths) else "[-]"))
        self.update_totals()

    def on_place_click(self, ev):
        t = self.placetree
        if t.identify_region(ev.x, ev.y) == "cell" and t.identify_column(ev.x) == "#1":
            self.toggle_place(t.identify_row(ev.y))
            return "break"

    def toggle_place(self, iid):
        paths = self.place_children.get(iid)
        if not paths:
            return
        if all(p in self.place_sel for p in paths):
            self.place_sel.difference_update(paths)
        else:
            self.place_sel.update(paths)
        self.refresh_place_boxes()

    # ---------- onglet vaisseaux ----------
    def build_ship_tab(self, parent):
        f = ttk.Frame(parent)
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="Rechercher :").pack(side="left")
        self.ship_filter = tk.StringVar()
        ttk.Entry(top, textvariable=self.ship_filter, width=30).pack(side="left", padx=4)
        self.ship_filter.trace_add("write", lambda *_a: self.fill_ships())
        ttk.Label(top, text="Cochez une variante, ou un modele pour toutes ses variantes.").pack(side="left", padx=8)
        mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True)
        self.ships = ttk.Treeview(mid, columns=("sel", "geom"), selectmode="browse")
        self.ships.heading("#0", text="Constructeur / modele / variante")
        self.ships.heading("sel", text="Sel.")
        self.ships.heading("geom", text="Fichier du modele")
        self.ships.column("#0", width=420)
        self.ships.column("sel", width=50, anchor="center", stretch=False)
        self.ships.column("geom", width=560)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.ships.yview)
        self.ships.configure(yscrollcommand=sb.set)
        self.ships.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.ships.insert("", "end", text="Chargement de la liste des vaisseaux...")
        self.ships.bind("<Button-1>", self.on_ship_click)
        self.ships.bind("<space>", lambda _e: self.toggle_ship(self.ships.focus()))
        return f

    def fill_ships(self):
        if not self.vehicles:
            return
        text = self.ship_filter.get().strip().lower()
        self.ships.delete(*self.ships.get_children())
        self.ship_children = {}
        groups = {}
        for ent, geom in self.vehicles:
            if text and text not in ent.lower() and text not in geom:
                continue
            mfr = ent.split("_", 1)[0].lower()
            model = geom.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            groups.setdefault(mfr, {}).setdefault((model, geom), []).append(ent)
        for mfr in sorted(groups):
            fid = f"f:{mfr}"
            self.ships.insert("", "end", iid=fid, text=MANUFACTURERS.get(mfr, mfr.upper()), open=bool(text))
            self.ship_children[fid] = []
            for (model, geom), ents in sorted(groups[mfr].items()):
                mid = f"m:{geom}"
                self.ships.insert(fid, "end", iid=mid, text=model, values=("", geom), open=bool(text))
                self.ship_children[mid] = ents
                self.ship_children[fid] += ents
                for ent in ents:
                    eid = f"e:{ent}"
                    self.ships.insert(mid, "end", iid=eid, text=ent, values=("", ""))
                    self.ship_children[eid] = [ent]
        self.refresh_ship_boxes()

    def refresh_ship_boxes(self):
        for iid, ents in self.ship_children.items():
            n = sum(1 for e in ents if e in self.ship_sel)
            self.ships.set(iid, "sel", "[ ]" if n == 0 else ("[x]" if n == len(ents) else "[-]"))
        self.update_totals()

    def on_ship_click(self, ev):
        if self.ships.identify_region(ev.x, ev.y) == "cell" and self.ships.identify_column(ev.x) == "#1":
            self.toggle_ship(self.ships.identify_row(ev.y))
            return "break"

    def toggle_ship(self, iid):
        ents = self.ship_children.get(iid)
        if not ents:
            return
        if all(e in self.ship_sel for e in ents):
            self.ship_sel.difference_update(ents)
        else:
            self.ship_sel.update(ents)
        self.refresh_ship_boxes()

    def poll(self):
        if not self.alive:  # cet outil a ete remplace par un rechargement sur un autre Data.p4k
            return
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "status":
                    self.status.config(text=val)
                elif kind == "error":
                    messagebox.showerror("Erreur", val)
                    self.status.config(text="Erreur")
                elif kind == "loaded":
                    sc_stl.save_config(p4k=str(self.p4k_path))  # memorise pour le prochain lancement
                    self.status.config(text=f"{len(self.p4k.filelist):,} entrees dans le p4k, "
                                            f"{self.root_node.nfiles:,} fichiers de modeles.".replace(",", " "))
                    self.extract_btn.config(state="normal")
                    self.convert_btn.config(state="normal")
                    self.refresh()
                elif kind == "progress":
                    done, total, msg = val
                    self.bar.config(maximum=max(total, 1), value=done)
                    self.status.config(text=f"{done}/{total}  {msg}")
                elif kind == "apply_update":
                    sc_stl.stop_all()
                    sc_update.apply_update(val)  # remplace l'exe et relance : ne revient pas
                elif kind == "objects":
                    self.objects = val
                    self.fill_objects()
                    self.status.config(text=f"{len(val)} objets disponibles.")
                elif kind == "places":
                    rows = [(p, s, place_display_parts(p)) for p, s in val]
                    rows.sort(key=lambda r: place_sort_key(r[2]))
                    self.places = rows
                    self.fill_places()
                elif kind == "ships":
                    self.vehicles = val
                    self.fill_ships()
                    self.status.config(text=f"{len(val)} vaisseaux et vehicules disponibles.")
                elif kind == "preview":
                    sc_preview.PreviewWindow(self.tk, val)
                elif kind == "done":
                    self.extract_btn.config(state="normal")
                    self.convert_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    messagebox.showinfo("Extraction", val)
        except queue.Empty:
            pass
        self.tk.after(100, self.poll)

    # ---------- affichage ----------
    def visible_kids(self, node):
        kids = list(node.kids.values())
        if self.filter_text:
            kids = [k for k in kids if k.hit == self.filter_stamp]
        if self.sort_by == "size":
            kids.sort(key=lambda k: -k.size)
        else:
            kids.sort(key=lambda k: (k.is_asset, k.name.lower()))
        return kids

    def box(self, n):
        if n.selfiles == 0:
            return "[ ]"
        return "[x]" if n.selfiles == n.nfiles else "[-]"

    def label_chain(self, n):
        parts = [n.name]
        while not n.is_asset and len(n.kids) == 1:
            k = next(iter(n.kids.values()))
            if k.is_asset:
                break
            parts.append(k.name)
            n = k
        return "/".join(parts), n

    def insert_node(self, parent_iid, n):
        label, deep = self.label_chain(n)
        iid = self.tree.insert(parent_iid, "end", text=label, values=self.row_values(deep))
        self.iid_node[iid] = deep
        if not deep.is_asset:
            self.tree.insert(iid, "end", iid=iid + "#dummy", text="")
        return iid

    def row_values(self, n):
        return (self.box(n), n.describe(), f"{n.nfiles}", human(n.size))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.iid_node.clear()
        for k in self.visible_kids(self.root_node):
            self.insert_node("", k)
        self.update_totals()

    def populate(self, iid):
        kids = self.tree.get_children(iid)
        if kids and kids[0].endswith("#dummy"):
            self.tree.delete(kids[0])
            node = self.iid_node[iid]
            vk = self.visible_kids(node)
            for k in vk[:MAX_CHILDREN]:
                self.insert_node(iid, k)
            if len(vk) > MAX_CHILDREN:
                self.tree.insert(iid, "end", text=f"... {len(vk) - MAX_CHILDREN} autres elements (utilisez le filtre)")

    def on_open(self, _e):
        iid = self.tree.focus()
        if iid in self.iid_node:
            self.populate(iid)

    def set_sort(self, key):
        self.sort_by = key
        self.refresh()

    def apply_filter(self):
        text = self.filter_var.get().strip().lower()
        self.filter_text = text
        if text:
            self.status.config(text="Filtrage...")
            self.tk.update_idletasks()
            self.filter_stamp += 1
            stamp = self.filter_stamp
            stack = [self.root_node]
            hits = 0
            while stack:
                n = stack.pop()
                for k in n.kids.values():
                    if k.is_asset:
                        if text in k.path.lower():
                            hits += 1
                            p = k
                            while p is not None and p.hit != stamp:
                                p.hit = stamp
                                p = p.parent
                    else:
                        stack.append(k)
            self.status.config(text=f"{hits} modeles correspondent a '{text}'.")
        self.refresh()
        if text:  # deplie automatiquement les premiers niveaux
            for iid in self.tree.get_children(""):
                self.expand_some(iid, depth=6)

    def expand_some(self, iid, depth):
        if depth == 0 or iid not in self.iid_node or self.iid_node[iid].is_asset:
            return
        self.populate(iid)
        self.tree.item(iid, open=True)
        kids = self.tree.get_children(iid)
        if len(kids) <= 6:
            for k in kids:
                self.expand_some(k, depth - 1)

    def clear_filter(self):
        self.filter_var.set("")
        self.apply_filter()

    # ---------- selection ----------
    def on_click(self, ev):
        if self.tree.identify_region(ev.x, ev.y) == "cell" and self.tree.identify_column(ev.x) == "#1":
            iid = self.tree.identify_row(ev.y)
            if iid in self.iid_node:
                self.toggle(iid)
            return "break"

    def on_space(self, _e):
        iid = self.tree.focus()
        if iid in self.iid_node:
            self.toggle(iid)

    def toggle(self, iid):
        node = self.iid_node[iid]
        self.set_selected(node, node.selfiles != node.nfiles)
        self.refresh_visible_rows()
        self.update_totals()

    def set_selected(self, node, on):
        stack = [node]
        while stack:
            n = stack.pop()
            if n.is_asset:
                self.set_asset(n, on)
            else:
                for k in n.kids.values():
                    if self.filter_text and k.hit != self.filter_stamp:
                        continue
                    stack.append(k)

    def set_asset(self, a, on):
        tf = a.nfiles if on else 0
        ts = a.size if on else 0
        df, ds = tf - a.selfiles, ts - a.selsize
        if not df and not ds:
            return
        n = a
        while n is not None:
            n.selfiles += df
            n.selsize += ds
            n = n.parent

    def refresh_visible_rows(self):
        for iid, n in self.iid_node.items():
            if self.tree.exists(iid):
                self.tree.set(iid, "sel", self.box(n))

    def clear_selection(self):
        """Decoche tout, dans les quatre onglets (vaisseaux, objets, lieux, fichiers)."""
        self.ship_sel.clear()
        self.obj_sel.clear()
        self.place_sel.clear()
        if self.ship_children:
            self.refresh_ship_boxes()
        if self.obj_children:
            self.refresh_obj_boxes()
        if self.place_children:
            self.refresh_place_boxes()
        if self.root_node:
            saved, self.filter_text = self.filter_text, ""
            self.set_selected(self.root_node, False)
            self.filter_text = saved
            self.refresh_visible_rows()
        self.update_totals()

    def update_totals(self):
        r = self.root_node
        txt = (f"Vaisseaux coches : {len(self.ship_sel)}   |   Objets coches : {len(getattr(self, 'obj_sel', ()))}"
               f"   |   Lieux coches : {len(getattr(self, 'place_sel', ()))}")
        if r:
            txt += f"   |   Fichiers coches : {r.selfiles} ({human(r.selsize)})"
        self.sel_label.config(text=txt)

    # ---------- extraction ----------
    def selected_infos(self):
        infos, stack = [], [self.root_node]
        while stack:
            n = stack.pop()
            if n.selfiles == 0:
                continue
            if n.is_asset:
                infos.extend(n.infos)
            else:
                stack.extend(n.kids.values())
        return infos

    def extract(self):
        infos = self.selected_infos()
        if not infos:
            messagebox.showinfo("Extraction", "Rien n'est coche.")
            return
        out = filedialog.askdirectory(title="Dossier de destination")
        if not out:
            return
        self.extract_btn.config(state="disabled")
        threading.Thread(target=self.run_extract, args=(infos, out), daemon=True).start()

    def cancel_conversion(self):
        self.cancel = True
        self.cancel_btn.config(state="disabled")
        n = sc_stl.stop_group("convert")  # arrete aussi les conversions deja lancees
        self.status.config(text=f"Conversion annulee ({n} processus arrete(s)).")

    # ---------- mises a jour ----------
    def _bg_update_check(self):
        """Au demarrage, sans rien bloquer : signale une nouvelle version dans la barre d'etat."""
        try:
            info = sc_update.latest_release()
        except sc_stl.ConvertError:
            return
        if sc_update.is_newer(info["version"]):
            self.q.put(("status", f"Nouvelle version {info['version']} disponible : bouton 'Mises a jour'."))

    def check_update(self):
        try:
            info = sc_update.latest_release()
        except sc_stl.ConvertError as exc:
            messagebox.showinfo("Mises a jour", f"Verification impossible : {exc}")
            return
        if not sc_update.is_newer(info["version"]):
            messagebox.showinfo("Mises a jour", f"Vous avez la derniere version ({__version__}).")
            return
        notes = ("\n\nNouveautes :\n" + info["notes"][:800]) if info["notes"] else ""
        if not sc_stl.FROZEN:
            messagebox.showinfo("Mises a jour", f"Version {info['version']} disponible.{notes}\n\n"
                                               "(Mode developpement : mettez a jour avec git.)")
            return
        if not messagebox.askyesno("Mises a jour", f"Version {info['version']} disponible (vous avez "
                                                   f"{__version__}).{notes}\n\nTelecharger et redemarrer ?"):
            return
        self.status.config(text="Telechargement de la mise a jour...")

        def work():
            try:
                path = sc_update.download_update(info, lambda d, t: self.q.put(
                    ("status", f"Mise a jour : {d / 1e6:.0f} / {t / 1e6:.0f} Mo")))
                self.q.put(("apply_update", str(path)))
            except sc_stl.ConvertError as exc:
                self.q.put(("error", f"Mise a jour impossible : {exc}"))
        threading.Thread(target=work, daemon=True).start()

    def browse_p4k(self):
        cur = Path(self.p4k_var.get())
        f = filedialog.askopenfilename(title="Choisir Data.p4k", filetypes=[("Archive Star Citizen", "*.p4k")],
                                       initialdir=str(cur.parent) if cur.parent.is_dir() else None)
        if f:
            self.p4k_var.set(os.path.normpath(f))
            self.change_p4k(f)

    def change_p4k(self, path):
        """Recharge tout l'outil sur un autre Data.p4k (ex. LIVE -> PTU)."""
        path = os.path.normpath(path.strip().strip('"'))
        if os.path.isdir(path):
            path = os.path.join(path, "Data.p4k")  # le dossier du jeu suffit
        if not os.path.isfile(path) or not path.lower().endswith(".p4k"):
            messagebox.showerror("Data.p4k", f"Fichier introuvable ou pas un .p4k :\n{path}")
            return
        if os.path.normcase(path) == os.path.normcase(str(self.p4k_path)) and self.root_node is not None:
            return  # deja charge
        sc_stl.stop_all()
        self.alive = False
        for w in self.tk.winfo_children():
            w.destroy()
        App(self.tk, path)

    def on_close(self):
        """Fermeture de l'outil : arrete toutes les conversions, reparations et rendus en cours."""
        self.cancel = True
        sc_stl.stop_all()
        self.tk.destroy()

    def open_wizard(self):
        """Assistant d'impression sur l'unique element coche (vaisseau, objet, lieu ou fichier)."""
        import sc_wizard
        if self.root_node is None or not getattr(self, "emap", None):
            messagebox.showinfo("Assistant", "Attendez la fin du chargement.")
            return
        names = {e: n for e, n, _c, _g in self.objects}
        items = [("entity", e, names.get(e, e)) for e in sorted(self.ship_sel | self.obj_sel)]
        items += [("place", p, p.rsplit("/", 1)[-1]) for p in sorted(self.place_sel)]
        items += [("asset", a, a.name) for a in _selected_assets(self.root_node)]
        if len(items) != 1:
            messagebox.showinfo("Assistant", f"Cochez un seul element (vaisseau, objet, lieu ou fichier) : "
                                             f"{len(items)} sont coches.")
            return
        sc_wizard.PrintWizard(self, items[0])

    def open_preview(self):
        files = filedialog.askopenfilenames(title="Choisir des STL", filetypes=[("STL", "*.stl")])
        if files:
            sc_preview.PreviewWindow(self.tk, list(files))

    def convert(self):
        assets = _selected_assets(self.root_node)
        entities = sorted(self.ship_sel | self.obj_sel)
        places = sorted(self.place_sel)
        if not assets and not entities and not places:
            messagebox.showinfo("Conversion", "Rien n'est coche.")
            return
        if not sc_stl.SB.is_file():
            messagebox.showerror("Conversion", f"StarBreaker introuvable : {sc_stl.SB}")
            return
        heavy = [p for p in places if place_desc(p.split("/")[2:]).startswith("Ensemble complet")]
        if heavy and not messagebox.askyesno(
                "Conversion", f"{len(heavy)} lieu(x) coche(s) sont des ensembles complets (ex. "
                              f"{heavy[0].rsplit('/', 1)[-1]}). Ils peuvent produire des dizaines de millions de "
                              f"faces (Lorville entier : environ 50 millions). Continuer ?"):
            return
        total = len(assets) + len(entities) + len(places)
        if total > 50 and not messagebox.askyesno(
                "Conversion", f"{total} elements sont coches. Chaque conversion produit un STL. Continuer ?"):
            return
        out = filedialog.askdirectory(title="Dossier de destination des STL")
        if not out:
            return
        opts = {
            "lod": int(self.lod_var.get()),
            "scale": 1000.0 if self.unit_var.get() == "millimetres" else 1.0,
            "z_up": self.zup_var.get(),
            "no_interior": self.noint_var.get(),
            "no_att": self.noatt_var.get(),
            "remove": [k for k, v in self.remove_vars.items() if v.get()],
        }
        self.extract_btn.config(state="disabled")
        self.convert_btn.config(state="disabled")
        self.cancel = False
        self.cancel_btn.config(state="normal")
        threading.Thread(target=_convert_worker, args=(self, assets, out, opts, entities, places),
                         daemon=True).start()

    def run_extract(self, infos, out):
        total, errors = len(infos), []
        for i, info in enumerate(infos, 1):
            try:
                self.p4k.extractall(path=out, members=[info], monitor=None)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{info.filename}: {exc}")
            if i % 20 == 0 or i == total:
                self.q.put(("progress", (i, total, info.filename)))
        msg = f"{total - len(errors)}/{total} fichiers extraits dans\n{out}"
        if errors:
            msg += f"\n\n{len(errors)} erreurs, premiere : {errors[0]}"
        self.q.put(("done", msg))


def _selected_assets(root):
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        if n.selfiles == 0:
            continue
        if n.is_asset:
            out.append(n)
        else:
            stack.extend(n.kids.values())
    return out


PARALLEL_JOBS = 3  # conversions simultanees (chaque job lance StarBreaker puis Blender)


def _convert_worker(app, assets, out, opts, entities=(), places=()):
    """Corps du thread de conversion (fonction separee pour rester testable)."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    tmp = tempfile.mkdtemp(prefix="sc_glb_")
    ok, errors, made = 0, [], []
    try:
        emap = sc_stl.load_entity_map(app.p4k_path, sc_stl.cache_path(app.p4k_path, "entity_map.json"))
    except Exception as exc:  # noqa: BLE001
        app.q.put(("done", f"Impossible de lire la base de donnees du jeu : {exc}"))
        return
    # ignore les niveaux de detail (_lodN) et ne convertit qu'une fois chaque entite
    skipped, seen, todo = 0, set(entities), []
    for a in assets:
        if re.search(r"_lod\d+$", a.name, re.IGNORECASE):
            skipped += 1
            continue
        ent = sc_stl.entity_for_asset(emap, a.path, a.exts)
        if ent:
            if ent in seen:
                skipped += 1
                continue
            seen.add(ent)
        todo.append(a)
    common = dict(lod=opts["lod"], scale=opts["scale"], z_up=opts["z_up"],
                  no_interior=opts["no_interior"], no_attachments=opts["no_att"],
                  remove=tuple(opts.get("remove", ())))
    jobs = [(e, lambda e=e: sc_stl.convert_entity(e, app.p4k_path, out, tmp, **common)) for e in entities]
    jobs += [(p.rsplit("/", 1)[-1], lambda p=p: sc_stl.convert_socpak(p, app.p4k_path, out, tmp, **common))
             for p in places]
    jobs += [(a.name, lambda a=a: sc_stl.convert_asset(a.path, a.exts, emap, app.p4k_path, out, tmp, **common))
             for a in todo]
    total, done, cancelled = len(jobs), 0, False
    app.q.put(("progress", (0, total, f"Conversion de {total} elements ({PARALLEL_JOBS} en parallele)...")))
    def in_group(fn):
        if getattr(app, "cancel", False):
            raise sc_stl.ConvertError("annule")
        sc_stl.set_group("convert")  # le bouton Annuler et la fermeture arretent ces processus
        return fn()

    with ThreadPoolExecutor(max_workers=PARALLEL_JOBS) as pool:
        futures = {pool.submit(in_group, fn): name for name, fn in jobs}
        for fut in as_completed(futures):
            name = futures[fut]
            done += 1
            if fut.cancelled():
                continue
            try:
                stl, _info, _meth = fut.result()
                ok += 1
                made.append(str(stl))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {exc}")
            app.q.put(("progress", (done, total, f"{name} termine")))
            if getattr(app, "cancel", False) and not cancelled:
                cancelled = True
                for f in futures:
                    f.cancel()
    assets = jobs
    app.q.put(("progress", (total, total, "termine")))
    if made:
        app.q.put(("preview", made))
    msg = f"{ok}/{len(assets)} modeles convertis en STL dans\n{out}"
    if skipped:
        msg += f"\n{skipped} ignores (niveaux de detail ou doublons d'entite)."
    if cancelled:
        msg += "\nConversion annulee."
    if errors:
        msg += "\n\nEchecs :\n" + "\n".join(errors[:8]) + (f"\n... (+{len(errors) - 8})" if len(errors) > 8 else "")
    shutil.rmtree(tmp, ignore_errors=True)  # GLB intermediaires : inutiles une fois les STL ecrits
    app.q.put(("done", msg))


def selftest(path):
    p4k = P4KFile(path)
    root = build_model(p4k)
    print("fichiers de modeles :", root.nfiles, "poids :", human(root.size))
    # trouve un petit asset de vaisseau
    target = None
    stack = [root]
    while stack:
        n = stack.pop()
        for k in n.kids.values():
            if k.is_asset and "orig_x1_velocity_thruster" in k.path.lower():
                target = k
            elif not k.is_asset:
                stack.append(k)
    print("asset :", target.path, sorted(target.exts), human(target.size))
    out = tempfile.mkdtemp(prefix="sc_selftest_")
    app = App.__new__(App)
    app.filter_text, app.filter_stamp = "", 0
    app.set_asset(target, True)
    print("selection racine :", root.selfiles, human(root.selsize))
    for info in target.infos:
        p4k.extractall(path=out, members=[info], monitor=None)
    for dp, _d, fs in os.walk(out):
        for f in fs:
            fp = os.path.join(dp, f)
            with open(fp, "rb") as fh:
                head = fh.read(8)
            print(os.path.getsize(fp), head, fp[len(out):])


def self_check():
    """SC_Extractor.exe --check : diagnostic complet, ecrit dans check.txt du dossier de donnees."""
    import json
    import tempfile
    import traceback
    rep = {"version": __version__, "frozen": sc_stl.FROZEN, "data_dir": str(sc_stl.DATA_DIR)}
    try:
        rep["missing_tools"] = [t["dest"] for t in sc_setup.missing_tools()]
        rep["blender"] = str(sc_stl.find_blender())
        rep["starbreaker"] = sc_stl.SB.is_file()
        rep["blender_script"] = sc_stl.BLENDER_SCRIPT.is_file()
        p4k = sc_stl.load_config().get("p4k") or find_p4k()
        rep["p4k"] = p4k
        out = tempfile.mkdtemp(prefix="sc_glb_")
        stl, info, meth = sc_stl.convert_entity("crlf_consumable_healing_01", p4k, out, out, scale=1000.0, z_up=True)
        rep["conversion"] = {"methode": meth, "faces": info["faces"], "extents_mm": info["extents"]}
        rep["ok"] = True
    except Exception:  # noqa: BLE001
        rep["ok"] = False
        rep["erreur"] = traceback.format_exc()
    (sc_stl.DATA_DIR / "check.txt").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")


def main():
    if "--check" in sys.argv:
        self_check()
        return
    if "--selftest" in sys.argv:
        args = [a for a in sys.argv[1:] if not a.startswith("--")]
        selftest(args[0] if args else P4K_DEFAULT)
        return
    # ordre de priorite : argument de la ligne de commande, dernier chemin utilise, emplacements habituels
    path = sys.argv[1] if len(sys.argv) > 1 else sc_stl.load_config().get("p4k") or find_p4k() or ""
    r = tk.Tk()
    r.withdraw()

    def start_app():
        p = path
        if not os.path.isfile(p):
            p = filedialog.askopenfilename(title="Choisir Data.p4k (dossier du jeu, ex. StarCitizen\\LIVE)",
                                           filetypes=[("Archive Star Citizen", "*.p4k")])
            if not p:
                r.destroy()
                return
        r.deiconify()
        App(r, os.path.normpath(p))

    def setup_failed(msg):
        messagebox.showerror("SC Extractor", f"Telechargement des outils impossible :\n{msg}\n\n"
                                             "Verifiez la connexion Internet puis relancez.")
        r.destroy()

    if sc_setup.missing_tools():  # premier lancement : StarBreaker et Blender a telecharger
        sc_setup.SetupDialog(r, start_app, setup_failed)
    else:
        start_app()
    r.mainloop()


def find_p4k():
    """Data.p4k dans les emplacements d'installation habituels de Star Citizen (LIVE en priorite)."""
    import string
    roots = []
    for drive in string.ascii_uppercase:
        base = f"{drive}:\\"
        if not os.path.isdir(base):
            continue
        roots += [os.path.join(base, "Program Files", "Roberts Space Industries", "StarCitizen"),
                  os.path.join(base, "Roberts Space Industries", "StarCitizen"),
                  os.path.join(base, "Jeux", "RSI", "StarCitizen"),
                  os.path.join(base, "Games", "Roberts Space Industries", "StarCitizen"),
                  os.path.join(base, "StarCitizen")]
    for root in roots:
        for channel in ("LIVE", "PTU", "EPTU", "TECH-PREVIEW"):
            p = os.path.join(root, channel, "Data.p4k")
            if os.path.isfile(p):
                return p
    return None


if __name__ == "__main__":
    main()
