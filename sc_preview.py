"""Fenetre d'apercu et de reparation des STL (rendu et reparation par Blender en headless)."""
import queue
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sc_stl

# (libelle, plus petit detail reproductible sur la piece imprimee, en mm)
PRINTERS = [("Filament (buse 0,4 mm)", 0.4), ("Filament fin (buse 0,2 mm)", 0.2), ("Resine (0,05 mm)", 0.05)]
# paroi la plus fine imprimable de facon fiable (mm sur la piece) : 2 passages de buse en filament
PRINTER_WALL = {"Filament (buse 0,4 mm)": 0.8, "Filament fin (buse 0,2 mm)": 0.5, "Resine (0,05 mm)": 0.4}
PIN_SIZE = (1.5, 4.0)  # trous d'alignement : rayon et profondeur (mm), goupilles de 2,7 x 7,5 mm
GRID_WARN = 3000  # cubes sur la plus grande dimension au-dela desquels le calcul devient tres lourd
GRID_MAX = 16000  # au-dela, la reparation est refusee ; en dessous, le garde-fou RAM protege la machine

VIEWS = [("iso", "3/4 avant"), ("front", "Avant"), ("back", "Arriere"), ("side", "Profil"), ("top", "Dessus")]


def views_data(res):
    """Resultat Blender (5 vues fixes) -> {"iso": png, "front": png, ..., "extents": [...]}."""
    views = {Path(p).stem.rsplit("_", 1)[-1]: p for p in res["images"]}
    views["extents"] = res["extents"]
    return views


def remember(var, key):
    """Relie une variable Tk a config.json : valeur restauree a l'ouverture, sauvee a chaque changement."""
    cfg = sc_stl.load_config()
    if key in cfg:
        try:
            var.set(cfg[key])
        except tk.TclError:
            pass
    var.trace_add("write", lambda *_a: _save_var(var, key))


def _save_var(var, key):
    try:
        sc_stl.save_config(**{key: var.get()})
    except tk.TclError:
        pass


class PreviewWindow:
    def __init__(self, parent, stl_paths):
        self.win = tk.Toplevel(parent)
        self.win.title("Apercu et reparation des STL")
        self.win.geometry("1150x860")
        self.paths = [str(p) for p in stl_paths]
        self.tmp = Path(tempfile.mkdtemp(prefix="sc_prev_"))
        self.cache = {}  # stl -> {view: png}
        self.photo = None
        self.q = queue.Queue()
        self.busy = False

        left = ttk.Frame(self.win, padding=6)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Fichiers STL").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=34, height=5, exportselection=False)
        self.listbox.pack(fill="x")
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self.show_selected())
        ttk.Button(left, text="Ajouter un STL...", command=self.add_file).pack(fill="x", pady=(6, 0))

        # --- impression : echelle, imprimante, plateau (sert a la reparation, a l'echelle et a la decoupe)
        imp = ttk.LabelFrame(left, text="Impression", padding=4)
        imp.pack(fill="x", pady=(8, 0))
        self.scale_n = tk.StringVar(value="100")
        self.printer = tk.StringVar(value=PRINTERS[0][0])
        self.stl_unit = tk.StringVar(value="mm")
        row = ttk.Frame(imp)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="Echelle  1 :", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.scale_n, width=7).pack(side="left")
        ttk.Label(row, text="  STL en").pack(side="left")
        ttk.Combobox(row, textvariable=self.stl_unit, values=["mm", "m"], width=4,
                     state="readonly").pack(side="left", padx=2)
        cb = ttk.Combobox(imp, textvariable=self.printer, values=[p[0] for p in PRINTERS], state="readonly")
        cb.pack(fill="x", pady=2)
        cb.bind("<<ComboboxSelected>>", lambda _e: self.wall.set(str(PRINTER_WALL[self.printer.get()])))
        self.bed = tk.StringVar(value="300")
        row = ttk.Frame(imp)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="Plateau (mm) :", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.bed, width=7).pack(side="left")
        ttk.Button(row, text="Adapter l'echelle", command=self.fit_to_bed).pack(side="left", padx=4)
        ttk.Button(imp, text="Creer le STL a cette echelle (taille d'impression)",
                   command=self.export_scaled).pack(fill="x", pady=(2, 2))
        self.shell = tk.StringVar(value="2")
        row = ttk.Frame(imp)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="Coque (mm) :", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.shell, width=7).pack(side="left")
        ttk.Button(row, text="Evider (resine)", command=self.hollow_selected).pack(side="left", padx=4)

        # --- reparation : methode precise en avant, reglages voxel ranges dans "Avance"
        rep = ttk.LabelFrame(left, text="Reparation (Blender)", padding=6)
        rep.pack(fill="x", pady=8)
        self.method = tk.StringVar(value="precise")
        mrow = ttk.Frame(rep)
        mrow.pack(fill="x")
        ttk.Radiobutton(mrow, text="Precise (conseille)", value="precise", variable=self.method,
                        command=self.sync_advanced).pack(side="left")
        ttk.Radiobutton(mrow, text="Voxel", value="voxel", variable=self.method,
                        command=self.sync_advanced).pack(side="left", padx=6)
        self.wall = tk.StringVar(value=str(PRINTER_WALL[PRINTERS[0][0]]))
        row = ttk.Frame(rep)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="Paroi mini (mm imprimes)", width=24).pack(side="left")
        ttk.Entry(row, textvariable=self.wall, width=7).pack(side="left")
        self.show_adv = tk.BooleanVar(value=False)
        ttk.Checkbutton(rep, text="Reglages avances (voxel)", variable=self.show_adv,
                        command=self.sync_advanced).pack(anchor="w", pady=(2, 0))
        self.adv = ttk.Frame(rep)
        self.weld = tk.StringVar(value="0.01")
        self.hole = tk.StringVar(value="64")
        self.voxel = tk.StringVar(value="0")
        self.island = tk.StringVar(value="0")
        self.adapt = tk.StringVar(value="0")
        for label, var in (("Soudure des sommets", self.weld), ("Fermer trous (cotes max)", self.hole),
                           ("Taille voxel (0 = non)", self.voxel), ("Simplification (0 a 1)", self.adapt),
                           ("Ilots < % du plus gros", self.island)):
            row = ttk.Frame(self.adv)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label, width=24).pack(side="left")
            ttk.Entry(row, textvariable=var, width=7).pack(side="left")
        ttk.Button(self.adv, text="Calculer le voxel (precision imprimante)", command=self.print_voxel).pack(fill="x")
        self.stl_max = tk.StringVar(value="500")
        self.probes = {}  # (stl, simplification) -> loi de prevision mesuree par essai rapide
        row = ttk.Frame(self.adv)
        row.pack(fill="x", pady=(4, 1))
        ttk.Label(row, text="STL max (Mo) :", width=12).pack(side="left")
        ttk.Entry(row, textvariable=self.stl_max, width=7).pack(side="left")
        ttk.Button(row, text="Voxel pour cette taille", command=self.size_voxel).pack(side="left", padx=4)
        self.repair_btn = ttk.Button(rep, text="Reparer la selection", command=self.repair)
        self.repair_btn.pack(fill="x", pady=(4, 0))
        rframe = ttk.Frame(left)
        rframe.pack(fill="both", expand=True)
        self.report = tk.Text(rframe, width=34, height=16, wrap="word", state="disabled")
        rsb = ttk.Scrollbar(rframe, orient="vertical", command=self.report.yview)
        self.report.configure(yscrollcommand=rsb.set)
        self.report.pack(side="left", fill="both", expand=True)
        rsb.pack(side="right", fill="y")

        right = ttk.Frame(self.win, padding=6)
        right.pack(side="left", fill="both", expand=True)
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        self.view_var = tk.StringVar(value="iso")
        for key, label in VIEWS:
            ttk.Radiobutton(bar, text=label, value=key, variable=self.view_var,
                            command=self.show_view).pack(side="left", padx=4)
        self.status = ttk.Label(bar, text="")
        self.status.pack(side="right")
        hbar = ttk.Frame(right)
        hbar.pack(fill="x", pady=(4, 0))
        self.pairs = {}  # stl repare -> (stl original, seuil)
        self.heat = {}   # stl repare -> vues de la carte des pertes
        self.heat_var = tk.BooleanVar(value=False)
        self.heat_chk = ttk.Checkbutton(hbar, text="Carte des pertes (sur l'original)", variable=self.heat_var,
                                        command=self.on_heat_toggle, state="disabled")
        self.heat_chk.pack(side="left")
        self.legend = ttk.Label(hbar, text="")
        self.legend.pack(side="left", padx=10)
        prog = ttk.Frame(right)
        prog.pack(fill="x", pady=(4, 0))
        self.pbar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.pbar.pack(side="left", fill="x", expand=True)
        self.pstep = ttk.Label(prog, text="", width=48, anchor="w")
        self.pstep.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(prog, text="Arreter", command=self.stop_task, state="disabled")
        self.stop_btn.pack(side="left")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        cut = ttk.LabelFrame(right, text="Grande maquette en plusieurs morceaux", padding=4)
        cut.pack(fill="x", pady=(6, 0))
        self.length = tk.StringVar(value="1000")
        self.vol = [tk.StringVar(value="300") for _ in range(3)]
        ttk.Label(cut, text="Longueur finale (mm) :").pack(side="left")
        ttk.Entry(cut, textvariable=self.length, width=6).pack(side="left", padx=2)
        ttk.Button(cut, text="Echelle pour cette longueur", command=self.length_scale).pack(side="left", padx=4)
        ttk.Label(cut, text="   Volume d'impression X/Y/Z (mm) :").pack(side="left")
        for v in self.vol:
            ttk.Entry(cut, textvariable=v, width=5).pack(side="left", padx=1)
        self.use_pins = tk.BooleanVar(value=True)
        ttk.Checkbutton(cut, text="Trous d'alignement", variable=self.use_pins).pack(side="left", padx=4)
        ttk.Button(cut, text="Decouper en morceaux", command=self.split_pieces).pack(side="left", padx=6)
        self.canvas = ttk.Label(right, anchor="center")
        self.canvas.pack(fill="both", expand=True)

        # reglages memorises d'une fenetre et d'un lancement a l'autre (config.json)
        for var, key in ((self.scale_n, "scale"), (self.stl_unit, "stl_unit"), (self.printer, "printer"),
                         (self.bed, "bed"), (self.wall, "wall"), (self.method, "method"), (self.adapt, "adapt"),
                         (self.stl_max, "stl_max"), (self.length, "length"), (self.weld, "weld"),
                         (self.hole, "hole"), (self.voxel, "voxel"), (self.island, "island"),
                         (self.vol[0], "vol_x"), (self.vol[1], "vol_y"), (self.vol[2], "vol_z"),
                         (self.shell, "shell"), (self.use_pins, "pins"), (self.show_adv, "show_advanced")):
            remember(var, key)
        self.sync_advanced()
        for p in self.paths:
            self.listbox.insert("end", Path(p).name)
        self.win.after(100, self.poll)
        if self.paths:
            self.listbox.selection_set(0)
            self.show_selected()

    # ---------- utilitaires ----------
    def selected(self):
        sel = self.listbox.curselection()
        return self.paths[sel[0]] if sel else None

    def set_report(self, text):
        self.report.config(state="normal")
        self.report.delete("1.0", "end")
        self.report.insert("end", text)
        self.report.config(state="disabled")

    def add_file(self):
        f = filedialog.askopenfilename(parent=self.win, filetypes=[("STL", "*.stl")])
        if f:
            self.add_path(f, select=True)

    def add_path(self, p, select=False):
        self.paths.append(str(p))
        self.listbox.insert("end", Path(p).name)
        if select:
            self.listbox.selection_clear(0, "end")
            self.listbox.selection_set("end")
            self.show_selected()

    def scale_params(self):
        """(N de l'echelle 1:N, facteur unite STL -> mm) ; leve ValueError si invalide."""
        n = float(self.scale_n.get().replace(",", "."))
        if n <= 0:
            raise ValueError
        return n, (1000.0 if self.stl_unit.get() == "m" else 1.0)

    def to_print_mm(self, v):
        """Convertit une longueur en unite du STL vers des mm sur la piece imprimee."""
        n, unit = self.scale_params()
        return v * unit / n

    def bed_mm(self):
        b = float(self.bed.get().replace(",", "."))
        if b <= 0:
            raise ValueError
        return b

    def fit_scale(self, ext, target=None):
        """Plus petite echelle 1:N 'ronde' pour que la plus grande dimension tienne dans 'target' mm
        (par defaut le plateau)."""
        import math
        unit = 1000.0 if self.stl_unit.get() == "m" else 1.0
        need = max(ext) * unit / (target or self.bed_mm())  # N minimal
        if need <= 1:
            return 1
        mag = 10 ** math.floor(math.log10(need))
        for step in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
            if step * mag >= need:
                return int(round(step * mag))
        return int(math.ceil(need))

    def length_scale(self):
        """Regle l'echelle pour que la plus grande dimension fasse au plus 'Longueur finale'."""
        stl = self.selected()
        ext = self.cache.get(stl, {}).get("extents")
        if not ext:
            self.set_report("Attendez la fin du rendu du modele.")
            return
        try:
            target = float(self.length.get().replace(",", "."))
            n = self.fit_scale(ext, target)
        except (ValueError, ZeroDivisionError):
            self.set_report("Longueur invalide.")
            return
        self.scale_n.set(str(n))
        unit = 1000.0 if self.stl_unit.get() == "m" else 1.0
        size = [round(e * unit / n) for e in ext]
        self.set_report(f"Echelle 1:{n} : la maquette fera {size[0]} x {size[1]} x {size[2]} mm.\n"
                        "Reparez d'abord le modele a taille reelle (methode Precise), puis selectionnez le "
                        "fichier _repaired et cliquez 'Decouper en morceaux'.")

    def split_pieces(self):
        stl = self.selected()
        if not stl:
            return
        try:
            n, unit = self.scale_params()
            size = [float(v.get().replace(",", ".")) for v in self.vol]
        except ValueError:
            self.set_report("Echelle ou volume d'impression invalide.")
            return
        if min(size) <= 0:
            self.set_report("Volume d'impression invalide.")
            return
        src = Path(stl)
        outdir = src.with_name(f"{src.stem}_morceaux_1-{n:g}")
        warn = "" if "_repaired" in src.stem else ("\nATTENTION : ce fichier n'est pas repare. Les morceaux "
                                                   "seront ouverts la ou le modele l'est.")
        self.set_report(f"Decoupe a l'echelle 1:{n:g} en morceaux de {size[0]:g} x {size[1]:g} x {size[2]:g} mm "
                        f"au plus...{warn}")
        pins = PIN_SIZE if self.use_pins.get() else None
        self.start_task("Decoupe en morceaux...", self._split_task, stl, str(outdir), unit / n, size, pins)

    def _split_task(self, stl, outdir, factor, size, pins=None):
        return ("split", sc_stl.split_stl(stl, outdir, factor, size, on_progress=self._prog, pins=pins))

    def export_scaled(self):
        """Ecrit une copie du STL selectionne a la taille d'impression (facteur unique sur les 3 axes)."""
        stl = self.selected()
        if not stl:
            return
        try:
            n, unit = self.scale_params()
        except ValueError:
            self.set_report("Echelle invalide.")
            return
        ext0 = self.cache.get(stl, {}).get("extents")
        try:
            bed = self.bed_mm()
        except ValueError:
            bed = 0
        if ext0 and bed and max(ext0) * unit / n > bed:
            fit = self.fit_scale(ext0)
            big = [round(e * unit / n) for e in ext0]
            ans = messagebox.askyesnocancel(
                "Echelle", f"A 1:{n:g}, la piece ferait {big[0]} x {big[1]} x {big[2]} mm : plus grand que le "
                           f"plateau de {bed:g} mm.\n\nOui : utiliser 1:{fit} (tient sur le plateau)\n"
                           f"Non : garder 1:{n:g} (si la piece sera decoupee en morceaux)\nAnnuler : ne rien faire",
                parent=self.win)
            if ans is None:
                return
            if ans:
                n = fit
                self.scale_n.set(str(fit))
        factor = unit / n  # unite du STL -> mm sur la piece
        src = Path(stl)
        dst = src.with_name(f"{src.stem}_1-{n:g}.stl")
        try:
            ext = sc_stl.scale_stl(str(src), str(dst), factor)
        except (sc_stl.ConvertError, OSError) as exc:
            self.set_report(f"Redimensionnement impossible : {exc}")
            return
        if str(dst) not in self.paths:
            self.add_path(str(dst))
        self.set_report(f"Ecrit : {dst.name}\nEchelle 1:{n:g} (facteur {factor:.6g} sur les 3 axes, proportions "
                        f"conservees)\nTaille : {ext[0]:g} x {ext[1]:g} x {ext[2]:g} mm\n\n"
                        "Ce fichier s'ouvre directement a la bonne taille dans le trancheur (100 %).\n"
                        "Pour une reparation, utilisez plutot le STL a taille reelle.")

    def fit_to_bed(self):
        stl = self.selected()
        ext = self.cache.get(stl, {}).get("extents")
        if not ext:
            self.set_report("Attendez la fin du rendu du modele.")
            return
        try:
            n = self.fit_scale(ext)
        except ValueError:
            self.set_report("Taille de plateau invalide.")
            return
        self.scale_n.set(str(n))
        self.print_voxel()

    def adapt_value(self):
        try:
            return min(max(float(self.adapt.get().replace(",", ".")), 0.0), 1.0)
        except ValueError:
            return 0.0

    def predict_text(self, stl, grid):
        """Prevision du resultat si un essai rapide a deja ete fait pour ce STL, sinon chaine vide."""
        probe = self.probes.get((stl, round(self.adapt_value(), 2)))
        if not probe or grid <= 0:
            return ""
        p = sc_stl.predict_result(probe, grid)
        total, _ = sc_stl.system_memory()
        txt = (f"Resultat prevu : {p['faces'] / 1e6:.1f} M faces, STL d'environ {sc_stl.fmt_bytes(p['stl_bytes'])}, "
               f"RAM d'environ {sc_stl.fmt_bytes(p['ram_bytes'])}")
        if grid > 2 * probe["g_ref"]:  # loin des essais : la loi surestime (Bengal : x2 a 4 fois la grille)
            txt += " (prevision large, sans doute surestimee jusqu'a 2 fois)"
        if total and p["ram_bytes"] > total:
            txt += f"\nATTENTION : plus que ta RAM ({sc_stl.fmt_bytes(total)}), tres lent (Windows utilisera le disque)"
        if p["stl_bytes"] > 2e9:
            txt += "\nATTENTION : un STL de plus de 2 Go est difficile a ouvrir dans un trancheur"
        return txt

    def size_voxel(self):
        """Calcule le voxel qui donne un STL d'environ 'STL max' Mo (essai rapide si besoin)."""
        stl = self.selected()
        if not stl or not self.cache.get(stl, {}).get("extents"):
            self.set_report("Attendez la fin du rendu du modele.")
            return
        key = (stl, round(self.adapt_value(), 2))
        if key in self.probes:
            self.apply_size_voxel(stl)
        else:
            self.set_report("Essai rapide a basse resolution pour mesurer ce modele (quelques secondes "
                            "a une minute)...")
            self.start_task("Essai rapide...", self._probe_task, stl, key[1])

    def _probe_task(self, stl, adapt):
        return ("probed", stl, adapt, sc_stl.probe_model(stl, adapt, on_progress=self._prog))

    def apply_size_voxel(self, stl):
        probe = self.probes[(stl, round(self.adapt_value(), 2))]
        ext = self.cache[stl]["extents"]
        try:
            max_mb = float(self.stl_max.get().replace(",", "."))
            n, unit = self.scale_params()
        except ValueError:
            self.set_report("Valeur invalide (STL max ou echelle).")
            return
        grid = sc_stl.grid_for_stl_size(probe, max_mb * 1e6)
        voxel = max(ext) / grid
        self.voxel.set(f"{voxel:.4g}")
        on_piece = voxel * unit / n
        detail = dict(PRINTERS)[self.printer.get()]
        txt = (f"Voxel pour un STL d'environ {max_mb:g} Mo : {voxel:.4g} {self.stl_unit.get()} "
               f"(grille de {grid:.0f} cubes)\n"
               f"Sur la piece (1:{n:g}) : {on_piece:.3g} mm, pour un detail imprimante de {detail:g} mm\n"
               + self.predict_text(stl, grid))
        if on_piece > detail:
            txt += ("\n\nLe voxel est plus gros que le detail de l'imprimante : des details imprimables seront "
                    "perdus. Augmentez 'STL max' ou imprimez plus grand.")
        self.set_report(txt)

    def print_voxel(self):
        stl = self.selected()
        ext = self.cache.get(stl, {}).get("extents")
        if not ext:
            self.set_report("Attendez la fin du rendu du modele.")
            return
        try:
            n, unit = self.scale_params()
        except ValueError:
            self.set_report("Echelle invalide.")
            return
        detail = dict(PRINTERS)[self.printer.get()]
        voxel_print = detail / 2  # deux voxels par detail minimal : marge de securite
        voxel = voxel_print * n / unit  # en unite du STL
        self.voxel.set(f"{voxel:.4g}")
        grid = max(ext) / voxel
        size = [round(e * unit / n, 1) for e in ext]
        txt = (f"Taille imprimee : {size[0]:g} x {size[1]:g} x {size[2]:g} mm\n"
               f"Detail mini de l'imprimante : {detail:g} mm\n"
               f"Voxel : {voxel_print:g} mm sur la piece = {voxel:.4g} {self.stl_unit.get()} dans le STL\n"
               f"Grille : {grid:.0f} cubes sur la plus grande dimension")
        try:
            bed = self.bed_mm()
            if max(size) > bed:
                txt = (f"TROP GRAND pour un plateau de {bed:g} mm (plus grande dimension : {max(size):g} mm).\n"
                       f"Echelle conseillee : 1:{self.fit_scale(ext)} (bouton 'Adapter l'echelle').\n"
                       f"Verifiez aussi l'unite du STL : en mm si converti par l'outil.\n\n") + txt
        except ValueError:
            pass
        pred = self.predict_text(stl, grid)
        if pred:
            txt += "\n" + pred
        else:
            txt += "\n(Le bouton 'Voxel pour cette taille' prevoit la taille du fichier et la RAM.)"
        if grid > GRID_WARN:
            txt += (f"\n\nATTENTION : grille tres fine, calcul long et gourmand en memoire. "
                    f"Un voxel de {max(ext) / GRID_WARN:.4g} reste sous {self.to_print_mm(max(ext) / GRID_WARN):.2f} mm "
                    f"sur la piece.")
        self.set_report(txt)

    def deviation_text(self, dev):
        if not dev:
            return ""
        u = self.stl_unit.get()
        try:
            pm = self.to_print_mm
            pm(1.0)
        except ValueError:
            pm = None

        def fmt(v):
            s = f"{v:.3g} {u}"
            return s + (f" ({pm(v):.3g} mm imprime)" if pm else "")

        mv, lo = dev["moved"], dev["lost"]
        return ("\n\nEcart avec l'original"
                + (f" (echelle 1:{self.scale_n.get()})" if pm else "") + " :\n"
                f"Surface deplacee : moyenne {fmt(mv['mean'])}, 95 % sous {fmt(mv['p95'])}, max {fmt(mv['max'])}\n"
                f"Details perdus en saillie : 99 % sous {fmt(lo['p99'])}, max {fmt(lo['max'])}\n"
                f"Points perdus de plus de {fmt(dev['threshold'])} : {dev['lost_share']:g} %\n"
                "(les structures internes supprimees ne sont pas comptees)")

    # ---------- rendu ----------
    def show_selected(self):
        stl = self.selected()
        if not stl:
            return
        if stl in self.cache:
            self.show_view()
            return
        self.start_task("Rendu Blender...", self._render_task, stl, self.render_size)

    def _render_task(self, stl, size):
        prefix = self.tmp / f"v{len(self.cache)}_{Path(stl).stem}"
        res = sc_stl.render_views(stl, str(prefix), size=size, on_progress=self._prog)
        self.cache[stl] = views_data(res)
        return ("rendered", stl)

    def on_heat_toggle(self):
        stl = self.selected()
        if self.heat_var.get() and stl in self.pairs and stl not in self.heat:
            self.start_task("Carte des pertes...", self._heat_task, stl, self.render_size)
        else:
            self.show_view()

    def _heat_task(self, stl, size):
        src, thr = self.pairs[stl]
        prefix = self.tmp / f"h{len(self.heat)}_{Path(stl).stem}"
        res = sc_stl.heatmap_views(src, stl, str(prefix), thr, size=size, on_progress=self._prog)
        self.heat[stl] = views_data(res)
        return ("rendered", stl)

    def update_heat_controls(self):
        stl = self.selected()
        if stl in self.pairs:
            self.heat_chk.config(state="normal")
            thr = self.pairs[stl][1]
            u = self.stl_unit.get()
            try:
                extra = f" ({self.to_print_mm(thr):.2g} mm imprime)"
            except ValueError:
                extra = ""
            self.legend.config(text=f"gris : intact   jaune : perte ~{thr:g} {u}{extra}   rouge : perte >= {4 * thr:g} {u}")
        else:
            self.heat_var.set(False)
            self.heat_chk.config(state="disabled")
            self.legend.config(text="")

    def show_view(self):
        stl = self.selected()
        self.update_heat_controls()
        data = self.cache.get(stl)
        if self.heat_var.get() and stl in self.heat:
            data = self.heat[stl]
        if not data:
            return
        self.photo = tk.PhotoImage(file=data[self.view_var.get()])
        self.canvas.config(image=self.photo)
        ext = data["extents"]
        self.status.config(text=f"Dimensions : {ext[0]:g} x {ext[1]:g} x {ext[2]:g} (unite du STL)")

    def sync_advanced(self):
        """Affiche les reglages voxel si demande, ou si la methode voxel est choisie."""
        if self.show_adv.get() or self.method.get() == "voxel":
            self.adv.pack(fill="x", before=self.repair_btn)
        else:
            self.adv.pack_forget()

    def hollow_selected(self):
        """Evide le STL selectionne (a la taille d'impression) en gardant sa surface exterieure intacte."""
        stl = self.selected()
        if not stl:
            return
        try:
            wall = float(self.shell.get().replace(",", "."))
        except ValueError:
            self.set_report("Epaisseur de coque invalide.")
            return
        ext = self.cache.get(stl, {}).get("extents")
        if ext and max(ext) > 2000 and self.stl_unit.get() == "mm":
            if not messagebox.askyesno("Evider", f"Ce STL mesure {max(ext):.0f} mm : il semble a taille reelle.\n"
                                       "L'evidement se regle en mm a la taille d'impression : creez d'abord le STL "
                                       "a l'echelle (ou les morceaux), puis evidez-le.\n\nEvider quand meme ?",
                                       parent=self.win):
                return
        src = Path(stl)
        dst = src.with_name(f"{src.stem}_evide.stl")
        self.set_report(f"Evidement : coque de {wall:g} mm, surface exterieure intacte...")
        self.start_task("Evidement...", self._hollow_task, str(src), str(dst), wall)

    def _hollow_task(self, src, dst, wall):
        res = sc_stl.hollow_stl(src, dst, wall, on_progress=self._prog)
        return ("hollowed", dst, res)

    @property
    def render_size(self):
        """Taille des images de rendu : toute la zone d'affichage disponible (carree)."""
        try:
            w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        except tk.TclError:
            w = h = 0
        side = min(w, h) - 8
        return side if side >= 400 else 700

    # ---------- reparation ----------
    def repair(self):
        stl = self.selected()
        if not stl:
            return
        if self.method.get() == "precise":
            return self.repair_precise(stl)
        try:
            args = (float(self.weld.get()), int(self.hole.get()), float(self.voxel.get()),
                    float(self.island.get()) / 100.0, float(self.adapt.get().replace(",", ".")))
        except ValueError:
            self.set_report("Valeurs invalides.")
            return
        voxel = args[2]
        ext = self.cache.get(stl, {}).get("extents")
        grid = max(ext) / voxel if voxel > 0 and ext else 0.0
        est = sc_stl.estimate_repair(sc_stl.stl_face_count(stl), grid)
        est_txt = ""
        if est:
            est_txt = f"Duree estimee : environ {sc_stl.fmt_duration(est['total'])}"
            if est["extrapolated"]:
                est_txt += " (cas plus lourd que ceux deja mesures : estimation peu sure)"
        pred = self.predict_text(stl, grid)
        if pred:
            est_txt = (est_txt + "\n" + pred).strip()
        if voxel > 0 and ext and sc_stl.MEM_GUARD:  # sans garde-fou memoire : ni refus ni confirmation
            if grid > GRID_MAX:
                self.set_report(f"Voxel trop fin : {grid:.0f} cubes sur la plus grande dimension (maximum {GRID_MAX}).\n"
                                f"Cela saturerait la memoire. Utilisez au moins {max(ext) / GRID_MAX:.4g}, "
                                f"ou 'Adapter l'echelle' puis 'Calculer le voxel'.")
                return
            if grid > GRID_WARN and not messagebox.askyesno(
                    "Reparation", f"Grille de {grid:.0f} cubes sur la plus grande dimension : calcul long et "
                                  f"gourmand en memoire. L'outil l'arretera si la RAM libre passe sous "
                                  f"{sc_stl.MEM_MIN_FREE_GB:g} Go.\n{est_txt}\nContinuer ?", parent=self.win):
                return
        self.start_task("Reparation Blender...", self._repair_task, stl, *args, est=est)
        if est_txt:
            self.set_report(est_txt)

    def repair_precise(self, stl):
        """Methode precise : pieces fermees intactes, toles epaissies a la paroi minimale imprimable."""
        try:
            wall = float(self.wall.get().replace(",", "."))
            n, unit = self.scale_params()
            weld = float(self.weld.get())
        except ValueError:
            self.set_report("Valeurs invalides (paroi, echelle ou soudure).")
            return
        thick = wall * n / unit  # epaisseur dans l'unite du STL
        faces = sc_stl.stl_face_count(stl) or 0
        self.set_report(f"Methode precise a l'echelle 1:{n:g} : paroi de {wall:g} mm sur la piece = "
                        f"{thick:.4g} {self.stl_unit.get()} dans le STL.\n"
                        f"Ecart maximal attendu : {wall / 2:g} mm sur la piece.\n"
                        f"Duree indicative : environ {sc_stl.fmt_duration(5 + 12 * faces / 1e6)} "
                        f"(Bengal, 2,8 M faces : 37 s).")
        self.start_task("Reparation precise...", self._precise_task, stl, thick, weld)

    def _precise_task(self, stl, thick, weld):
        src = Path(stl)
        dst = src.with_name(src.stem.replace("_repaired", "") + "_repaired.stl")
        res = sc_stl.precise_stl(str(src), str(dst), thick, weld, on_progress=self._prog)
        return ("repaired", str(dst), res, str(src))

    def _repair_task(self, stl, weld, hole, voxel, island, adapt=0.0):
        src = Path(stl)
        dst = src.with_name(src.stem.replace("_repaired", "") + "_repaired.stl")
        res = sc_stl.repair_stl(str(src), str(dst), weld, hole, voxel, island, on_progress=self._prog, adapt=adapt)
        return ("repaired", str(dst), res, str(src))

    def _prog(self, pct, msg):
        self.q.put(("progress", pct, msg))

    # ---------- threads ----------
    def stop_task(self):
        if sc_stl.stop_group("preview"):
            self.step_msg = "arret en cours"

    def close(self):
        import shutil
        sc_stl.stop_group("preview")  # ne laisse pas Blender tourner apres la fermeture
        self.win.destroy()
        shutil.rmtree(self.tmp, ignore_errors=True)  # images d'apercu et cartes des pertes

    def remaining_text(self):
        """Temps restant d'apres l'estimation par etape, corrige par la vitesse reellement observee."""
        est = self.est
        if not est:
            return ""
        now = time.time()
        steps = est["steps"]
        cur = self.cur_pct
        if cur is None:  # Blender demarre
            left = est["total"] - (now - self.task_t0)
        else:
            done_exp = est["startup"] + sum(d for p, d in steps.items() if p < cur)
            done_real = self.cur_start - self.task_t0
            speed = min(max(done_real / done_exp, 0.3), 5.0) if done_exp > 2 else 1.0
            in_step = now - self.cur_start
            left_cur = steps.get(cur, 0) * speed - in_step
            left = max(left_cur, 0) + speed * sum(d for p, d in steps.items() if p > cur)
            if left_cur < 0 and left < 5:
                return "  - bientot fini"
        return f"  - reste environ {sc_stl.fmt_duration(left)}" if left > 0 else "  - bientot fini"

    def start_task(self, label, fn, *args, est=None):
        if self.busy:
            return
        self.est = est
        self.cur_pct = None
        self.cur_start = time.time()
        self.busy = True
        self.status.config(text=label)
        self.repair_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.task_t0 = time.time()
        self.step_msg = "demarrage de Blender"
        self.pbar.config(value=0)

        def run():
            sc_stl.set_group("preview")
            try:
                self.q.put(fn(*args))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("error", str(exc)))

        threading.Thread(target=run, daemon=True).start()

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    self.pbar.config(value=msg[1])
                    self.step_msg = msg[2]
                    if msg[1] != self.cur_pct:
                        self.cur_pct, self.cur_start = msg[1], time.time()
                    continue
                self.busy = False
                self.repair_btn.config(state="normal")
                self.stop_btn.config(state="disabled")
                self.pbar.config(value=100 if msg[0] != "error" else 0)
                self.pstep.config(text=f"termine en {time.time() - self.task_t0:.0f} s")
                if msg[0] == "error":
                    self.status.config(text="Erreur")
                    self.set_report(msg[1])
                elif msg[0] == "hollowed":
                    _, dst, res = msg
                    if res["cavity_faces"]:
                        self.set_report(
                            f"Ecrit : {Path(dst).name}\nCoque de {res['wall']:g} mm\n"
                            f"Resine : {res['volume_full_ml'] - res['volume_saved_ml']:.1f} mL au lieu de "
                            f"{res['volume_full_ml']:.1f} mL ({res['saved_pct']:g} % economises)\n\n"
                            "Ajoutez des trous d'evacuation dans le trancheur (outil de percage), en bas de la "
                            "piece, pour vider la resine de la cavite.")
                    else:
                        self.set_report("Piece trop fine pour etre evidee avec cette coque : elle reste pleine.")
                    if dst not in self.paths:
                        self.add_path(dst)
                elif msg[0] == "split":
                    res = msg[1]
                    c, s, e = res["counts"], res["step"], res["extent"]
                    still_open = sum(p["open_edges"] for p in res["pieces"])
                    self.set_report(
                        f"{len(res['pieces'])} morceaux ecrits dans :\n{res['outdir']}\n\n"
                        f"Maquette : {e[0]:g} x {e[1]:g} x {e[2]:g} mm\n"
                        f"Grille : {c[0]} x {c[1]} x {c[2]}, morceaux de {s[0]:g} x {s[1]:g} x {s[2]:g} mm au plus\n"
                        f"Bords restes ouverts : {still_open}"
                        + ("" if still_open == 0 else " (le trancheur pourra les reboucher)")
                        + (f"\nAlignement : {res['pins']} goupilles a imprimer (goupilles.stl), "
                           f"{sum(p.get('holes', 0) for p in res['pieces'])} trous perces"
                           if res.get("pins") else "")
                        + "\n\nNoms : X = gauche->droite, Y = arriere->avant, Z = bas->haut.\n"
                          "Positions de chaque morceau : assemblage.txt dans le dossier.")
                elif msg[0] == "probed":
                    _, stl, adapt, data = msg
                    self.probes[(stl, round(adapt, 2))] = data
                    if stl == self.selected():
                        self.apply_size_voxel(stl)
                elif msg[0] == "rendered":
                    if msg[1] == self.selected():
                        self.show_view()
                elif msg[0] == "repaired":
                    _, dst, res, src = msg
                    wall = time.time() - self.task_t0
                    if res.get("method") != "precise":  # l'historique sert a estimer les reparations voxel
                        sc_stl.record_repair_timing(res, wall, sc_stl.stl_face_count(src))
                    timing = f"\nDuree : {sc_stl.fmt_duration(wall)}"
                    if self.est:
                        timing += f" (estimee : {sc_stl.fmt_duration(self.est['total'])})"
                    if res.get("deviation"):
                        self.pairs[dst] = (src, res["deviation"]["threshold"])
                        self.heat.pop(dst, None)  # une nouvelle reparation invalide l'ancienne carte
                    b, a = res["before"], res["after"]
                    self.set_report(
                        f"Ecrit : {Path(dst).name}\n"
                        f"Faces : {b['faces']} -> {a['faces']}\n"
                        f"Aretes non manifold : {b['non_manifold_edges']} -> {a['non_manifold_edges']}\n"
                        f"Aretes de bord (trous) : {b['boundary_edges']} -> {a['boundary_edges']}\n"
                        + (f"Methode precise : {res['closed_parts']} pieces deja fermees gardees intactes, "
                           f"{res['parts'] - res['closed_parts']} toles epaissies de {res['thick']:.4g}"
                           if res.get("method") == "precise" else
                           f"Ilots : {res['islands']} (supprimes : {res['islands_removed']})")
                        + (f"\nRemaillage voxel : {res['voxel']:g}" if res["voxel"] else "")
                        + timing
                        + self.deviation_text(res.get("deviation")))
                    if dst not in self.paths:
                        self.add_path(dst)
                    self.listbox.selection_clear(0, "end")
                    self.listbox.selection_set(self.paths.index(dst))
                    self.show_selected()
        except queue.Empty:
            pass
        if self.busy:  # le chrono avance meme pendant une etape longue (remaillage voxel)
            self.pstep.config(text=f"{self.step_msg}  ({sc_stl.fmt_duration(time.time() - self.task_t0)})"
                                   f"{self.remaining_text()}")
        if self.win.winfo_exists():
            self.win.after(100, self.poll)
