"""Assistant "Preparer pour l'impression" : de l'element coche au dossier pret pour le trancheur.

Enchaine, sans manipulation : conversion a taille reelle -> reparation precise (paroi minimale de
l'imprimante a l'echelle choisie) -> mise a l'echelle ou decoupe en morceaux avec trous d'alignement
-> evidement (resine) -> dossier final avec lisezmoi.txt.
"""
import os
import queue
import shutil
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import sc_stl
from sc_preview import PIN_SIZE, PRINTER_WALL, PRINTERS, remember


class PrintWizard:
    def __init__(self, app, source):
        """source : ("entity", nom) | ("place", chemin p4k) | ("asset", Node) ; libelle lisible en [2]."""
        self.app, self.source = app, source
        self.q = queue.Queue()
        self.busy = False
        self.win = tk.Toplevel(app.tk)
        self.win.title("Preparer pour l'impression")
        self.win.geometry("760x640")
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        f = ttk.Frame(self.win, padding=10)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text=f"Element : {source[2]}", font=("", 10, "bold")).pack(anchor="w")
        rm = [lbl for k, lbl in sc_stl.REMOVE_LABELS if app.remove_vars[k].get()]
        ttk.Label(f, text="Retire : " + (", ".join(rm) if rm else "rien") +
                  "   (cases 'Retirer du vaisseau' de la fenetre principale)").pack(anchor="w", pady=(0, 8))

        g = ttk.LabelFrame(f, text="Impression", padding=6)
        g.pack(fill="x")
        self.printer = tk.StringVar(value=PRINTERS[2][0])
        ttk.Label(g, text="Imprimante :").grid(row=0, column=0, sticky="w")
        ttk.Combobox(g, textvariable=self.printer, values=[p[0] for p in PRINTERS], state="readonly",
                     width=28).grid(row=0, column=1, columnspan=3, sticky="w")
        self.size_mode = tk.StringVar(value="length")
        self.length = tk.StringVar(value="250")
        self.scale = tk.StringVar(value="4000")
        ttk.Radiobutton(g, text="Longueur finale (mm) :", value="length", variable=self.size_mode).grid(
            row=1, column=0, sticky="w")
        ttk.Entry(g, textvariable=self.length, width=8).grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(g, text="ou echelle 1 :", value="scale", variable=self.size_mode).grid(
            row=1, column=2, sticky="w", padx=(12, 0))
        ttk.Entry(g, textvariable=self.scale, width=8).grid(row=1, column=3, sticky="w")
        ttk.Label(g, text="Volume d'impression X / Y / Z (mm) :").grid(row=2, column=0, sticky="w")
        self.vol = [tk.StringVar(value="300") for _ in range(3)]
        vf = ttk.Frame(g)
        vf.grid(row=2, column=1, columnspan=3, sticky="w")
        for v in self.vol:
            ttk.Entry(vf, textvariable=v, width=6).pack(side="left", padx=1)

        o = ttk.LabelFrame(f, text="Options", padding=6)
        o.pack(fill="x", pady=8)
        self.hollow = tk.BooleanVar(value=True)
        self.shell = tk.StringVar(value="2")
        row = ttk.Frame(o)
        row.pack(fill="x")
        ttk.Checkbutton(row, text="Evider (resine), coque de", variable=self.hollow).pack(side="left")
        ttk.Entry(row, textvariable=self.shell, width=5).pack(side="left", padx=2)
        ttk.Label(row, text="mm").pack(side="left")
        self.pins = tk.BooleanVar(value=True)
        ttk.Checkbutton(o, text="Trous d'alignement entre les morceaux (+ goupilles a imprimer)",
                        variable=self.pins).pack(anchor="w")

        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Dossier de sortie :").pack(side="left")
        self.outdir = tk.StringVar(value=str(sc_stl.OUTPUT_DIR / "Impression"))
        ttk.Entry(row, textvariable=self.outdir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row, text="...", width=3, command=self.choose_dir).pack(side="left")

        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        self.go_btn = ttk.Button(row, text="Lancer", command=self.start)
        self.go_btn.pack(side="left")
        self.stop_btn = ttk.Button(row, text="Arreter", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.step = ttk.Label(row, text="")
        self.step.pack(side="left", padx=6)
        self.bar = ttk.Progressbar(f, mode="determinate", maximum=100)
        self.bar.pack(fill="x")
        self.log = tk.Text(f, height=14, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, pady=(6, 0))

        for var, key in ((self.printer, "wiz_printer"), (self.size_mode, "wiz_mode"), (self.length, "wiz_length"),
                         (self.scale, "wiz_scale"), (self.vol[0], "vol_x"), (self.vol[1], "vol_y"),
                         (self.vol[2], "vol_z"), (self.hollow, "wiz_hollow"), (self.shell, "shell"),
                         (self.pins, "pins"), (self.outdir, "wiz_outdir")):
            remember(var, key)
        self.win.after(100, self.poll)

    # ---------- interface ----------
    def choose_dir(self):
        d = filedialog.askdirectory(parent=self.win, title="Dossier de sortie")
        if d:
            self.outdir.set(os.path.normpath(d))

    def say(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def stop(self):
        self.cancel = True
        sc_stl.stop_group("wizard")
        self.step.config(text="arret en cours...")

    def close(self):
        if self.busy and not messagebox.askyesno("Assistant", "Une preparation est en cours. L'arreter ?",
                                                 parent=self.win):
            return
        sc_stl.stop_group("wizard")
        self.cancel = True
        self.win.destroy()

    def start(self):
        try:
            p = dict(
                printer=self.printer.get(),  # lu ici : Tkinter ne doit pas etre lu depuis le calcul
                detail=dict(PRINTERS)[self.printer.get()], wall=PRINTER_WALL[self.printer.get()],
                mode=self.size_mode.get(), length=float(self.length.get().replace(",", ".")),
                scale=float(self.scale.get().replace(",", ".")),
                vol=[float(v.get().replace(",", ".")) for v in self.vol],
                hollow=self.hollow.get(), shell=float(self.shell.get().replace(",", ".")),
                pins=self.pins.get(), outdir=self.outdir.get().strip(),
                lod=int(self.app.lod_var.get()), no_interior=self.app.noint_var.get(),
                no_att=self.app.noatt_var.get(),
                remove=tuple(k for k, v in self.app.remove_vars.items() if v.get()))
        except ValueError:
            messagebox.showerror("Assistant", "Une valeur n'est pas un nombre.", parent=self.win)
            return
        if min(p["vol"]) <= 0 or p["length"] <= 0 or p["scale"] <= 0 or not p["outdir"]:
            messagebox.showerror("Assistant", "Valeurs ou dossier invalides.", parent=self.win)
            return
        self.cancel = False
        self.busy = True
        self.go_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.t0 = time.time()
        threading.Thread(target=self.run, args=(p,), daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, *val = self.q.get_nowait()
                if kind == "say":
                    self.say(val[0])
                elif kind == "step":
                    self.step.config(text=val[0])
                elif kind == "pct":
                    self.bar.config(value=val[0])
                elif kind == "end":
                    self.busy = False
                    self.go_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.step.config(text=f"termine en {sc_stl.fmt_duration(time.time() - self.t0)}")
                    self.bar.config(value=100 if val[0] else 0)
                    if val[0]:
                        self.say(f"\nDossier pret : {val[0]}")
        except queue.Empty:
            pass
        if self.win.winfo_exists():
            self.win.after(150, self.poll)

    # ---------- chaine complete (thread) ----------
    def run(self, p):
        sc_stl.set_group("wizard")
        work = Path(tempfile.mkdtemp(prefix="sc_glb_"))  # nettoye a la fin, ou au prochain demarrage
        out = None
        try:
            out = self._pipeline(p, work)
        except sc_stl.ConvertError as exc:
            self.q.put(("say", f"\nERREUR : {exc}"))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("say", f"\nERREUR inattendue : {exc!r}"))
        finally:
            shutil.rmtree(work, ignore_errors=True)
            self.q.put(("end", out))

    def _phase(self, base, span, label):
        """Callback de progression Blender rapporte a une tranche [base, base+span] de la barre."""
        self.q.put(("step", label))

        def cb(pct, msg):
            self.q.put(("pct", base + span * pct / 100.0))
            self.q.put(("step", f"{label} : {msg}"))
        return cb

    def _check(self):
        if self.cancel:
            raise sc_stl.ConvertError("arrete a la demande")

    def _pipeline(self, p, work):
        say = lambda t: self.q.put(("say", t))  # noqa: E731
        app, kind, ref = self.app, self.source[0], self.source[1]
        name = sc_stl.safe_name(self.source[2])[:60] or "modele"

        # 1. conversion a taille reelle, en mm
        self.q.put(("step", "conversion"))
        self.q.put(("pct", 2))
        common = dict(lod=p["lod"], scale=1000.0, z_up=True, no_interior=p["no_interior"],
                      no_attachments=p["no_att"], remove=p["remove"])
        if kind == "entity":
            stl, info, _m = sc_stl.convert_entity(ref, app.p4k_path, work, work, **common)
        elif kind == "place":
            stl, info, _m = sc_stl.convert_socpak(ref, app.p4k_path, work, work, **common)
        else:
            stl, info, _m = sc_stl.convert_asset(ref.path, ref.exts, app.emap, app.p4k_path, work, work, **common)
        ext = info["extents"]
        say(f"1. Converti : {ext[0] / 1000:g} x {ext[1] / 1000:g} x {ext[2] / 1000:g} m dans le jeu, "
            f"{info['faces']} faces" + (f", {info.get('parts_removed', 0)} pieces retirees"
                                         if info.get("parts_removed") else ""))
        self._check()

        # 2. echelle
        n = p["scale"] if p["mode"] == "scale" else sc_stl.nice_scale(max(ext) / p["length"])
        printed = [e / n for e in ext]
        say(f"2. Echelle 1:{n:g} -> maquette de {printed[0]:.0f} x {printed[1]:.0f} x {printed[2]:.0f} mm")

        # 3. reparation precise : paroi minimale de l'imprimante, ramenee a la taille reelle
        thick = p["wall"] * n
        rep = work / f"{name}_repare.stl"
        res = sc_stl.precise_stl(str(stl), str(rep), thick, on_progress=self._phase(8, 30, "reparation"))
        a = res["after"]
        say(f"3. Repare (methode precise, parois de {p['wall']:g} mm imprimes) : "
            f"{res['closed_parts']} pieces intactes, {res['parts'] - res['closed_parts']} toles epaissies, "
            f"{a['boundary_edges']} bords ouverts restants")
        self._check()

        # 4. a l'echelle : une piece si elle tient dans le volume (dans une orientation), sinon decoupe
        dest = Path(p["outdir"]) / f"{name}_1-{n:g}"
        dest.mkdir(parents=True, exist_ok=True)
        fits = all(s <= v for s, v in zip(sorted(printed), sorted(p["vol"])))
        finals, pins_txt = [], ""
        if fits:
            one = dest / f"{name}_1-{n:g}.stl"
            sc_stl.scale_stl(str(rep), str(one), 1.0 / n)
            finals = [one]
            say(f"4. Tient en une seule piece dans {p['vol'][0]:g} x {p['vol'][1]:g} x {p['vol'][2]:g} mm "
                "(au besoin, tournez-la dans le trancheur)")
        else:
            r = sc_stl.split_stl(str(rep), str(dest), 1.0 / n, p["vol"], on_progress=self._phase(40, 30, "decoupe"),
                                 pins=PIN_SIZE if p["pins"] else None)
            finals = [dest / pc["file"] for pc in r["pieces"]]
            opened = sum(pc["open_edges"] for pc in r["pieces"])
            c = r["counts"]
            say(f"4. Decoupe en {len(finals)} morceaux (grille {c[0]} x {c[1]} x {c[2]}), "
                f"{opened} bords ouverts restants")
            if r.get("pins"):
                pins_txt = (f"{r['pins']} goupilles dans goupilles.stl (diametre {2 * PIN_SIZE[0] - 0.3:.1f} mm, "
                            f"longueur {2 * PIN_SIZE[1] - 0.5:.1f} mm)")
                say(f"   Alignement : {pins_txt}")
            elif p["pins"]:
                say(f"   Alignement : aucun endroit assez epais sur les coupes pour un trou de "
                    f"{2 * PIN_SIZE[0]:g} mm sur {PIN_SIZE[1]:g} mm de profondeur ; collage a plat.")
        self._check()

        # 5. evidement (resine) de chaque piece
        resin_full = resin_left = 0.0
        if p["hollow"]:
            for k, fp in enumerate(finals):
                self._check()
                cb = self._phase(72 + 25 * k / len(finals), 25 / len(finals), f"evidement {k + 1}/{len(finals)}")
                tmp = fp.with_name(fp.stem + "_evide.stl")
                h = sc_stl.hollow_stl(str(fp), str(tmp), p["shell"], on_progress=cb)
                resin_full += h["volume_full_ml"]
                resin_left += h["volume_full_ml"] - h["volume_saved_ml"]
                os.replace(tmp, fp)  # le fichier final est la version evidee
            say(f"5. Evide (coque de {p['shell']:g} mm) : environ {resin_left:.0f} mL de resine "
                f"au lieu de {resin_full:.0f} mL")
        self.q.put(("pct", 99))

        # 6. lisezmoi
        lines = [f"Element : {self.source[2]}", f"Echelle : 1:{n:g}",
                 f"Taille de la maquette : {printed[0]:.0f} x {printed[1]:.0f} x {printed[2]:.0f} mm",
                 f"Imprimante : {p['printer']} (parois mini {p['wall']:g} mm)",
                 f"Fichiers : {len(finals)} ({'une piece' if fits else 'voir assemblage.txt'})"]
        if pins_txt:
            lines.append("Alignement : " + pins_txt + " ; collez chaque goupille dans les deux trous en vis-a-vis.")
        if p["hollow"]:
            lines.append(f"Evide : coque de {p['shell']:g} mm. Percez des trous d'evacuation dans le trancheur, "
                         "en bas de chaque piece, pour vider la resine.")
        lines.append("Les fichiers s'ouvrent a 100 % dans le trancheur : ils sont deja a la taille d'impression.")
        (dest / "lisezmoi.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(dest)
