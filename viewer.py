#!/usr/bin/env python3
"""
viewer.py - live STL preview for topo2stl.

    ./viewer.py cordoba.stl

Opens a browser window rendering the STL. Leave it running: every time you
re-run topo2stl.py and overwrite that file, the view reloads automatically
(camera is kept), so you can dial in --z-exaggeration / --model-width / --base
without touching the slicer.

Point it at a NAME.tileset.json instead of an .stl and it merges the tiles
into a preview via tileset_preview.py automatically before serving it.

A later `viewer.py other.stl` just retargets an already-running server on the
same port rather than restarting it - fine for a new model, but it means a
server started before a viewer.py/topo2stl.py code change keeps running the
old code. `--replace` kills any running viewer first and starts fresh (this
is what a stale server serving the same content forever, ignoring apparently
irrelevant input, usually means); `--kill` just stops whatever's running.

Standard library only - no pip install needed.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8731
TOPO2STL = Path(__file__).with_name("topo2stl.py")
TILESET_PREVIEW = Path(__file__).with_name("tileset_preview.py")
STARTED_AT = time.time()   # this process's start - lets a caller (launch_viewer,
                           # --replace) tell whether an already-running viewer
                           # predates the current code and needs restarting,
                           # not just retargeting

# area / output options stripped from the stored argv before re-running with a
# fresh --bbox (name -> whether it takes a following value)
_AREA_OPTS = {"--bbox": True, "--center": True, "--width-km": True,
              "--height-km": True, "-o": True, "--output": True,
              "--view": False, "--no-open": False}

_regen = {"running": False, "done": False, "error": None, "log": "",
          "saved_as": None}
_regen_lock = threading.Lock()
_numpy_ok = None


def _have_numpy() -> bool:
    global _numpy_ok
    if _numpy_ok is None:
        try:
            _numpy_ok = subprocess.run(
                [sys.executable, "-c", "import numpy"],
                capture_output=True, timeout=20).returncode == 0
        except Exception:
            _numpy_ok = False
    return _numpy_ok


def _sidecar(stl: Path) -> Path:
    return stl.with_name(stl.stem + ".topo.json")


def _target_from_name(name: str) -> Path:
    """Turn a user-typed 'save as' filename into a sibling path of the
    current model - strips any directory component and enforces .stl."""
    name = Path(name).name.strip()
    if not name or name in (".", ".."):
        raise ValueError("empty filename")
    if not name.lower().endswith(".stl"):
        name += ".stl"
    return Handler.stl_path.parent / name


def _is_tiled(meta: dict) -> bool:
    """Whether the sidecar's original command used --tile - both a bare
    tile's own sidecar and a tileset_preview.py merge's carry the full
    original argv, so this works from either."""
    return any(tok.split("=", 1)[0] == "--tile" for tok in meta.get("argv", []))


def _exists_for_save(name: str) -> bool:
    """Whether "save as `name`" would overwrite something. A plain model
    writes to that file directly; a --tile model never writes to its own
    -o path at all (write_tiles only writes NAME_r#c#.stl + NAME.tileset.json),
    so the meaningful check there is the manifest that would land there."""
    try:
        target = _target_from_name(name)
    except ValueError:
        return False
    try:
        tiled = _is_tiled(json.loads(_sidecar(Handler.stl_path).read_text()))
    except Exception:
        tiled = False
    if tiled:
        return target.with_name(target.stem + ".tileset.json").exists()
    return target.exists()


def _regen_available():
    if not TOPO2STL.exists():
        return False, "topo2stl.py is not next to viewer.py"
    try:
        meta = json.loads(_sidecar(Handler.stl_path).read_text())
    except Exception:
        return False, "no .topo.json sidecar for this model"
    if not isinstance(meta.get("argv"), list):
        return False, "sidecar predates regen - rebuild once from the CLI"
    if _is_tiled(meta):
        if meta.get("generator") != "tileset_preview":
            return False, ("this is one tile of a --tile set - open the merged "
                           "preview to regenerate the whole area "
                           "(tileset_preview.py NAME.tileset.json --view)")
        if not meta.get("tileset_output"):
            return False, "this preview predates tiled regen - rebuild it with tileset_preview.py"
        if not TILESET_PREVIEW.exists():
            return False, "tileset_preview.py is not next to viewer.py"
    if not _have_numpy():
        return False, "this Python has no numpy - start viewer.py with the venv Python"
    return True, ""


def _strip_area_args(argv):
    out, skip = [], False
    for tok in argv:
        if skip:
            skip = False
            continue
        name = tok.split("=", 1)[0]
        if name in _AREA_OPTS:
            if _AREA_OPTS[name] and "=" not in tok:
                skip = True
        else:
            out.append(tok)
    return out


def _area_args(argv, bbox):
    """New area flags for a regenerate - keeps the model's original style
    (--center/--width-km or --bbox)."""
    if "--center" in argv or "--width-km" in argv:
        import math
        lat = (bbox[0] + bbox[2]) / 2
        lon = (bbox[1] + bbox[3]) / 2
        wkm = (bbox[3] - bbox[1]) * 111.320 * math.cos(math.radians(lat))
        hkm = (bbox[2] - bbox[0]) * 111.320
        return ["--center", f"{lat:.6f},{lon:.6f}",
                "--width-km", f"{wkm:.4f}", "--height-km", f"{hkm:.4f}"]
    return ["--bbox", ",".join(f"{v:.6f}" for v in bbox)]


def _run_subprocess(cmd: list[str]) -> int:
    with _regen_lock:
        _regen["log"] += "$ " + " ".join(cmd) + "\n\n"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, cwd=str(TOPO2STL.parent))
    for line in proc.stdout:
        with _regen_lock:
            _regen["log"] += line
    return proc.wait()


def _run_regen(bbox, target: Path | None = None):
    """Re-run topo2stl for `bbox`. Overwrites the current model by default;
    if `target` is given (a "save as"), writes there instead and, on
    success, retargets the viewer to the new file.

    For a --tile model, `target` (or the sidecar's remembered
    `tileset_output`) is the tileset's *base* name - write_tiles never
    writes a file there itself, only NAME_r#c#.stl + NAME.tileset.json - so
    after that succeeds this also re-runs tileset_preview.py and retargets
    the viewer to the fresh merged preview, not the base name."""
    stl = Handler.stl_path
    try:
        meta = json.loads(_sidecar(stl).read_text())
        argv = meta["argv"]
        tiled = _is_tiled(meta)
        if tiled:
            out_path = target or Path(meta["tileset_output"])
            if not out_path.is_absolute():
                out_path = stl.parent / out_path
        else:
            out_path = target or stl

        cmd = [sys.executable, str(TOPO2STL), *_area_args(argv, bbox),
               *_strip_area_args(argv), "-o", str(out_path)]
        with _regen_lock:
            _regen["log"] = ""
        rc = _run_subprocess(cmd)
        err = None if rc == 0 else f"topo2stl exited with code {rc}"

        new_target = target if (err is None and target is not None) else None
        if err is None and tiled:
            manifest = out_path.with_name(out_path.stem + ".tileset.json")
            preview = out_path.with_name(out_path.stem + ".preview.stl")
            rc2 = _run_subprocess([sys.executable, str(TILESET_PREVIEW),
                                  str(manifest), "-o", str(preview)])
            err = None if rc2 == 0 else f"tileset_preview.py exited with code {rc2}"
            new_target = preview if err is None else new_target
        if err is None and new_target is not None:
            Handler.stl_path = new_target
    except Exception as e:                    # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    with _regen_lock:
        _regen.update(running=False, done=True, error=err,
                       saved_as=(str(target) if target and not err else None))

_PAGE_FILE = Path(__file__).with_name("viewer.html")


def _page() -> bytes:
    """The viewer HTML/JS lives in viewer.html next to this file."""
    try:
        return _PAGE_FILE.read_bytes()
    except OSError:
        return (b"<h1>viewer.html is missing</h1><p>It must sit next to "
                b"viewer.py.</p>")




class Handler(http.server.BaseHTTPRequestHandler):
    stl_path: Path = Path()
    server_ref: http.server.HTTPServer | None = None

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
                self._send(_page(), "text/html; charset=utf-8")
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
                sc = _sidecar(self.stl_path)
                self._send(sc.read_bytes() if sc.exists() else b"{}",
                           "application/json")
            elif p == "/regen/available":
                ok, reason = _regen_available()
                self._send(json.dumps({"ok": ok, "reason": reason}).encode(),
                           "application/json")
            elif p == "/regen/status":
                with _regen_lock:
                    self._send(json.dumps(_regen).encode(), "application/json")
            elif p == "/exists":
                qs = parse_qs(urlparse(self.path).query)
                name = (qs.get("name") or [""])[0]
                exists = _exists_for_save(name) if name else False
                self._send(json.dumps({"exists": exists}).encode(),
                           "application/json")
            elif p == "/status":
                # who's running here and since when - lets launch_viewer()
                # tell a stale process (started before viewer.py/topo2stl.py
                # last changed) apart from a fresh one, and backs `--status`.
                self._send(json.dumps({"pid": os.getpid(), "started": STARTED_AT,
                                       "stl": str(self.stl_path)}).encode(),
                           "application/json")
            else:
                self._send(b"not found", "text/plain", 404)
        except OSError:
            self._send(b"file not ready", "text/plain", 503)

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if p == "/target":
            new = Path(body.decode().strip()).resolve()
            Handler.stl_path = new
            print(f"retargeted -> {new}")
            self._send(new.name.encode(), "text/plain")
        elif p == "/regen":
            ok, reason = _regen_available()
            if not ok:
                self._send(json.dumps({"error": reason}).encode(),
                           "application/json", 400)
                return
            try:
                data = json.loads(body)
                bb = [float(x) for x in data["bbox"]]
                assert len(bb) == 4
                assert bb[0] < bb[2] and bb[1] < bb[3]
                assert -85 <= bb[0] and bb[2] <= 85 and -180 <= bb[1] and bb[3] <= 180
                target = _target_from_name(data["filename"]) if data.get("filename") else None
            except Exception as e:                       # noqa: BLE001
                self._send(json.dumps({"error": f"bad request: {e}"}).encode(),
                           "application/json", 400)
                return
            with _regen_lock:
                if _regen["running"]:
                    self._send(b'{"error":"a regenerate is already running"}',
                               "application/json", 409)
                    return
                _regen.update(running=True, done=False, error=None, log="",
                               saved_as=None)
            threading.Thread(target=_run_regen, args=(bb, target), daemon=True).start()
            self._send(b'{"started":true}', "application/json")
        elif p == "/quit":
            self._send(b'{"stopping":true}', "application/json")
            # shut down from a fresh thread - shutdown() blocks until
            # serve_forever() (running on the main thread) notices and
            # returns, which would deadlock if run on this handler thread
            # instead, and would also prevent this response from flushing.
            if Handler.server_ref is not None:
                threading.Thread(target=Handler.server_ref.shutdown, daemon=True).start()
        else:
            self._send(b"not found", "text/plain", 404)

    def log_message(self, *_):
        pass


def _resolve_target(path: Path) -> Path:
    """A .tileset.json isn't itself a mesh viewer.py can serve - transparently
    merge it via tileset_preview.py (which also (re)writes the merged
    preview's sidecar, so Regenerate/Save-as work) and view that instead, so
    `viewer.py NAME.tileset.json` just does the right thing rather than
    failing to parse it as an STL."""
    if not path.name.endswith(".tileset.json"):
        return path
    if not TILESET_PREVIEW.exists():
        sys.exit(f"{path.name} is a tileset manifest, not an STL - "
                 "tileset_preview.py (needed to merge it) isn't next to viewer.py")
    preview = path.with_name(path.name[:-len(".tileset.json")] + ".preview.stl")
    print(f"{path.name} is a tileset manifest - merging into {preview.name} ...")
    rc = subprocess.run([sys.executable, str(TILESET_PREVIEW), str(path),
                        "-o", str(preview)], cwd=str(TOPO2STL.parent)).returncode
    if rc != 0:
        sys.exit(f"tileset_preview.py failed (exit {rc})")
    return preview


def _port_alive(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _quit_running(port: int) -> bool:
    """POST /quit to whatever's listening on `port`. Returns whether anything
    was there to ask (not whether it was actually a viewer.py - best effort,
    same assumption launch_viewer() already makes elsewhere)."""
    if not _port_alive(port):
        return False
    try:
        import urllib.request
        urllib.request.urlopen(
            urllib.request.Request(f"http://127.0.0.1:{port}/quit", data=b"",
                                   method="POST"), timeout=2).read()
    except Exception:
        pass
    for _ in range(20):                # wait up to ~2s for the port to free
        if not _port_alive(port):
            break
        time.sleep(0.1)
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = PORT
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])

    if "--kill" in argv:
        print(f"stopped the viewer on port {port}" if _quit_running(port)
              else f"nothing running on port {port}")
        return

    if not argv or argv[0].startswith("--"):
        sys.exit("usage: viewer.py OUTPUT.stl [--port N] [--no-open] [--replace]\n"
                 "       viewer.py NAME.tileset.json [...]  (merged automatically)\n"
                 "       viewer.py --kill [--port N]")
    Handler.stl_path = _resolve_target(Path(argv[0]).resolve())
    no_open = "--no-open" in argv

    if "--replace" in argv and _quit_running(port):
        print(f"replaced the viewer previously on port {port}")

    url = f"http://localhost:{port}/"
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # a viewer is very likely already bound to this port; just point the
        # browser at it (it polls the file, so it will show the fresh model).
        # If it's running code older than what's on disk now, --replace above
        # would already have cleared it before this point.
        print(f"port {port} busy - assuming a viewer is already running: {url}")
        if not no_open:
            webbrowser.open(url)
        return
    Handler.server_ref = srv

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
