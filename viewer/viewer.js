import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js';

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CLS = JSON.parse(document.getElementById('clsdata').textContent);
const BASE = window.V3_BASE || '/outils/salvage/ships';
const DRACO = (window.V3_DRACO || 'https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/libs/draco/gltf/');
const KINDS = {
  powerplant:['Générateur','#ff6b3d',1], cooler:['Refroidisseur','#3db8ff',1], shield:['Bouclier','#9d8cff',1], quantum:['Moteur quantique','#d46bff',1],
  weapon:['Arme / râtelier','#ff3d6b',1], turret:['Tourelle','#ffb13d',1], missile:['Missiles','#ff9a9a',1],
  door:['Porte','#3dff8a',1], elevator:['Ascenseur','#fff03d',1], airlock:['Sas','#3dffe8',1], ramp:['Rampe','#a6ff3d',1], ladder:['Échelle','#e6e6e6',1],
  cargo:['Soute','#e0c060',1], seat:['Siège','#7aa0c0',0], bed:['Lit','#c8a47a',0], terminal:['Écran / terminal','#6bd0ff',0], other:['Autre','#8a8a8a',0]
};
const ORDER = Object.keys(KINDS);
// sorties : rampes, sas, portes extérieures, ascenseurs d'embarquement
const EXIT_RE = /exterior|boarding|entrance|personnel|rearelevator|hatch|ramp|airlock|docking|garage|eva_door|elevator_door/i, NOT_EXIT_RE = /blastshield|fuelport|_inner|canopy/i;
function isExit(m){
  if (m.room) return false; // morceau de décor d'intérieur, pas un vrai passage
  if (m.kind === 'ramp' || m.kind === 'airlock') return true;
  const t = (m.item || '') + ' ' + m.name;
  if (m.kind === 'door' || m.kind === 'elevator') return EXIT_RE.test(t) && !NOT_EXIT_RE.test(t);
  return false;
}
// pièces de circulation
const COR_RE = /corridor|hallway|passage|walkway|foyer|entrance|atrium|lift|elevator|airlock|stair/i;
let corGroup = null, exitGroup = null, rooms = null, pathGroup = null;
const ROOM_COR_RE = /corridor|entrance|airlock|elevator|ladder|hangar/i;
const pathMat = new THREE.MeshBasicMaterial({color:0xffa726, transparent:true, opacity:0.22, depthWrite:false, depthTest:false});
const pathEdge = new THREE.LineBasicMaterial({color:0xffa726, transparent:true, opacity:0.8, depthTest:false});
const pathLine = new THREE.LineBasicMaterial({color:0xffd54f, depthTest:false});
const _v = new THREE.Vector3(), _q = new THREE.Quaternion();
// le point est-il dans la pièce ? (boîtes orientées : centre c, demi-tailles h, rotation q)
function inRoom(r, p, pad = 0.15){
  for (const b of r.boxes){
    _v.set(p[0] - b.c[0], p[1] - b.c[1], p[2] - b.c[2]).applyQuaternion(_q.set(...b.q).invert());
    if (Math.abs(_v.x) <= b.h[0] + pad && Math.abs(_v.y) <= b.h[1] + pad && Math.abs(_v.z) <= b.h[2] + pad) return true;
  }
  return false;
}
function roomOf(p){
  if (!rooms) return null;
  let r = rooms.rooms.find(x => inRoom(x, p)); if (r) return r;
  let best = null, bd = 4;
  for (const x of rooms.rooms) for (const b of x.boxes){ const d = Math.hypot(p[0] - b.c[0], p[1] - b.c[1], p[2] - b.c[2]) - Math.max(...b.h); if (d < bd){ bd = d; best = x; } }
  return best;
}
const realExit = l => l.b === 'exterior' && (l.room_connector || l.inferred || l.kind === 'ramp');
// plus court chemin (en nombre de passages) de la pièce de départ jusqu'à une vraie sortie
function pathToExit(start){
  if (!rooms || !start) return null;
  const prev = new Map([[start.id, null]]), queue = [start.id];
  while (queue.length){
    const id = queue.shift();
    for (const l of rooms.links){
      if ((l.a === id || l.b === id) && realExit(l) && l.a === id){
        const steps = [l]; let cur = id;
        while (prev.get(cur)){ const [pl, pid] = prev.get(cur); steps.unshift(pl); cur = pid; }
        return steps;
      }
    }
    for (const l of rooms.links){
      if (l.b === 'exterior' || l.a === 'exterior') continue;
      const nx = l.a === id ? l.b : l.b === id ? l.a : null;
      if (nx && !prev.has(nx)){ prev.set(nx, [l, id]); queue.push(nx); }
    }
  }
  return null;
}
const roomById = id => rooms && rooms.rooms.find(r => r.id === id);
function roomBoxes(r, mat, edge, group){
  for (const b of r.boxes){
    const g = new THREE.BoxGeometry(b.h[0] * 2, b.h[1] * 2, b.h[2] * 2);
    const m = new THREE.Mesh(g, mat); m.position.set(...b.c); m.quaternion.set(...b.q); m.renderOrder = 5;
    const e = new THREE.LineSegments(new THREE.EdgesGeometry(g), edge); e.position.copy(m.position); e.quaternion.copy(m.quaternion); e.renderOrder = 6;
    group.add(m, e);
  }
}
function showPath(m){
  if (pathGroup){ scene.remove(pathGroup); pathGroup = null; }
  if (!rooms || !$('v3path').checked) return '';
  const start = roomOf(m.pos);
  if (!start) return '';
  let txt = `Pièce : <b>${esc(start.label || start.name)}</b>`;
  const steps = m.exit ? [] : pathToExit(start);
  if (m.exit) return txt;
  if (!steps){ return txt + '<br><span class="fine">Pas de sortie trouvée depuis cette pièce dans les données.</span>'; }
  pathGroup = new THREE.Group();
  pathMat.clippingPlanes = [clip]; pathEdge.clippingPlanes = [clip];
  const ids = new Set([start.id]); for (const l of steps){ ids.add(l.a); if (l.b !== 'exterior') ids.add(l.b); }
  for (const id of ids){ const r = roomById(id); if (r) roomBoxes(r, pathMat, pathEdge, pathGroup); }
  const pts = [new THREE.Vector3(...m.pos)];
  for (const l of steps) if (l.pos) pts.push(new THREE.Vector3(...l.pos));
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), pathLine); line.renderOrder = 12;
  pathGroup.add(line);
  scene.add(pathGroup);
  const last = steps[steps.length - 1];
  const via = []; let cur = start.id;
  for (const l of steps){ if (l.b === 'exterior') break; cur = l.a === cur ? l.b : l.a; const r = roomById(cur); if (r) via.push(esc(r.label || r.name)); }
  return txt + `<br><span style="color:#ffd54f">Sortie la plus proche : ${steps.length} passage${steps.length > 1 ? 's' : ''}</span>${last.kind ? ' · par ' + esc(KINDS[last.kind] ? KINDS[last.kind][0].toLowerCase() : last.kind) : ''}${via.length ? '<br><small>via ' + via.join(' → ') + '</small>' : ''}`;
}
const corMat = new THREE.MeshBasicMaterial({color:0x2fd4ff, transparent:true, opacity:0.18, depthWrite:false, depthTest:false, clippingPlanes:[]});
const corEdge = new THREE.LineBasicMaterial({color:0x2fd4ff, transparent:true, opacity:0.55, depthTest:false});
const exitMat = new THREE.MeshBasicMaterial({color:0xffffff, transparent:true, opacity:0.9, depthTest:false, side:THREE.DoubleSide});
const show = {}; for (const k in KINDS) show[k] = !!KINDS[k][2];
try { Object.assign(show, JSON.parse(localStorage.getItem('salvage.v3show') || '{}')); } catch (e) {}

let skinGroup = null, skinState = '', curEntity = '';
let renderer, scene, camera, controls, hullGroup = null, markGroup = null, data = null, LIST = [], selected = null, bbox = null;
const clip = new THREE.Plane(new THREE.Vector3(0, -1, 0), 1e6);
const hullMat = new THREE.MeshStandardMaterial({color:0x9fb3c8, metalness:0.1, roughness:0.8, transparent:true, opacity:0.25, depthWrite:false, side:THREE.DoubleSide, clippingPlanes:[clip]});

function init(){
  if (renderer) return;
  const box = $('v3view');
  renderer = new THREE.WebGLRenderer({antialias:true});
  renderer.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
  renderer.localClippingEnabled = true;
  box.appendChild(renderer.domElement);
  scene = new THREE.Scene(); scene.background = new THREE.Color(0x0b1118);
  scene.add(new THREE.HemisphereLight(0xdde8ff, 0x202830, 1.2));
  const d = new THREE.DirectionalLight(0xffffff, 1.1); d.position.set(1, 2, 1.5); scene.add(d);
  camera = new THREE.PerspectiveCamera(45, 1, 0.05, 5000);
  controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true;
  new ResizeObserver(resize).observe(box); resize();
  const cv = renderer.domElement;
  cv.addEventListener('pointerdown', e => { cv._down = [e.clientX, e.clientY]; });
  cv.addEventListener('pointerup', e => { const d0 = cv._down; if (d0 && Math.hypot(e.clientX - d0[0], e.clientY - d0[1]) < 5) pick(e, true); });
  cv.addEventListener('pointermove', e => pick(e, false));
  cv.addEventListener('pointerleave', () => { $('v3tip').hidden = true; });
  const loop = () => { requestAnimationFrame(loop); controls.update(); if (exitGroup){ const k = 1 + 0.18 * Math.sin(performance.now() / 260); for (const o of exitGroup.children) o.scale.setScalar(k); } renderer.render(scene, camera); };
  loop();
}
function resize(){
  const box = $('v3view'), w = box.clientWidth || 600, h = box.clientHeight || 420;
  renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
}
const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
function pick(e, click){
  if (!markGroup) return;
  const r = renderer.domElement.getBoundingClientRect();
  ndc.set((e.clientX - r.left) / r.width * 2 - 1, -(e.clientY - r.top) / r.height * 2 + 1);
  ray.setFromCamera(ndc, camera);
  const hits = ray.intersectObjects(markGroup.children.filter(o => o.visible), false);
  const tip = $('v3tip');
  if (!hits.length){ tip.hidden = true; renderer.domElement.style.cursor = ''; return; }
  const m = hits[0].object.userData.m;
  renderer.domElement.style.cursor = 'pointer';
  tip.hidden = false; tip.textContent = KINDS[m.kind][0] + ' · ' + (m.item ? niceItem(m) : m.name);
  tip.style.left = (e.clientX - r.left + 12) + 'px'; tip.style.top = (e.clientY - r.top + 12) + 'px';
  if (click) select(m, false);
}
const niceItem = m => (m.item || '').replace(/_SCItem$/i, '').replace(/_/g, ' ');
function select(m, focus){
  selected = m;
  for (const o of markGroup.children) o.scale.setScalar(o.userData.m === m ? 1.8 : 1);
  $('v3info').innerHTML = `<b style="color:${KINDS[m.kind][1]}">${esc(KINDS[m.kind][0])}</b>${m.size ? ' · S' + m.size : ''}<br>${m.item ? esc(niceItem(m)) + '<br>' : ''}${m.exit ? '<span class="v3exit">Sortie du vaisseau</span><br>' : ''}<small>${esc(m.name)}${m._n ? ` (+${m._n - 1} éléments proches regroupés)` : ''}</small><br><small>x ${m.pos[0].toFixed(1)} · y ${m.pos[1].toFixed(1)} · z ${m.pos[2].toFixed(1)} m</small>`;
  const ptxt = showPath(m); if (ptxt) $('v3info').insertAdjacentHTML('beforeend', '<div class="v3room">' + ptxt + '</div>');
  document.querySelectorAll('#v3list [data-id]').forEach(b => b.classList.toggle('on', b.dataset.id === m._i));
  if (focus){
    const p = new THREE.Vector3(...m.pos), dir = camera.position.clone().sub(controls.target).normalize();
    const dist = Math.max(6, (bbox ? bbox.getSize(new THREE.Vector3()).length() : 40) * 0.3);
    controls.target.copy(p); camera.position.copy(p.clone().add(dir.multiplyScalar(dist)));
    // la coupe suit le repère pour voir l'intérieur du pont
    if (bbox){ const span = bbox.max.y - bbox.min.y; $('v3cut').value = Math.min(1000, Math.round((m.pos[1] + 1.2 - bbox.min.y) / span * 1000)); applyCut(); }
  }
}
function buildOverlays(rad, size){
  if (corGroup) scene.remove(corGroup);
  if (exitGroup) scene.remove(exitGroup);
  corMat.clippingPlanes = [clip]; corEdge.clippingPlanes = [clip];
  corGroup = new THREE.Group(); exitGroup = new THREE.Group();
  // couloirs : morceaux d'intérieur des pièces de circulation (sinon la boîte de la zone)
  let boxes = rooms ? [] : data.pieces.filter(p => COR_RE.test(p.room));
  if (rooms){ const cr = rooms.rooms.filter(r => ROOM_COR_RE.test(r.kind || '')); for (const r of cr) roomBoxes(r, corMat, corEdge, corGroup); boxes = cr; }
  else if (boxes.length < 3) boxes = data.zones.filter(z => COR_RE.test(z.name.slice(5)));
  const big = size * 0.5;
  for (const b of (rooms ? [] : boxes)){
    const e = b.extent.map(v => Math.max(0.05, v));
    if (Math.max(...e) > big) continue;
    const g = new THREE.BoxGeometry(...e);
    const box = new THREE.Mesh(g, corMat); box.position.set(...b.pos); box.renderOrder = 5;
    const ed = new THREE.LineSegments(new THREE.EdgesGeometry(g), corEdge); ed.position.copy(box.position); ed.renderOrder = 6;
    corGroup.add(box, ed);
  }
  const ring = new THREE.SphereGeometry(rad * 2.6, 14, 10);
  for (const m of data.markers) if (m.exit){
    const r = new THREE.Mesh(ring, exitMat); r.material.wireframe = true; r.position.set(...m.pos); r.renderOrder = 11; r.userData.m = m;
    exitGroup.add(r);
  }
  // sorties du jeu sans repère associé : anneau à la position du passage
  if (rooms) for (const l of rooms.links) if (realExit(l) && !(l.marker && data.byId[l.marker]) && l.pos){
    const r = new THREE.Mesh(ring, exitMat); r.material.wireframe = true; r.position.set(...l.pos); r.renderOrder = 11; r.userData.m = {pos:l.pos};
    exitGroup.add(r);
  }
  scene.add(corGroup, exitGroup);
  $('v3corn').textContent = boxes.length ? '' : '(pas de couloir connu)';
  $('v3exitn').textContent = `(${exitGroup.children.length})`;
  applyOverlays();
}
function applyOverlays(){
  if (corGroup) corGroup.visible = $('v3cor').checked;
  if (exitGroup){
    const f = $('v3cut').value / 1000, y = bbox ? bbox.min.y + (bbox.max.y - bbox.min.y) * f : 1e9;
    exitGroup.visible = $('v3exit').checked;
    for (const o of exitGroup.children) o.visible = f >= 1 || o.userData.m.pos[1] <= y + 0.05;
  }
}
function applyCut(){
  if (!bbox) return;
  const f = $('v3cut').value / 1000, y = bbox.min.y + (bbox.max.y - bbox.min.y) * f;
  clip.constant = f >= 1 ? 1e6 : y;
  $('v3cutv').textContent = f >= 1 ? '(aucune)' : `(${y.toFixed(1)} m)`;
  if (markGroup) for (const o of markGroup.children) o.visible = show[o.userData.m.kind] && (f >= 1 || o.userData.m.pos[1] <= y + 0.05);
  applyOverlays();
}
function applyOpacity(){
  const v = $('v3op').value / 100;
  hullMat.opacity = v; hullMat.transparent = v < 1; hullMat.depthWrite = v >= 1; hullMat.needsUpdate = true;
  const info = LIST.find(x => x.entity === curEntity), hasSkin = !!(info && info.skin);
  $('v3opv').textContent = `(${Math.round(v * 100)} %${hasSkin ? (v >= 1 ? ' · aspect réel' : ' · 100 % = aspect réel') : ''})`;
  // à 100 % : la coque texturée du jeu (skin.glb) si elle existe
  const wantSkin = hasSkin && v >= 1;
  if (wantSkin && !skinGroup && skinState !== 'loading' && skinState !== 'error') loadSkin(curEntity);
  if (skinGroup) skinGroup.visible = wantSkin;
  if (hullGroup) hullGroup.visible = !(wantSkin && skinGroup);
}
async function loadSkin(entity){
  skinState = 'loading'; $('v3opv').textContent = '(chargement de l\'aspect réel…)';
  try {
    const draco = new DRACOLoader(); draco.setDecoderPath(DRACO);
    const loader = new GLTFLoader(); loader.setDRACOLoader(draco);
    const g = await loader.loadAsync(`${BASE}/${encodeURIComponent(entity)}/skin.glb`);
    if (entity !== curEntity) return;
    g.scene.traverse(o => { if (o.isMesh){ for (const m of [].concat(o.material)){ m.clippingPlanes = [clip]; m.needsUpdate = true; } } });
    skinGroup = g.scene; scene.add(skinGroup); skinState = 'ok';
  } catch (e) { skinState = 'error'; }
  applyOpacity();
}
function legend(){
  const n = {}; for (const m of data.markers) n[m.kind] = (n[m.kind] || 0) + 1;
  $('v3legend').innerHTML = ORDER.filter(k => n[k]).map(k => `<label><input type="checkbox" data-k="${k}"${show[k] ? ' checked' : ''}><i style="background:${KINDS[k][1]}"></i>${esc(KINDS[k][0])} <small>${n[k]}</small></label>`).join('');
  const rows = data.markers.filter(m => show[m.kind] || m.exit).sort((a, b) => (b.exit - a.exit) || ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind) || (a.item || a.name).localeCompare(b.item || b.name));
  $('v3list').innerHTML = rows.map(m => `<button type="button" data-id="${m._i}"${selected === m ? ' class="on"' : ''}><i style="background:${KINDS[m.kind][1]}"></i>${m.exit ? '<b class="v3exit">Sortie</b> ' : ''}${esc(KINDS[m.kind][0])}${m.size ? ' S' + m.size : ''} <small>${esc(m.item ? niceItem(m) : m.name)}</small></button>`).join('') || '<span class="fine">Aucun repère pour les types cochés.</span>';
}
async function load(entity){
  init();
  $('v3empty').hidden = false; $('v3empty').textContent = 'Chargement du vaisseau…';
  try {
    const r = await fetch(`${BASE}/${encodeURIComponent(entity)}/markers.json`, {cache:'no-cache'});
    if (!r.ok) throw new Error('repères ' + r.status);
    data = await r.json();
    for (const m of data.markers) if (!KINDS[m.kind]) m.kind = 'other';
    // zones (pièces) et morceaux d'intérieur : servent au surlignage des couloirs, pas comme repères
    data.zones = data.markers.filter(m => m.name.startsWith('zone:'));
    data.pieces = data.markers.filter(m => m.room && m.extent && !m.name.startsWith('zone:'));
    data.markers = data.markers.filter(m => !m.name.startsWith('zone:'));
    // les morceaux d'un même ascenseur / d'une même rampe / d'une même soute (murs, plafond, plateformes…) sont regroupés en un seul repère
    const GROUP = {elevator:4, ramp:2.5, airlock:3, cargo:1.0, ladder:1.0};
    const kept = [], mergedAll = [];
    for (const m of data.markers){
      const g = GROUP[m.kind];
      const near = g != null ? kept.find(k => k.kind === m.kind && !!k.room === !!m.room && Math.hypot(k.pos[0] - m.pos[0], k.pos[1] - m.pos[1], k.pos[2] - m.pos[2]) < g) : null;
      if (near){ near._n = (near._n || 1) + 1; if (!near.item && m.item) near.item = m.item; m._into = near; mergedAll.push(m); continue; }
      kept.push(m);
    }
    data.markers = kept;
    data.markers.forEach((m, i) => { m._i = String(i); m.exit = isExit(m); });
    // pièces et passages du jeu (rooms.json) : les vraies sorties remplacent la déduction par les noms
    data.byId = {}; for (const m of data.markers) data.byId[m.id] = m;
    for (const m of mergedAll) if (!data.byId[m.id] && m._into) data.byId[m.id] = m._into;
    rooms = null;
    const info = LIST.find(x => x.entity === entity);
    if (info && info.rooms){ try { const rr = await fetch(`${BASE}/${encodeURIComponent(entity)}/rooms.json`, {cache:'no-cache'}); if (rr.ok) rooms = await rr.json(); } catch (e) {} }
    if (rooms && rooms.rooms && rooms.rooms.length){
      for (const m of data.markers) m.exit = false;
      for (const l of rooms.links) if (realExit(l) && l.marker && data.byId[l.marker]) data.byId[l.marker].exit = true;
    } else rooms = null;
    const draco = new DRACOLoader(); draco.setDecoderPath(DRACO);
    const loader = new GLTFLoader(); loader.setDRACOLoader(draco);
    const gltf = await loader.loadAsync(`${BASE}/${encodeURIComponent(entity)}/hull.glb`);
    if (hullGroup) scene.remove(hullGroup);
    if (markGroup) scene.remove(markGroup);
    if (skinGroup){ scene.remove(skinGroup); skinGroup = null; }
    skinState = ''; curEntity = entity;
    if (pathGroup){ scene.remove(pathGroup); pathGroup = null; }
    hullGroup = gltf.scene;
    hullGroup.traverse(o => { if (o.isMesh){ o.material = hullMat; o.renderOrder = 0; } });
    scene.add(hullGroup);
    bbox = new THREE.Box3().setFromObject(hullGroup);
    if (bbox.isEmpty() && data.bbox) bbox = new THREE.Box3(new THREE.Vector3(...data.bbox[0]), new THREE.Vector3(...data.bbox[1]));
    const size = bbox.getSize(new THREE.Vector3()).length();
    markGroup = new THREE.Group();
    const geo = new THREE.SphereGeometry(Math.max(0.12, size * 0.0045), 16, 12), mats = {};
    for (const m of data.markers){
      const mat = mats[m.kind] || (mats[m.kind] = new THREE.MeshBasicMaterial({color:KINDS[m.kind][1], depthTest:false, transparent:true, opacity:0.95}));
      const s = new THREE.Mesh(geo, mat); s.position.set(...m.pos); s.renderOrder = 10; s.userData.m = m;
      markGroup.add(s);
    }
    scene.add(markGroup);
    buildOverlays(Math.max(0.12, size * 0.0045), size);
    const c = bbox.getCenter(new THREE.Vector3());
    controls.target.copy(c);
    const fit = Math.max(1, 1.15 / (camera.aspect || 1)); // écran étroit (téléphone) : on recule pour voir tout le vaisseau
    camera.position.copy(c.clone().add(new THREE.Vector3(size * 0.55, size * 0.4, size * 0.65).multiplyScalar(fit)));
    camera.near = Math.max(0.02, size / 2000); camera.far = size * 20; camera.updateProjectionMatrix();
    selected = null; $('v3info').innerHTML = `<b>${esc(CLS[entity] || entity)}</b><br><span class="fine">${data.markers.length} repères. Clique sur un repère (ou dans la liste) pour voir ce que c'est. Glisser = tourner · molette = zoom · clic droit = déplacer.</span>`;
    $('v3cut').value = 1000; applyCut(); applyOpacity(); legend();
    $('v3empty').hidden = true;
  } catch (e) {
    $('v3empty').hidden = false; $('v3empty').textContent = 'Impossible de charger ce vaisseau (' + e.message + ').';
  }
}
async function refreshList(pick){
  try { const r = await fetch(BASE, {cache:'no-store'}); LIST = r.ok ? await r.json() : []; } catch (e) { LIST = []; }
  window.V3LIST = LIST.map(x => x.entity);
  const sel = $('v3ship'), cur = pick || sel.value;
  sel.innerHTML = LIST.length ? '<option value="">— Choisir un vaisseau —</option>' + LIST.map(x => `<option value="${esc(x.entity)}">${esc(CLS[x.entity] || x.entity)} · ${x.markers} repères</option>`).join('') : '<option value="">Aucun vaisseau importé</option>';
  if (cur && LIST.some(x => x.entity === cur)){ sel.value = cur; load(cur); }
  document.dispatchEvent(new Event('v3list'));
}
$('v3ship').addEventListener('change', e => { if (e.target.value) load(e.target.value); });
$('v3legend').addEventListener('change', e => { const k = e.target.dataset.k; if (!k) return; show[k] = e.target.checked; try { localStorage.setItem('salvage.v3show', JSON.stringify(show)); } catch (x) {} applyCut(); legend(); });
$('v3list').addEventListener('click', e => { const b = e.target.closest('[data-id]'); if (!b) return; const m = data.markers[+b.dataset.id]; if (m) select(m, true); });
$('v3cut').addEventListener('input', applyCut);
$('v3cor').addEventListener('change', applyOverlays);
$('v3exit').addEventListener('change', applyOverlays);
$('v3path').addEventListener('change', () => { if (selected) select(selected, false); else if (pathGroup){ scene.remove(pathGroup); pathGroup = null; } });
$('v3op').addEventListener('input', applyOpacity);
$('v3import').addEventListener('click', () => $('v3files').click());
$('v3files').addEventListener('change', async e => {
  const files = [...e.target.files].filter(f => /^(hull\.glb|markers\.json|report\.txt)$/.test(f.name));
  e.target.value = '';
  if (!files.length){ $('v3msg').textContent = 'Aucun hull.glb / markers.json dans ce dossier.'; return; }
  let ok = 0, ko = 0, last = '';
  for (const f of files){
    const parts = (f.webkitRelativePath || f.name).split('/'); const entity = parts.length > 1 ? parts[parts.length - 2] : '';
    if (!/^[A-Za-z0-9_]{2,80}$/.test(entity)){ ko++; continue; }
    $('v3msg').textContent = `Envoi ${ok + ko + 1}/${files.length} : ${entity}/${f.name}…`;
    try {
      const r = await fetch(`${BASE}/${entity}/${f.name}`, {method:'POST', headers:{'X-Requested-With':'panel', 'Content-Type':'application/octet-stream'}, body:f});
      if (r.ok){ ok++; if (f.name === 'markers.json' || !last) last = entity; } else ko++;
    } catch (x) { ko++; }
  }
  $('v3msg').textContent = `${ok} fichier${ok > 1 ? 's' : ''} importé${ok > 1 ? 's' : ''}${ko ? `, ${ko} refusé${ko > 1 ? 's' : ''}` : ''}.`;
  refreshList(last);
});
window.v3open = entity => { $('v3ship').value = entity; load(entity); const p = $('v3d-panel'); if (p.scrollIntoView) p.scrollIntoView({behavior:'smooth', block:'start'}); };
applyOpacity();
refreshList();
