#!/usr/bin/env python3
"""
viewer.py - live STL preview for topo2stl.

    ./viewer.py cordoba.stl

Opens a browser window rendering the STL. Leave it running: every time you
re-run topo2stl.py and overwrite that file, the view reloads automatically
(camera is kept), so you can dial in --z-exaggeration / --model-width / --base
without touching the slicer.

Standard library only - no pip install needed.
"""

from __future__ import annotations

import http.server
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

PORT = 8731

PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>topo2stl viewer</title>
<style>
  html, body { margin: 0; height: 100%; background: #14171c; overflow: hidden;
               font: 12px/1.5 -apple-system, system-ui, sans-serif; color: #cdd3da; }
  #c { display: block; width: 100vw; height: 100vh; }
  #hud { position: fixed; top: 12px; left: 12px; padding: 10px 12px;
         background: rgba(20,23,28,.72); border: 1px solid #2b313a; border-radius: 8px;
         backdrop-filter: blur(6px); pointer-events: none; }
  #hud b { color: #fff; font-weight: 600; }
  #hud .dim { color: #8b95a1; }
  #bar { position: fixed; top: 12px; right: 12px; display: flex; gap: 6px; }
  #bar button { pointer-events: auto; background: rgba(20,23,28,.72); color: #cdd3da;
    border: 1px solid #2b313a; border-radius: 6px; padding: 6px 10px; cursor: pointer; }
  #bar button:hover { background: #232830; color: #fff; }
  #bar button.on { background: #3a6df0; border-color: #3a6df0; color: #fff; }
  #bar button:disabled { opacity: .4; cursor: default; }
  #err { position: fixed; bottom: 12px; left: 12px; color: #ff8c8c; }
  #compass { position: fixed; right: 18px; bottom: 18px; width: 66px; height: 66px;
    border-radius: 50%; background: rgba(20,23,28,.72); border: 1px solid #2b313a;
    backdrop-filter: blur(6px); pointer-events: none; }
  #compass .ring { position: absolute; inset: 7px; border-radius: 50%;
    border: 1px solid #3a4048; }
  #needle { position: absolute; inset: 0; transition: transform .08s linear; }
  #needle .n { position: absolute; left: 50%; top: 5px; transform: translateX(-50%);
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-bottom: 20px solid #e8623c; }
  #needle .s { position: absolute; left: 50%; bottom: 5px; transform: translateX(-50%);
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 20px solid #566; }
  #needle .lbl { position: absolute; left: 50%; top: -15px; transform: translateX(-50%);
    font-weight: 700; font-size: 11px; color: #e8623c; }
  .corner { position: fixed; pointer-events: none; z-index: 5; }
  .corner .dot { position: absolute; left: -4px; top: -4px; width: 8px; height: 8px;
    border-radius: 50%; background: #e8623c; box-shadow: 0 0 0 2px rgba(0,0,0,.55); }
  .corner .lab { position: absolute; top: 0; transform: translateY(-50%); white-space: nowrap;
    font: 600 12px/1.3 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
    color: #eef2f6; text-shadow: 0 1px 3px #000, 0 0 3px #000; }
  .corner .lab .tag { color: #9db3c9; font-weight: 700; letter-spacing: .07em;
    margin-right: 6px; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="hud">
  <div><b id="fname">-</b></div>
  <div class="dim"><span id="tris">-</span> triangles</div>
  <div>X <b id="sx">-</b> &nbsp; Y <b id="sy">-</b> &nbsp; Z <b id="sz">-</b> mm</div>
  <div class="dim" id="relief"></div>
  <div class="dim" id="stamp"></div>
</div>
<div id="bar">
  <button id="bfit">Fit</button>
  <button id="brelief">Relief: lines</button>
  <button id="bwire">Wireframe</button>
  <button id="bspin">Spin</button>
  <button id="bcoords">Coords</button>
</div>
<div id="err"></div>
<div id="compass">
  <div class="ring"></div>
  <div id="needle"><span class="lbl">N</span><span class="n"></span><span class="s"></span></div>
</div>
<div class="corner" id="cSW"><span class="dot"></span><span class="lab"><span class="tag">SW</span><span class="txt"></span></span></div>
<div class="corner" id="cNE"><span class="dot"></span><span class="lab"><span class="tag">NE</span><span class="txt"></span></span></div>

<script type="importmap">
{ "imports": {
  "three": "https://unpkg.com/three@0.160.0/build/three.module.js",
  "three/addons/": "https://unpkg.com/three@0.160.0/examples/jsm/"
}}
</script>
<script type="module">
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const canvas = document.getElementById('c');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x14171c);

const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100000);
camera.up.set(0, 0, 1);                       // topo2stl STLs are Z-up

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.autoRotateSpeed = 1.2;   // orbits the camera around controls.target

scene.add(new THREE.HemisphereLight(0xffffff, 0x33383f, 2.2));
const key = new THREE.DirectionalLight(0xfff0e0, 2.4);
key.position.set(0.6, -1, 1.2);
scene.add(key);
const rim = new THREE.DirectionalLight(0x88aaff, 0.8);
rim.position.set(-1, 0.4, 0.5);
scene.add(rim);

const material = new THREE.MeshStandardMaterial({
  color: 0xc98f5a, roughness: 0.82, metalness: 0.02, flatShading: false,
  side: THREE.DoubleSide });   // stay solid even if an STL is wound inside-out

// Inject a hypsometric tint + contour lines keyed off world Z, toggled by uniforms.
material.onBeforeCompile = (shader) => {
  shader.uniforms.uZMin = { value: 0 };
  shader.uniforms.uZMax = { value: 1 };
  shader.uniforms.uStep = { value: 0 };      // contour spacing in model mm
  shader.uniforms.uTint = { value: 1 };
  shader.uniforms.uContour = { value: 1 };
  shader.vertexShader = 'varying vec3 vWPos;\n' + shader.vertexShader.replace(
    '#include <begin_vertex>',
    '#include <begin_vertex>\n  vWPos = (modelMatrix * vec4(position, 1.0)).xyz;');
  shader.fragmentShader =
    'varying vec3 vWPos;\nuniform float uZMin,uZMax,uStep,uTint,uContour;\n' +
    shader.fragmentShader.replace('#include <color_fragment>', `#include <color_fragment>
    {
      float t = clamp((vWPos.z - uZMin) / max(uZMax - uZMin, 1e-4), 0.0, 1.0);
      if (uTint > 0.5) {
        vec3 g0=vec3(0.20,0.44,0.28), g1=vec3(0.55,0.72,0.36), g2=vec3(0.86,0.78,0.52),
             g3=vec3(0.62,0.42,0.26), g4=vec3(0.97,0.96,0.93);
        vec3 hc = t<0.25 ? mix(g0,g1,t/0.25)
                : t<0.50 ? mix(g1,g2,(t-0.25)/0.25)
                : t<0.75 ? mix(g2,g3,(t-0.50)/0.25)
                :          mix(g3,g4,(t-0.75)/0.25);
        diffuseColor.rgb = mix(diffuseColor.rgb, hc, 0.9);
      }
      if (uContour > 0.5 && uStep > 0.0) {
        float band = (vWPos.z - uZMin) / uStep;
        float fp = fract(band); float dist = min(fp, 1.0 - fp);
        float line = 1.0 - smoothstep(0.0, max(fwidth(band), 1e-5) * 1.2, dist);
        float mb = band / 5.0; float mfp = fract(mb); float mdist = min(mfp, 1.0 - mfp);
        float major = 1.0 - smoothstep(0.0, max(fwidth(mb), 1e-5) * 1.2, mdist);
        diffuseColor.rgb = mix(diffuseColor.rgb, vec3(0.09,0.08,0.06),
                               line * 0.30 + major * 0.35);
      }
    }`);
  material.userData.shader = shader;
};

let mesh = null;
let grid = null;
let reliefMode = 2;                 // 0 off, 1 tint, 2 tint + contour lines
const RELIEF_LABELS = ['Relief: off', 'Relief: tint', 'Relief: lines'];

function niceInterval(reliefM) {
  const steps = [5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000];
  for (const s of steps) if (reliefM / s <= 16) return s;
  return 5000;
}
function applyReliefUniforms() {
  const sh = material.userData.shader;
  if (sh) {
    sh.uniforms.uTint.value = reliefMode >= 1 ? 1 : 0;
    sh.uniforms.uContour.value = reliefMode >= 2 ? 1 : 0;
  }
  const b = document.getElementById('brelief');
  if (b) { b.textContent = RELIEF_LABELS[reliefMode];
           b.classList.toggle('on', reliefMode > 0); }
}

// Recompute the tint range + contour spacing from the current model + meta.
function updateReliefRange() {
  const sh = material.userData.shader;
  if (!sh || !mesh) return;
  const bb = mesh.geometry.boundingBox;
  const baseMm = (meta && +meta.base_mm) || 0;
  const zLo = bb.min.z + baseMm, zHi = bb.max.z;
  sh.uniforms.uZMin.value = zLo;
  sh.uniforms.uZMax.value = zHi;

  const reliefMm = Math.max(zHi - zLo, 0.001);
  const mPerMm = meta && +meta.elev_m_per_mm ? +meta.elev_m_per_mm : null;
  let stepMm, label;
  if (mPerMm) {
    const intM = niceInterval(reliefMm * mPerMm);
    stepMm = intM / mPerMm;
    label = `contours every ${intM} m (bold every ${intM * 5} m)`;
  } else {
    stepMm = reliefMm / 12;
    label = `contours every ~${stepMm.toFixed(1)} mm`;
  }
  sh.uniforms.uStep.value = stepMm;
  document.getElementById('relief').textContent = reliefMode >= 2 ? label : '';
}

const loader = new STLLoader();
const $ = id => document.getElementById(id);

function frame(geometry) {
  geometry.computeBoundingBox();
  const bb = geometry.boundingBox;
  const size = new THREE.Vector3(); bb.getSize(size);
  const c = new THREE.Vector3(); bb.getCenter(c);
  const r = size.length() / 2;
  const dir = new THREE.Vector3(1, -1.15, 0.85).normalize();
  camera.position.copy(c).addScaledVector(dir, r * 2.6);
  controls.target.copy(c);
  camera.near = r / 100; camera.far = r * 50;
  camera.updateProjectionMatrix();
  controls.update();
}

function setModel(geometry, refit) {
  geometry.computeVertexNormals();
  geometry.computeBoundingBox();
  const size = new THREE.Vector3(); geometry.boundingBox.getSize(size);

  if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); }
  mesh = new THREE.Mesh(geometry, material);
  scene.add(mesh);

  if (grid) scene.remove(grid);
  const g = Math.max(size.x, size.y) * 1.4;
  grid = new THREE.GridHelper(g, 20, 0x3a4048, 0x262b31);
  grid.rotation.x = Math.PI / 2;               // into the XY plane (Z-up world)
  const cen = new THREE.Vector3(); geometry.boundingBox.getCenter(cen);
  grid.position.set(cen.x, cen.y, geometry.boundingBox.min.z);
  scene.add(grid);

  const bb = geometry.boundingBox;                 // SW/NE base corners for labels
  swWorld = new THREE.Vector3(bb.min.x, bb.min.y, bb.min.z);
  neWorld = new THREE.Vector3(bb.max.x, bb.max.y, bb.min.z);

  $('tris').textContent = (geometry.attributes.position.count / 3).toLocaleString();
  $('sx').textContent = size.x.toFixed(1);
  $('sy').textContent = size.y.toFixed(1);
  $('sz').textContent = size.z.toFixed(1);
  $('stamp').textContent = 'updated ' + new Date().toLocaleTimeString();
  updateReliefRange();
  if (refit) frame(geometry);
}

let currentVersion = null;
let currentName = null;
let firstLoad = true;

async function poll() {
  try {
    const v = await (await fetch('/version', { cache: 'no-store' })).text();
    const name = v.split('|').slice(1).join('|');
    window.__fname = name;
    $('fname').textContent = name || '-';
    const missing = v.startsWith('0|');
    if (v !== currentVersion && !missing) {
      const buf = await (await fetch('/model.stl', { cache: 'no-store' })).arrayBuffer();
      const geo = loader.parse(buf);
      setModel(geo, firstLoad || name !== currentName);
      try { applyMeta(await (await fetch('/meta', { cache: 'no-store' })).json()); }
      catch (_) { applyMeta(null); }
      currentVersion = v;
      currentName = name;
      firstLoad = false;
      $('err').textContent = '';
    } else if (missing) {
      $('err').textContent = 'waiting for ' + (name || 'file') + ' ...';
    }
  } catch (e) {
    $('err').textContent = 'viewer server not responding';
  }
  setTimeout(poll, 1000);
}

$('bfit').onclick = () => { if (mesh) frame(mesh.geometry); };
$('brelief').onclick = () => {
  reliefMode = (reliefMode + 1) % 3;
  applyReliefUniforms();
  updateReliefRange();
};
$('bwire').onclick = e => { material.wireframe = !material.wireframe;
  e.target.classList.toggle('on', material.wireframe); };
$('bspin').onclick = e => { controls.autoRotate = !controls.autoRotate;
  e.target.classList.toggle('on', controls.autoRotate); };

let cw = 1, ch = 1;
function resize() {
  cw = canvas.clientWidth; ch = canvas.clientHeight;
  renderer.setSize(cw, ch, false);
  camera.aspect = cw / ch; camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);

// --- corner coordinate labels -------------------------------------------------
let meta = null;
let showCoords = true;
let swWorld = null, neWorld = null;
const elSW = $('cSW'), elNE = $('cNE');

const fmtDeg = (lat, lon) =>
  `${Math.abs(lat).toFixed(4)}°${lat >= 0 ? 'N' : 'S'}  ` +
  `${Math.abs(lon).toFixed(4)}°${lon >= 0 ? 'E' : 'W'}`;

function applyMeta(m) {
  meta = (m && Array.isArray(m.bbox) && m.bbox.length === 4) ? m : null;
  const btn = $('bcoords');
  if (!meta) {
    btn.disabled = true; btn.classList.remove('on');
    elSW.style.display = elNE.style.display = 'none';
  } else {
    btn.disabled = false;
    btn.classList.toggle('on', showCoords);
    elSW.querySelector('.txt').textContent = fmtDeg(meta.bbox[0], meta.bbox[1]);
    elNE.querySelector('.txt').textContent = fmtDeg(meta.bbox[2], meta.bbox[3]);
  }
  updateReliefRange();
}

function placeCorner(el, world) {
  if (!showCoords || !meta || !world) { el.style.display = 'none'; return; }
  const v = world.clone().project(camera);
  if (v.z > 1) { el.style.display = 'none'; return; }     // behind the camera
  const sx = (v.x * 0.5 + 0.5) * cw;
  const sy = (-v.y * 0.5 + 0.5) * ch;
  el.style.display = '';
  el.style.left = sx + 'px';
  el.style.top = Math.max(14, Math.min(ch - 14, sy)) + 'px';
  const lab = el.querySelector('.lab');
  const onLeft = sx < cw / 2;               // point on the left half -> hang label further left
  lab.style.left = onLeft ? 'auto' : '14px';
  lab.style.right = onLeft ? '14px' : 'auto';
  lab.style.textAlign = onLeft ? 'right' : 'left';
}
function placeCorners() { placeCorner(elSW, swWorld); placeCorner(elNE, neWorld); }

$('bcoords').onclick = e => {
  showCoords = !showCoords;
  e.target.classList.toggle('on', showCoords && !!meta);
};

const NORTH = new THREE.Vector3(0, 1, 0);   // +Y is north in topo2stl STLs
const invQ = new THREE.Quaternion();
const needle = document.getElementById('needle');
function updateCompass() {
  // north direction expressed in camera/view space, projected onto the screen
  const v = NORTH.clone().applyQuaternion(invQ.copy(camera.quaternion).invert());
  const deg = Math.atan2(v.x, v.y) * 180 / Math.PI;   // 0 = north points up
  needle.style.transform = `rotate(${deg}deg)`;
}

let shaderReady = false;
function tick() {
  requestAnimationFrame(tick);
  controls.update();            // applies damping + autoRotate about the target
  if (!shaderReady && material.userData.shader) {   // shader compiles on 1st render
    shaderReady = true;
    applyReliefUniforms();
    updateReliefRange();
  }
  updateCompass();
  placeCorners();
  renderer.render(scene, camera);
}
applyReliefUniforms(); resize(); tick(); poll();
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    stl_path: Path = Path()

    def _send(self, body: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        try:
            if p == "/":
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif p == "/name":
                self._send(self.stl_path.name.encode(), "text/plain")
            elif p == "/version":
                # "<mtime_ns>|<name>"  ('0' mtime = file not present yet)
                try:
                    mt = str(self.stl_path.stat().st_mtime_ns)
                except OSError:
                    mt = "0"
                self._send(f"{mt}|{self.stl_path.name}".encode(), "text/plain")
            elif p == "/model.stl":
                data = self.stl_path.read_bytes()
                self._send(data, "model/stl")
            elif p == "/meta":
                sidecar = self.stl_path.with_name(self.stl_path.stem + ".topo.json")
                if sidecar.exists():
                    self._send(sidecar.read_bytes(), "application/json")
                else:
                    self._send(b"{}", "application/json")
            else:
                self._send(b"not found", "text/plain", 404)
        except OSError:
            self._send(b"file not ready", "text/plain", 503)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/target":
            self._send(b"not found", "text/plain", 404)
            return
        n = int(self.headers.get("Content-Length", 0))
        new = Path(self.rfile.read(n).decode().strip()).resolve()
        Handler.stl_path = new
        print(f"retargeted -> {new}")
        self._send(new.name.encode(), "text/plain")

    def log_message(self, *_):
        pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.exit("usage: viewer.py OUTPUT.stl [--port N] [--no-open]")
    Handler.stl_path = Path(argv[0]).resolve()
    port = PORT
    no_open = "--no-open" in argv
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])

    url = f"http://localhost:{port}/"
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # a viewer is very likely already bound to this port; just point the
        # browser at it (it polls the file, so it will show the fresh model).
        print(f"port {port} busy - assuming a viewer is already running: {url}")
        if not no_open:
            webbrowser.open(url)
        return

    print(f"serving {Handler.stl_path.name} at {url}  (Ctrl-C to stop)")
    if not no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        srv.shutdown()


if __name__ == "__main__":
    main()
