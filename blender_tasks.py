"""Taches Blender en headless pour les STL.

blender --background --factory-startup --python blender_tasks.py -- repair IN.stl OUT.stl [options]
blender --background --factory-startup --python blender_tasks.py -- render IN.stl OUT_PREFIX [--size 900]

Options repair :
  --weld D        distance de soudure des sommets (unite du STL, defaut 0.01)
  --hole N        ferme les trous de N cotes maximum (defaut 64)
  --voxel S       remaillage voxel de taille S pour garantir un volume etanche (0 = desactive)
  --min-island F  supprime les ilots dont le nombre de faces < F * (plus gros ilot) (defaut 0)
Les resultats sont ecrits en JSON sur la ligne 'RESULT: {...}'.
"""
import json
import math
import os
import sys

import re

import bmesh
import bpy

REMOVE_RX = {  # noms des points d'attache dans les vaisseaux exportes par StarBreaker
    "gear": r"hardpoint_landing_gear",
    "weapons": r"hardpoint_(weapon|gun|gimbal|turret|class_\d)",
    "missiles": r"hardpoint_(missile|bomb|torpedo)",
    "thrusters": r"hardpoint_thruster",
    "countermeasures": r"hardpoint_(countermeasure|cm_)",
}
DMG_RE = re.compile(r"_dmg(_|\.|$)|_damaged?(_|\.|$)", re.IGNORECASE)
from mathutils import Vector


def argv():
    a = sys.argv
    return a[a.index("--") + 1:] if "--" in a else []


def opt(args, name, default, cast=float):
    if name in args:
        return cast(args[args.index(name) + 1])
    return default


_STEPS = []  # (pourcentage, horodatage) de chaque etape, pour mesurer leur duree


def progress(pct, msg):
    """Ligne lue en direct par sc_stl.run_blender pour la barre d'avancement."""
    import time
    _STEPS.append((int(pct), time.time()))
    print(f"PROGRESS: {int(pct)} {msg}", flush=True)


def step_durations():
    """[[pourcentage, secondes], ...] : duree de chaque etape jusqu'a la suivante (ou maintenant)."""
    import time
    marks = _STEPS + [(None, time.time())]
    return [[p, round(marks[i + 1][1] - t, 3)] for i, (p, t) in enumerate(marks[:-1])]


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_stl(path):
    bpy.ops.wm.stl_import(filepath=path)
    obj = bpy.context.selected_objects[0] if bpy.context.selected_objects else bpy.context.object
    bpy.context.view_layer.objects.active = obj
    return obj


def stats(bm):
    nonman = sum(1 for e in bm.edges if not e.is_manifold)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    return {"verts": len(bm.verts), "faces": len(bm.faces), "non_manifold_edges": nonman, "boundary_edges": boundary}


def islands(bm):
    seen, groups = set(), []
    for f in bm.faces:
        if f.index in seen:
            continue
        stack, grp = [f], []
        seen.add(f.index)
        while stack:
            cur = stack.pop()
            grp.append(cur)
            for e in cur.edges:
                for nf in e.link_faces:
                    if nf.index not in seen:
                        seen.add(nf.index)
                        stack.append(nf)
        groups.append(grp)
    return groups


def repair(args):
    src, dst = os.path.abspath(args[0]), os.path.abspath(args[1])
    weld = opt(args, "--weld", 0.01)
    hole = int(opt(args, "--hole", 64))
    voxel = opt(args, "--voxel", 0.0)
    min_island = opt(args, "--min-island", 0.0)
    adapt = min(max(opt(args, "--adapt", 0.0), 0.0), 1.0)  # simplification des zones plates apres voxel

    progress(2, "import du STL")
    clear_scene()
    obj = import_stl(src)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    progress(10, f"analyse ({len(bm.faces)} faces)")
    before = stats(bm)
    grid = max(obj.dimensions) / voxel if voxel > 0 else 0.0  # cubes sur la plus grande dimension
    # pas de copie de l'original en memoire : il est reimporte a la fin pour mesurer l'ecart

    progress(20, "soudure des sommets")
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=weld)
    progress(35, "suppression des faces degenerees")
    bmesh.ops.dissolve_degenerate(bm, dist=weld * 0.1, edges=bm.edges[:])
    bm.faces.index_update()

    removed_islands = 0
    progress(45, "recherche des ilots")
    groups = islands(bm)
    n_islands = len(groups)
    if min_island > 0 and groups:
        biggest = max(len(g) for g in groups)
        doomed = [f for g in groups if len(g) < min_island * biggest for f in g]
        removed_islands = sum(1 for g in groups if len(g) < min_island * biggest)
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
    loose = [v for v in bm.verts if not v.link_faces]
    bmesh.ops.delete(bm, geom=loose, context="VERTS")

    # Avec un remaillage voxel, la fermeture des trous est inutile (le voxel referme tout) et c'est de loin
    # l'etape la plus lente (105 s sur 110 pour l'Arrow) ; sans elle le resultat colle mieux a l'original.
    holes_skipped = voxel > 0 and hole > 0
    if holes_skipped:
        hole = 0
    if hole > 0:
        progress(55, "fermeture des trous")
        boundary = [e for e in bm.edges if e.is_boundary]
        if boundary:
            bmesh.ops.holes_fill(bm, edges=boundary, sides=hole)
    progress(65, "recalcul des normales")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    after = stats(bm)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.update()

    if voxel > 0:
        progress(72, f"remaillage voxel ({voxel:g}), etape la plus longue")
        obj.data.remesh_voxel_size = voxel
        obj.data.remesh_voxel_adaptivity = adapt
        obj.data.remesh_mode = "VOXEL"
        bpy.ops.object.voxel_remesh()
        progress(90, f"analyse du resultat ({len(obj.data.polygons)} faces)")
        after = mesh_stats(obj.data)  # sans bmesh : une copie bmesh d'un tres gros resultat sature la RAM

    progress(92, "mesure de l'ecart avec l'original")
    dev = deviation(src, obj, threshold=voxel / 2 if voxel > 0 else weld * 10)

    progress(97, "export du STL")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.wm.stl_export(filepath=dst, export_selected_objects=True)
    print("RESULT: " + json.dumps({"before": before, "after": after, "islands": n_islands,
                                    "islands_removed": removed_islands, "voxel": voxel, "deviation": dev,
                                    "grid": round(grid, 1), "steps": step_durations(),
                                    "holes_skipped": holes_skipped, "adapt": adapt}))


def precise(args):
    """Reparation precise, sans voxel : aucun detail n'est arrondi.

    -- precise MODELE.stl SORTIE.stl --thick T [--weld D]
    - les pieces deja fermees sont gardees telles quelles ;
    - les pieces ouvertes (toles sans epaisseur) recoivent une epaisseur T centree sur leur surface
      (modificateur Solidify avec bords fermes) : elles deviennent des volumes fermes ;
    - les pieces qui se chevauchent sont laissees ainsi : les trancheurs fusionnent les volumes fermes.
    Ecart maximal avec l'original : T / 2.
    """
    src, dst = os.path.abspath(args[0]), os.path.abspath(args[1])
    thick = opt(args, "--thick", 1.0)
    weld = opt(args, "--weld", 0.01)
    progress(2, "import du STL")
    clear_scene()
    obj = import_stl(src)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    progress(10, f"analyse ({len(bm.faces)} faces)")
    before = stats(bm)
    progress(20, "soudure des sommets")
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=weld)
    bmesh.ops.dissolve_degenerate(bm, dist=weld * 0.1, edges=bm.edges[:])
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    progress(35, "tri des pieces fermees et ouvertes")
    groups = islands(bm)
    closed_faces, open_faces, n_closed = [], [], 0
    for g in groups:
        if all(len(e.link_faces) == 2 for f in g for e in f.edges):
            closed_faces.extend(g)
            n_closed += 1
        else:
            open_faces.extend(g)
    closed_idx = {f.index for f in closed_faces}  # releve avant toute suppression (indices encore justes)
    progress(45, f"{n_closed} pieces fermees gardees, {len(groups) - n_closed} pieces ouvertes a epaissir")

    bm_open = bm.copy()
    # objet "ferme" : on retire les faces ouvertes
    bmesh.ops.delete(bm, geom=open_faces, context="FACES")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(obj.data)
    bm.free()
    # objet "ouvert" : on retire les faces fermees (memes indices dans la copie)
    bm_open.faces.ensure_lookup_table()
    bm_open.faces.index_update()
    bmesh.ops.delete(bm_open, geom=[f for f in bm_open.faces if f.index in closed_idx], context="FACES")
    me_open = bpy.data.meshes.new("ouvert")
    bm_open.to_mesh(me_open)
    bm_open.free()
    oobj = bpy.data.objects.new("ouvert", me_open)
    bpy.context.scene.collection.objects.link(oobj)

    progress(55, f"epaississement des toles ({thick:g})")
    mod = oobj.modifiers.new("epaisseur", "SOLIDIFY")
    mod.thickness = thick
    mod.offset = 0.0          # centree : la surface d'origine reste au milieu de la paroi
    mod.use_rim = True        # ferme les bords de chaque tole
    # "use_even_offset" cree des pointes geantes sur les angles tres aigus : jamais active
    mod.use_even_offset = False
    mod.use_quality_normals = True
    solmode = args[args.index("--solmode") + 1] if "--solmode" in args else "complex"
    if solmode == "complex":  # gere les aretes partagees par 3 faces ou plus (pieces non manifold)
        mod.solidify_mode = "NON_MANIFOLD"
        mod.nonmanifold_thickness_mode = args[args.index("--thickmode") + 1] if "--thickmode" in args else "FIXED"
    else:
        mod.solidify_mode = "EXTRUDE"
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = oobj
    oobj.select_set(True)
    bpy.ops.object.modifier_apply(modifier=mod.name)

    progress(80, "assemblage")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.join()
    after = mesh_stats(obj.data)

    progress(88, "mesure de l'ecart avec l'original")
    dev = deviation(src, obj, threshold=thick / 2)
    progress(96, "export du STL")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.wm.stl_export(filepath=dst, export_selected_objects=True)
    print("RESULT: " + json.dumps({"before": before, "after": after, "parts": len(groups),
                                    "closed_parts": n_closed, "thick": thick, "deviation": dev,
                                    "islands": len(groups), "islands_removed": 0, "voxel": 0,
                                    "grid": 0, "steps": step_durations(), "method": "precise"}))


def split(args):
    """Decoupe un modele en morceaux imprimables, a la taille d'impression.

    -- split MODELE.stl DOSSIER --factor F --size X Y Z
    F : facteur applique d'abord (unite du STL -> mm imprimes), X Y Z : volume d'impression en mm.
    Le modele est coupe selon une grille reguliere ; chaque plan de coupe est rebouche pour que chaque
    morceau soit un objet ferme. Les morceaux vides ne sont pas ecrits.
    """
    import math
    import numpy as np
    from mathutils import Matrix
    src, outdir = os.path.abspath(args[0]), os.path.abspath(args[1])
    factor = opt(args, "--factor", 1.0)
    i = args.index("--size")
    size = [float(args[i + 1]), float(args[i + 2]), float(args[i + 3])]
    pin_r, pin_d = 0.0, 0.0
    if "--pins" in args:  # trous d'alignement : rayon et profondeur (mm) de chaque cote de la coupe
        j = args.index("--pins")
        pin_r, pin_d = float(args[j + 1]), float(args[j + 2])
    pins = {}  # (axe, indice du plan, indices des 2 autres axes) -> points des trous
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]

    progress(2, "import du STL")
    clear_scene()
    obj = import_stl(src)
    me = obj.data
    me.transform(Matrix.Scale(factor, 4))
    me.update()
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    lo, hi = co.min(axis=0), co.max(axis=0)
    ext = hi - lo
    # grille : nombre de morceaux par axe pour que chacun tienne dans le volume d'impression (marge 2 %)
    counts = [max(1, math.ceil(ext[a] / (size[a] * 0.98))) for a in range(3)]
    step = [ext[a] / counts[a] for a in range(3)]
    progress(8, f"grille {counts[0]} x {counts[1]} x {counts[2]} ({step[0]:.0f} x {step[1]:.0f} x {step[2]:.0f} mm)")

    # boite englobante de chaque triangle (le STL ne contient que des triangles)
    vi = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", vi)
    tris = vi.reshape(-1, 3)
    tri = co[tris]
    fmin, fmax = tri.min(axis=1), tri.max(axis=1)
    del tri, vi
    bpy.data.objects.remove(obj, do_unlink=True)
    whole = None
    if pin_r > 0:  # modele entier, pour verifier qu'il y a assez de matiere derriere chaque trou
        from mathutils.bvhtree import BVHTree
        whole = BVHTree.FromPolygons([tuple(c) for c in co], tris.tolist())

    def cell_bmesh(face_ids):
        """BMesh contenant seulement les triangles demandes (sans copier tout le modele)."""
        sub = tris[face_ids]
        used, inv = np.unique(sub.ravel(), return_inverse=True)
        cm = bpy.data.meshes.new("cellule")
        cm.vertices.add(len(used))
        cm.vertices.foreach_set("co", co[used].astype(np.float32).ravel())
        cm.loops.add(len(inv))
        cm.loops.foreach_set("vertex_index", inv.astype(np.int32))
        cm.polygons.add(len(sub))
        cm.polygons.foreach_set("loop_start", np.arange(0, len(inv), 3, dtype=np.int32))
        cm.update(calc_edges=True)
        b = bmesh.new()
        b.from_mesh(cm)
        bpy.data.meshes.remove(cm)
        # pas de soudure ici : les sommets gardent les liens du modele d'origine, et souder fusionnerait
        # des pieces differentes qui se touchent (boucles de coupe impossibles a reboucher)
        return b

    cells = [(ix, iy, iz) for ix in range(counts[0]) for iy in range(counts[1]) for iz in range(counts[2])]
    pieces, eps = [], 1e-4
    for ci, (ix, iy, iz) in enumerate(cells):
        idx = (ix, iy, iz)
        cmin = [lo[a] + idx[a] * step[a] for a in range(3)]
        cmax = [lo[a] + (idx[a] + 1) * step[a] for a in range(3)]
        progress(10 + int(85 * ci / len(cells)), f"morceau {ci + 1}/{len(cells)}")
        inside = np.all(fmax >= np.array(cmin) - eps, axis=1) & np.all(fmin <= np.array(cmax) + eps, axis=1)
        if not inside.any():
            continue
        bm = cell_bmesh(np.nonzero(inside)[0])
        holes_here = [0]
        # coupe sur chaque face interieure de la cellule, puis rebouchage du plan de coupe
        for a in range(3):
            for side, val in ((-1, cmin[a]), (1, cmax[a])):
                if (side < 0 and idx[a] == 0) or (side > 0 and idx[a] == counts[a] - 1):
                    continue  # bord exterieur du modele : rien a couper
                no = [0.0, 0.0, 0.0]
                no[a] = float(side)
                pco = [0.0, 0.0, 0.0]
                pco[a] = val
                res = bmesh.ops.bisect_plane(bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:], dist=1e-5,
                                             plane_co=pco, plane_no=no, clear_outer=True)
                cut_edges = [e for e in res["geom_cut"] if isinstance(e, bmesh.types.BMEdge) and e.is_valid
                             and e.is_boundary]
                if cut_edges:
                    # chaque piece coupee laisse une boucle sur le plan : on la rebouche
                    # (triangle_fill relierait entre elles des pieces qui se chevauchent : a eviter)
                    _separate_bowties(cut_edges)
                    cut_edges = [e for e in cut_edges if e.is_valid and e.is_boundary]
                    caps = bmesh.ops.holes_fill(bm, edges=cut_edges, sides=0)["faces"]
                    if pin_r > 0:
                        others = tuple(idx[b] for b in range(3) if b != a)
                        if side > 0:  # le morceau "du dessous" choisit les emplacements (valables des 2 cotes)
                            key = (a, idx[a] + 1, others)
                            pins[key] = _pin_points(caps, a, pin_r, pin_d, whole)
                        else:
                            key = (a, idx[a], others)
                        for p in pins.get(key, []):
                            f = _cap_at(caps, p, a)
                            if f is not None:
                                _drill_cap(bm, f, p, a, side, pin_r, pin_d)
                                holes_here[0] += 1
        # passes supplementaires : boucles restees ouvertes (coupe passant par un sommet ou une arete
        # existants) ; on recommence tant que ca referme quelque chose
        prev = None
        for _ in range(3):
            left = [e for e in bm.edges if e.is_boundary]
            if not left or len(left) == prev:
                break
            prev = len(left)
            _separate_bowties(left)
            left = [e for e in left if e.is_valid and e.is_boundary]
            bmesh.ops.holes_fill(bm, edges=left, sides=0)
        loose = [v for v in bm.verts if not v.link_faces]
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
        if not bm.faces:
            bm.free()
            continue
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
        pme = bpy.data.meshes.new("morceau")
        bm.to_mesh(pme)
        bm.free()
        st = mesh_stats(pme)
        pob = bpy.data.objects.new("morceau", pme)
        bpy.context.scene.collection.objects.link(pob)
        n_holes = holes_here[0]
        name = f"{stem}_X{ix + 1}_Y{iy + 1}_Z{iz + 1}.stl"
        bpy.ops.object.select_all(action="DESELECT")
        pob.select_set(True)
        bpy.context.view_layer.objects.active = pob
        bpy.ops.wm.stl_export(filepath=os.path.join(outdir, name), export_selected_objects=True)
        pieces.append({"file": name, "faces": st["faces"], "open_edges": st["boundary_edges"], "holes": n_holes,
                       "min": [round(float(v), 1) for v in cmin], "max": [round(float(v), 1) for v in cmax]})
        bpy.data.objects.remove(pob, do_unlink=True)
        bpy.data.meshes.remove(pme)
    n_pins = sum(len(v) for v in pins.values())
    if n_pins:
        _write_pins(os.path.join(outdir, "goupilles.stl"), n_pins, pin_r, pin_d)
    with open(os.path.join(outdir, "assemblage.txt"), "w", encoding="utf-8") as f:
        f.write(f"Modele : {os.path.basename(src)}\nTaille totale : {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm\n")
        if n_pins:
            f.write(f"Alignement : {n_pins} goupilles (goupilles.stl), diametre {2 * pin_r - 0.3:.1f} mm, "
                    f"longueur {2 * pin_d - 0.5:.1f} mm, dans des trous de {2 * pin_r:.1f} mm.\n")
        f.write(f"Grille : {counts[0]} x {counts[1]} x {counts[2]} morceaux de {step[0]:.1f} x {step[1]:.1f} x "
                f"{step[2]:.1f} mm au plus\n")
        f.write("Nom : X = gauche->droite, Y = arriere->avant, Z = bas->haut (1 = premier)\n\n")
        for p in pieces:
            f.write(f"{p['file']} : de {p['min']} a {p['max']} mm\n")
    print("RESULT: " + json.dumps({"counts": counts, "step": [round(s, 1) for s in step],
                                    "extent": [round(float(e), 1) for e in ext], "pieces": pieces,
                                    "outdir": outdir, "pins": n_pins}))


def _sdf_surface(src_obj, voxel, band, threshold, flip):
    """Nouvel objet : iso-surface du champ de distance de src_obj a 'threshold' (negatif = interieur)."""
    ng = bpy.data.node_groups.new("sdf", "GeometryNodeTree")
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi, go = ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
    m2s = ng.nodes.new("GeometryNodeMeshToSDFGrid")
    m2s.inputs["Voxel Size"].default_value = voxel
    m2s.inputs["Band Width"].default_value = band
    g2m = ng.nodes.new("GeometryNodeGridToMesh")
    g2m.inputs["Threshold"].default_value = threshold
    ng.links.new(gi.outputs[0], m2s.inputs["Mesh"])
    ng.links.new(m2s.outputs["SDF Grid"], g2m.inputs["Grid"])
    last = g2m.outputs["Mesh"]
    if flip:
        fl = ng.nodes.new("GeometryNodeFlipFaces")
        ng.links.new(last, fl.inputs[0])
        last = fl.outputs[0]
    ng.links.new(last, go.inputs[0])
    ob = bpy.data.objects.new("sdf", src_obj.data.copy())
    bpy.context.scene.collection.objects.link(ob)
    mod = ob.modifiers.new("sdf", "NODES")
    mod.node_group = ng
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.node_groups.remove(ng)
    return ob


def _volume(ob):
    b = bmesh.new()
    b.from_mesh(ob.data)
    v = abs(b.calc_volume(signed=True))
    b.free()
    return v


def hollow(args):
    """Evide un modele pour la resine, sans toucher a sa surface exterieure.

    -- hollow MODELE.stl SORTIE.stl --wall W [--voxel V]   (W, V en mm a la taille d'impression)
    La cavite est l'iso-surface a -W du champ de distance de tout le modele : elle reste a W de chaque
    surface (pieces interieures comprises) et disparait la ou le modele est plus fin que 2 W.
    Elle est ajoutee retournee (normales vers l'interieur) au modele d'origine, laisse intact.
    """
    src, dst = os.path.abspath(args[0]), os.path.abspath(args[1])
    wall = opt(args, "--wall", 2.0)
    voxel = opt(args, "--voxel", max(wall / 4, 0.05))
    band = int(math.ceil(wall / voxel)) + 3
    progress(5, "import du STL")
    clear_scene()
    obj = import_stl(src)
    progress(20, f"champ de distance (voxel {voxel:g} mm)")
    env = _sdf_surface(obj, voxel, band, 0.0, False)
    progress(55, f"cavite a {wall:g} mm de toutes les surfaces")
    cav = _sdf_surface(obj, voxel, band, -wall, True)
    v_env, v_cav = _volume(env), _volume(cav)
    n_cav = len(cav.data.polygons)
    bpy.data.objects.remove(env, do_unlink=True)
    progress(85, "assemblage")
    if n_cav:
        bpy.ops.object.select_all(action="DESELECT")
        cav.select_set(True)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.join()
    else:
        bpy.data.objects.remove(cav, do_unlink=True)
    progress(95, "export du STL")
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.ops.wm.stl_export(filepath=dst, export_selected_objects=True)
    print("RESULT: " + json.dumps({"wall": wall, "voxel": voxel, "cavity_faces": n_cav,
                                    "volume_full_ml": round(v_env / 1000, 2),
                                    "volume_saved_ml": round(v_cav / 1000, 2),
                                    "saved_pct": round(100 * v_cav / v_env, 1) if v_env else 0.0}))


def _separate_bowties(edges):
    """Separe les sommets ou se touchent plusieurs boucles de bord (boucles en "8"), qu'holes_fill
    ne sait pas reboucher : chaque boucle devient simple."""
    verts = {v for e in edges if e.is_valid for v in e.verts}
    for v in verts:
        if not v.is_valid:
            continue
        b = [e for e in v.link_edges if e.is_boundary]
        if len(b) > 2:
            bmesh.utils.vert_separate(v, b)


def _pin_points(caps, axis, r, depth=0.0, whole=None, max_pins=2):
    """Choisit jusqu'a max_pins emplacements de trous dans les faces de rebouchage d'un plan de coupe :
    les plus grandes sections pleines, avec au moins 1,6 rayon de matiere tout autour, et assez de
    matiere des deux cotes du plan (rayons lances dans le modele entier 'whole')."""
    from mathutils import Vector
    ax = [b for b in range(3) if b != axis]

    def deep_enough(c):
        if whole is None:
            return True
        for s in (1.0, -1.0):
            d = Vector((0.0, 0.0, 0.0))
            d[axis] = s
            hit = whole.ray_cast(c + d * 1e-3, d)
            if hit[0] is None or hit[3] < depth + 0.5:
                return False
        return True
    cands = []
    for f in caps:
        if not f.is_valid or len(f.verts) < 3:
            continue
        poly = [(v.co[ax[0]], v.co[ax[1]]) for v in f.verts]
        area = 0.5 * abs(sum(poly[k][0] * poly[k - 1][1] - poly[k - 1][0] * poly[k][1] for k in range(len(poly))))
        if area < 9 * r * r:
            continue
        cands.append((area, f, poly))
    cands.sort(key=lambda c: -c[0])
    chosen = []
    for _area, f, poly in cands[:40]:
        c = f.calc_center_median()
        p2 = (c[ax[0]], c[ax[1]])
        if not _inside(p2, poly) or _edge_dist(p2, poly) < 1.6 * r:
            continue
        if any(((p2[0] - q[ax[0]]) ** 2 + (p2[1] - q[ax[1]]) ** 2) ** 0.5 < 8 * r for q in chosen):
            continue
        if not deep_enough(c):
            continue
        chosen.append(c.copy())
        if len(chosen) >= max_pins:
            break
    return chosen


def _inside(p, poly):
    x, y = p
    inside = False
    for k in range(len(poly)):
        (x1, y1), (x2, y2) = poly[k - 1], poly[k]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _edge_dist(p, poly):
    best = float("inf")
    for k in range(len(poly)):
        (x1, y1), (x2, y2) = poly[k - 1], poly[k]
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / (dx * dx + dy * dy or 1e-12)))
        best = min(best, ((p[0] - x1 - t * dx) ** 2 + (p[1] - y1 - t * dy) ** 2) ** 0.5)
    return best


def _cylinder(bm, center, axis, r, length, segments=24):
    from mathutils import Matrix
    rot = {0: Matrix.Rotation(math.radians(90), 4, "Y"), 1: Matrix.Rotation(math.radians(-90), 4, "X"),
           2: Matrix.Identity(4)}[axis]
    m = Matrix.Translation(center) @ rot
    try:
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segments, radius1=r, radius2=r,
                              depth=length, matrix=m)
    except TypeError:  # anciennes versions de Blender
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=segments, diameter1=r, diameter2=r,
                              depth=length, matrix=m)


def _cap_at(caps, p, axis):
    """Face de rebouchage qui contient le point p (sur le plan de coupe), ou None."""
    ax = [b for b in range(3) if b != axis]
    p2 = (p[ax[0]], p[ax[1]])
    for f in caps:
        if f.is_valid and _inside(p2, [(v.co[ax[0]], v.co[ax[1]]) for v in f.verts]):
            return f
    return None


def _drill_cap(bm, face, center, axis, side, r, depth, segments=20):
    """Trou borgne sans booleen : cercle decoupe dans la face de coupe (plane), tube ferme vers l'interieur."""
    from mathutils import Vector
    n = Vector((0.0, 0.0, 0.0))
    n[axis] = float(side)  # normale sortante de la face de coupe
    ax = [b for b in range(3) if b != axis]
    u, w = Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 0.0))
    u[ax[0]], w[ax[1]] = 1.0, 1.0
    c = Vector(center)
    c[axis] = face.verts[0].co[axis]  # exactement sur le plan de la face
    ring = [bm.verts.new(c + (u * math.cos(2 * math.pi * k / segments) +
                              w * math.sin(2 * math.pi * k / segments)) * r) for k in range(segments)]
    ring_edges = [bm.edges.new((ring[k], ring[(k + 1) % segments])) for k in range(segments)]
    bound = list(face.edges)
    bmesh.ops.delete(bm, geom=[face], context="FACES_ONLY")
    # face de coupe refaite avec un trou circulaire (le remplissage 2D gere les boucles imbriquees)
    bmesh.ops.triangle_fill(bm, use_beauty=True, use_dissolve=False, edges=bound + ring_edges, normal=n)
    ext = bmesh.ops.extrude_edge_only(bm, edges=ring_edges)
    new_verts = [g for g in ext["geom"] if isinstance(g, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, verts=new_verts, vec=-n * depth)
    vs = set(new_verts)
    bottom = [g for g in ext["geom"] if isinstance(g, bmesh.types.BMEdge) and all(v in vs for v in g.verts)]
    bmesh.ops.contextual_create(bm, geom=bottom)  # fond du trou


def _drill(obj, pts, r, depth):
    """Perce des trous cylindriques (rayon r, profondeur depth de chaque cote du plan) dans obj."""
    cb = bmesh.new()
    for axis, p in pts:
        _cylinder(cb, p, axis, r, 2 * depth)
    cme = bpy.data.meshes.new("percage")
    cb.to_mesh(cme)
    cb.free()
    cutter = bpy.data.objects.new("percage", cme)
    bpy.context.scene.collection.objects.link(cutter)
    mod = obj.modifiers.new("trous", "BOOLEAN")
    mod.operation = "DIFFERENCE"
    mod.object = cutter
    mod.solver = os.environ.get("SC_BOOL_SOLVER", "EXACT")
    if mod.solver == "EXACT":
        # le morceau contient des pieces qui se chevauchent : il faut les options "auto-intersection"
        # et "tolerance aux trous", sinon le percage laisse des bords ouverts
        mod.use_self = os.environ.get("SC_BOOL_SELF", "1") == "1"
        mod.use_hole_tolerant = os.environ.get("SC_BOOL_HOLE", "1") == "1"
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.data.objects.remove(cutter, do_unlink=True)
    bpy.data.meshes.remove(cme)
    return len(pts)


def _write_pins(path, n, r, depth):
    """Goupilles a imprimer a part : un peu plus fines et plus courtes que les trous (jeu d'assemblage)."""
    from mathutils import Vector
    bm = bmesh.new()
    pr, pl = max(r - 0.15, 0.2), max(2 * depth - 0.5, 0.5)
    cols = max(1, int(math.ceil(math.sqrt(n))))
    for k in range(n):
        _cylinder(bm, Vector(((k % cols) * 4 * r, (k // cols) * 4 * r, pl / 2)), 2, pr, pl)
    me = bpy.data.meshes.new("goupilles")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("goupilles", me)
    bpy.context.scene.collection.objects.link(ob)
    bpy.ops.object.select_all(action="DESELECT")
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    bpy.ops.wm.stl_export(filepath=path, export_selected_objects=True)
    bpy.data.objects.remove(ob, do_unlink=True)


def analyze(args):
    """Inventaire des pieces d'un STL : lesquelles sont deja fermees (imprimables telles quelles).

    -- analyze MODELE.stl [--weld D]
    """
    src = os.path.abspath(args[0])
    weld = opt(args, "--weld", 0.01)
    progress(5, "import du STL")
    clear_scene()
    obj = import_stl(src)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    progress(25, "soudure des sommets")
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=weld)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    progress(45, "decoupage en pieces")
    groups = islands(bm)
    progress(70, f"analyse de {len(groups)} pieces")
    label = {}
    for gi, g in enumerate(groups):
        for f in g:
            label[f.index] = gi
    open_edges = [0] * len(groups)
    bad_edges = [0] * len(groups)
    for e in bm.edges:
        lf = e.link_faces
        if not lf:
            continue
        gi = label[lf[0].index]
        if len(lf) == 1:
            open_edges[gi] += 1
        elif len(lf) > 2:
            bad_edges[gi] += 1
    total = len(bm.faces)
    closed = [i for i in range(len(groups)) if open_edges[i] == 0 and bad_edges[i] == 0]
    opened = [i for i in range(len(groups)) if open_edges[i] > 0]
    fc = lambda ids: sum(len(groups[i]) for i in ids)
    biggest_open = sorted(opened, key=lambda i: -len(groups[i]))[:8]
    print("RESULT: " + json.dumps({
        "faces": total, "parts": len(groups),
        "closed_parts": len(closed), "closed_faces_pct": round(100 * fc(closed) / max(total, 1), 1),
        "open_parts": len(opened), "open_faces_pct": round(100 * fc(opened) / max(total, 1), 1),
        "only_bad_parts": len(groups) - len(closed) - len(opened),
        "open_edges": sum(open_edges), "bad_edges": sum(bad_edges),
        "biggest_open": [{"faces": len(groups[i]), "open_edges": open_edges[i]} for i in biggest_open]}))


def probe(args):
    """Essai voxel rapide a basse resolution : mesure la densite du modele.

    -- probe MODELE.stl --grid G [--adapt A]
    densite = faces produites / grille^2 ; quasi constante pour un modele donne, elle permet de prevoir
    le nombre de faces (donc la taille du STL et la RAM) pour n'importe quelle taille de voxel.
    """
    src = os.path.abspath(args[0])
    grid = opt(args, "--grid", 500.0)
    adapt = min(max(opt(args, "--adapt", 0.0), 0.0), 1.0)
    progress(5, "import du STL")
    clear_scene()
    obj = import_stl(src)
    faces_in = len(obj.data.polygons)
    voxel = max(obj.dimensions) / grid
    progress(40, f"essai voxel ({grid:.0f} cubes)")
    obj.data.remesh_voxel_size = voxel
    obj.data.remesh_voxel_adaptivity = adapt
    obj.data.remesh_mode = "VOXEL"
    bpy.ops.object.voxel_remesh()
    faces = len(obj.data.polygons)
    print("RESULT: " + json.dumps({"faces_in": faces_in, "grid": grid, "adapt": adapt, "faces": faces,
                                    "density": faces / grid ** 2, "extent": max(obj.dimensions)}))


def mesh_stats(me):
    """Memes compteurs que stats(), calcules sur le maillage directement (peu de memoire)."""
    import numpy as np
    le = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("edge_index", le)
    per_edge = np.bincount(le, minlength=len(me.edges))
    return {"verts": len(me.vertices), "faces": len(me.polygons),
            "non_manifold_edges": int(np.count_nonzero(per_edge != 2)),
            "boundary_edges": int(np.count_nonzero(per_edge == 1))}


def _summary(vals):
    if not vals:
        return {"mean": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    vals.sort()
    n = len(vals)
    return {"mean": round(sum(vals) / n, 4), "p95": round(vals[int(0.95 * (n - 1))], 4),
            "p99": round(vals[int(0.99 * (n - 1))], 4), "max": round(vals[-1], 4)}


def deviation(src_path, result_obj, threshold, max_samples=150000):
    """Ecart geometrique entre l'original (reimporte depuis src_path) et le resultat (unite du STL).

    - moved : distance de chaque point du resultat a la surface d'origine (surface deplacee/lissee).
    - lost  : pour chaque point de l'original situe HORS du resultat, sa distance au resultat
              (details en saillie supprimes : antennes, aretes fines...). Les points a l'interieur
              (structures internes, pieces imbriquees) sont ignores : leur disparition est voulue.
    """
    from mathutils.bvhtree import BVHTree
    res_tree = BVHTree.FromObject(result_obj, bpy.context.evaluated_depsgraph_get())
    orig = import_stl(src_path)
    orig_tree = BVHTree.FromObject(orig, bpy.context.evaluated_depsgraph_get())

    rv = result_obj.data.vertices
    step = max(1, len(rv) // max_samples)
    moved = []
    for i in range(0, len(rv), step):
        hit = orig_tree.find_nearest(rv[i].co)
        if hit[0] is not None:
            moved.append(hit[3])

    ov = orig.data.vertices
    step = max(1, len(ov) // max_samples)
    lost, sampled, over = [], 0, 0
    for i in range(0, len(ov), step):
        p = ov[i].co
        loc, nor, _idx, dist = res_tree.find_nearest(p)
        if loc is None:
            continue
        sampled += 1
        if (p - loc).dot(nor) > 0:  # point hors du volume repare
            lost.append(dist)
            if dist > threshold:
                over += 1
    del orig_tree
    bpy.data.objects.remove(orig, do_unlink=True)
    return {"moved": _summary(moved), "lost": _summary(lost), "threshold": threshold,
            "lost_share": round(100.0 * over / sampled, 2) if sampled else 0.0}


def render(args):
    src, prefix = os.path.abspath(args[0]), os.path.abspath(args[1])
    size = int(opt(args, "--size", 900))
    clear_scene()
    obj = import_stl(src)
    out, ext = render_obj(obj, prefix, size)
    print("RESULT: " + json.dumps({"images": out, "extents": ext}))


def heat_color(t):
    """0 -> gris (aucune perte), puis jaune -> orange -> rouge (perte >= 1)."""
    if t <= 0:
        return (0.72, 0.78, 0.86, 1.0)
    t = min(t, 1.0)
    stops = [(0.0, (1.0, 0.95, 0.2)), (0.5, (1.0, 0.5, 0.05)), (1.0, (0.9, 0.05, 0.05))]
    for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0)
            return tuple(c0[i] + (c1[i] - c0[i]) * k for i in range(3)) + (1.0,)
    return stops[-1][1] + (1.0,)


def heatmap(args):
    """Colore l'ORIGINAL selon la matiere perdue par la reparation (points hors du modele repare).

    -- heatmap ORIGINAL.stl REPARE.stl PREFIXE --threshold T [--size N]
    gris : rien de perdu ; jaune : perte ~ T ; rouge : perte >= 4 T
    """
    from mathutils.bvhtree import BVHTree
    src, rep, prefix = (os.path.abspath(a) for a in args[:3])
    thr = opt(args, "--threshold", 1.0)
    size = int(opt(args, "--size", 640))
    clear_scene()
    progress(2, "import du modele repare")
    robj = import_stl(rep)
    tree = BVHTree.FromObject(robj, bpy.context.evaluated_depsgraph_get())
    bpy.data.objects.remove(robj, do_unlink=True)
    progress(15, "import de l'original")
    obj = import_stl(src)
    me = obj.data
    n = len(me.vertices)
    progress(25, f"mesure de {n} points")
    loss = [0.0] * n
    for i, v in enumerate(me.vertices):
        loc, nor, _idx, dist = tree.find_nearest(v.co)
        if loc is not None and (v.co - loc).dot(nor) > 0 and dist > thr * 0.25:
            loss[i] = dist
        if i % 100000 == 0 and i:
            progress(25 + int(35 * i / n), f"mesure {i}/{n} points")
    progress(60, "coloration")
    attr = me.color_attributes.new(name="perte", type="FLOAT_COLOR", domain="POINT")
    span = thr * 4
    for i, d in enumerate(loss):
        attr.data[i].color = heat_color(d / span if d else 0.0)
    me.color_attributes.active_color = attr
    over = sum(1 for d in loss if d > thr)
    out, ext = render_obj(obj, prefix, size, color="VERTEX", pct0=65)
    print("RESULT: " + json.dumps({"images": out, "extents": ext, "threshold": thr,
                                    "lost_share": round(100.0 * over / max(n, 1), 2)}))


def render_turntable(obj, prefix, size, color="SINGLE", n_az=24, elevs=(-20, 25, 65), pct0=0):
    """Images tout autour du modele (apercu qu'on fait tourner a la souris).

    Azimut 0 = profil droit (+X), 90 = avant (le nez des vaisseaux est vers +Y). Cadrage fixe sur la
    sphere englobante : le modele ne 'saute' pas d'une image a l'autre. Plus une vue de dessus.
    """
    from mathutils import Matrix
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.render.image_settings.file_format = "PNG"
    sh = scene.display.shading
    sh.light, sh.color_type = "STUDIO", color
    sh.single_color = (0.72, 0.78, 0.86)
    sh.show_cavity, sh.show_shadows = True, False
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("w")
    world.color = (0.09, 0.10, 0.12)
    scene.world = world
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(c[i] for c in corners) for i in range(3)))
    hi = Vector((max(c[i] for c in corners) for i in range(3)))
    center, diag = (lo + hi) / 2, (hi - lo).length
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type, cam_data.ortho_scale, cam_data.clip_end = "ORTHO", diag * 1.02, diag * 10
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam

    def shoot(d, up, path):
        loc = center + d.normalized() * diag * 2
        look = (center - loc).normalized()
        right = look.cross(up).normalized()
        cam.matrix_world = Matrix.Translation(loc) @ Matrix((right, right.cross(look), -look)).transposed().to_4x4()
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)

    frames, total, k = [], n_az * len(elevs) + 1, 0
    for ei, el in enumerate(elevs):
        for ai in range(n_az):
            az = math.radians(360.0 * ai / n_az)
            e = math.radians(el)
            progress(pct0 + int((100 - pct0) * k / total), f"apercu 3D {k + 1}/{total}")
            path = f"{prefix}_e{ei}_a{ai}.png"
            shoot(Vector((math.cos(e) * math.cos(az), math.cos(e) * math.sin(az), math.sin(e))),
                  Vector((0, 0, 1)), path)
            frames.append([ei, ai, path])
            k += 1
    top = f"{prefix}_top.png"
    shoot(Vector((0, 0, 1)), Vector((0, 1, 0)), top)
    return {"frames": frames, "top": top, "n_az": n_az, "elevs": list(elevs),
            "extents": [round(hi[i] - lo[i], 3) for i in range(3)]}


def turntable(args):
    """-- turntable MODELE.stl PREFIXE [--size N] [--az N]"""
    src, prefix = os.path.abspath(args[0]), os.path.abspath(args[1])
    clear_scene()
    obj = import_stl(src)
    big = len(obj.data.polygons) > 2_000_000  # tres gros modeles : moins d'images, apercu plus rapide
    n_az = int(opt(args, "--az", 16 if big else 24))
    res = render_turntable(obj, prefix, int(opt(args, "--size", 420)), n_az=n_az,
                           elevs=(-20, 30) if big else (-20, 25, 65))
    print("RESULT: " + json.dumps(res))


def render_obj(obj, prefix, size, color="SINGLE", pct0=0):
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.render.resolution_x = scene.render.resolution_y = size
    scene.render.image_settings.file_format = "PNG"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = color
    sh.single_color = (0.72, 0.78, 0.86)
    sh.show_cavity = True
    sh.show_shadows = False
    scene.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("w")
    world.color = (0.09, 0.10, 0.12)
    scene.world = world

    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(c[i] for c in corners) for i in range(3)))
    hi = Vector((max(c[i] for c in corners) for i in range(3)))
    center = (lo + hi) / 2
    diag = (hi - lo).length
    cam_data = bpy.data.cameras.new("cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = diag * 0.92
    cam_data.clip_end = diag * 10
    cam = bpy.data.objects.new("cam", cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    # les vaisseaux importes depuis le GLB ont le nez vers +Y (glTF -Z -> Blender +Y)
    views = {
        "iso": Vector((1.0, 1.0, 0.75)),
        "front": Vector((0.0, 1.0, 0.0)),
        "back": Vector((0.0, -1.0, 0.0)),
        "side": Vector((1.0, 0.0, 0.0)),
        "top": Vector((0.0, 0.0, 1.0)),
    }
    out = []
    for vi, (name, d) in enumerate(views.items()):
        progress(pct0 + int((100 - pct0) * vi / len(views)), f"vue {name}")
        d = d.normalized()
        cam.location = center + d * diag * 2
        up = Vector((0, 1, 0)) if name == "top" else Vector((0, 0, 1))
        from mathutils import Matrix
        look = (center - cam.location).normalized()
        right = look.cross(up).normalized()
        true_up = right.cross(look).normalized()
        m = Matrix((right, true_up, -look)).transposed().to_4x4()
        cam.matrix_world = Matrix.Translation(cam.location) @ m
        # cadrage serre : emprise projetee de la boite englobante dans cette vue
        w = max(abs((c - center).dot(right)) for c in corners) * 2
        h = max(abs((c - center).dot(true_up)) for c in corners) * 2
        cam_data.ortho_scale = max(w, h) * 1.08
        path = f"{prefix}_{name}.png"
        scene.render.filepath = path
        bpy.ops.render.render(write_still=True)
        out.append(path)
    return out, [round(hi[i] - lo[i], 3) for i in range(3)]


def glb2stl(args):
    """GLB -> STL. L'import glTF de Blender convertit deja Y-up en Z-up et applique la hierarchie."""
    import math
    src, dst = os.path.abspath(args[0]), os.path.abspath(args[1])
    scale = opt(args, "--scale", 1.0)
    zup = int(opt(args, "--zup", 1, int))
    clear_scene()
    bpy.ops.import_scene.gltf(filepath=src)
    # elements retires a la demande : tout ce qui est accroche sous le point d'attache correspondant
    removed = 0
    groups = [g for g in (args[args.index("--remove") + 1].split(",") if "--remove" in args else []) if g]
    if groups:
        rx = re.compile("|".join(REMOVE_RX[g] for g in groups if g in REMOVE_RX), re.IGNORECASE)

        def under_removed(o):
            while o is not None:
                if rx.match(o.name):
                    return True
                o = o.parent
            return False
        doomed = [o for o in bpy.data.objects if o.type == "MESH" and under_removed(o)]
        removed = len(doomed)
        for o in doomed:
            bpy.data.objects.remove(o, do_unlink=True)
    # maillages d'etat endommage (ex. Arrow_Thruster_Main_DMG) : superposes aux pieces intactes
    dmg = [o for o in bpy.data.objects if o.type == "MESH" and DMG_RE.search(o.name)]
    bpy.context.view_layer.update()
    for o in dmg:
        for c in list(o.children):  # garde la position monde des eventuels enfants
            mw = c.matrix_world.copy()
            c.parent = None
            c.matrix_world = mw
        bpy.data.objects.remove(o, do_unlink=True)
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    if not meshes:
        print("RESULT: " + json.dumps({"error": "le GLB ne contient aucune surface"}))
        return
    if not zup:  # revient au Y-up d'origine
        root = bpy.data.objects.new("root", None)
        bpy.context.scene.collection.objects.link(root)
        for o in [o for o in bpy.data.objects if o.parent is None and o is not root]:
            o.parent = root
        root.rotation_euler = (math.radians(-90), 0, 0)
    bpy.context.view_layer.update()
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    faces = 0
    for o in meshes:
        faces += len(o.data.polygons)
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            for i in range(3):
                lo[i] = min(lo[i], w[i])
                hi[i] = max(hi[i], w[i])
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.ops.wm.stl_export(filepath=dst, export_selected_objects=True, global_scale=scale)
    print("RESULT: " + json.dumps({"faces": faces, "extents": [round((hi[i] - lo[i]) * scale, 3) for i in range(3)],
                                    "watertight": None, "damage_removed": len(dmg), "parts_removed": removed}))


def main():
    a = argv()
    if not a:
        print("usage: -- repair|render|glb2stl ...")
        return
    {"repair": repair, "render": render, "glb2stl": glb2stl, "heatmap": heatmap, "probe": probe,
     "analyze": analyze, "precise": precise, "split": split, "hollow": hollow,
     "turntable": turntable}[a[0]](a[1:])


main()
