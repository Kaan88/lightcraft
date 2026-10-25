#!/usr/bin/env python3
"""Measure the desktop app's interactive performance through its control channel, and A/B two builds.

    python tools/perf/appperf.py ab --builds A B --probe drag --files F... [--rounds 2] [--port 18731]
    python tools/perf/appperf.py run --probe step --port 7980 --token-file T --files F...   (a running app)

`ab` starts each build in turn (A, B, A, B, ...), each in its own process with a fresh library and settings
folder, runs the probe, stops the process it started, and prints the median of every measurement per build. A
build is a path to the desktop binary. The probes, all on raw files in the Detail view at 1600 x 1000:

- `drag`: in SDR, HDR and HDR-on-SDR editing, with high-quality adjustment previews off and on: single exposure
  updates (time until the loupe is quiet again, and until the first render lands) and a 2 s exposure drag at
  60 Hz (renders finished per second, all slots and the shown view's own (`perf.slots`), render time, UI update
  time, release to quiet).
- `step`: arrow keys through the files after a 1.5 s look at each: time until the next photo shows its own final
  render, and what showed first.
- `release`: at 1:1, a 0.5 s exposure drag to a value no earlier drag ended on, then let go: time until no visible
  render is pending, and until every tile (the ring included) is in.
- `zoom`: the first zoom to 1:1 after a 2.5 s look at the whole photo: time until every tile is in.

Session-to-session noise on one machine is ~10 %: compare builds within one `ab` run, not across runs.
"""

import argparse
import json
import os
import pathlib
import socket
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]


def env_prefix():
    """`env_prefix` from brand.toml (the settings-folder variable is `<PREFIX>_CONFIG_DIR`)."""
    for line in (ROOT / "brand.toml").read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "env_prefix":
            return value.split("#")[0].strip().strip('"')
    raise SystemExit("brand.toml has no env_prefix")


class Client:
    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=600)
        self.buf = b""
        self.n = 0
        if not self.call("auth", {"token": token}).get("ok"):
            raise SystemExit("the control token was refused")

    def call(self, method, params=None):
        self.n += 1
        self.sock.sendall((json.dumps({"id": self.n, "method": method, "params": params or {}}) + "\n").encode())
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 22)
            if not chunk:
                raise RuntimeError("the app closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def ex(self, command, params=None):
        r = self.call("engine.execute", {"command": command, "params": params or {}})
        if not r.get("ok"):
            print("!!", command, params, r.get("error"), file=sys.stderr)
        return r.get("result")

    def inspect(self):
        return self.call("ui.inspect").get("result", {})


def quiet(i):
    """Nothing rendering or queued, and the loupe (and its 1:1 region) has nothing pending."""
    p, l = i.get("perf", {}), i.get("loupe") or {}
    return p.get("renderQueue", 1) == 0 and p.get("rendersInFlight", 1) == 0 and not l.get("pending") and not (l.get("region") or {}).get("pending", 0)


def wait_quiet(c, timeout=60):
    t = time.perf_counter()
    while time.perf_counter() - t < timeout:
        if quiet(c.inspect()):
            return round((time.perf_counter() - t) * 1e3, 1)
        time.sleep(0.003)
    return None


def import_files(c, files):
    c.call("ui.resize", {"width": 1600, "height": 1000})
    # the window must have drawn before the first command runs
    for _ in range(100):
        if c.call("engine.execute", {"command": "catalog.query", "params": {}}).get("ok"):
            break
        time.sleep(0.2)
    c.ex("library.import", {"paths": files})
    names = {pathlib.Path(f).name for f in files}
    return [(p["id"], p["fileName"]) for p in c.ex("catalog.query")["photos"] if p["fileName"] in names]


def sweep(lo, hi, n=120):
    import math

    return [lo + (hi - lo) * (0.5 - 0.5 * math.cos(2 * math.pi * k / n)) for k in range(n)]


def probe_drag(c, files):
    photos = import_files(c, files)
    for pid, name in photos:
        c.ex("library.select", {"ids": [pid], "active": pid})
        c.call("ui.set", {"view": "detail"})
        wait_quiet(c)
        for mode, hdr, preview in (("sdr", False, True), ("hdr", True, True), ("hdrOnSdr", True, False)):
            c.ex("develop.hdr", {"enabled": hdr})
            c.ex("view.hdrPreview", {"on": preview})
            for hq in (False, True):
                c.call("ui.set", {"settings": {"highQualityAdjustmentPreviews": hq}})
                wait_quiet(c)
                lat, first = [], []
                c.ex("develop.beginInteraction", {"label": "probe"})
                for v in (0.2, 0.6, 0.3, 0.9, 0.1, 0.5, 0.7):
                    t = time.perf_counter()
                    done0 = c.inspect()["perf"]["rendersDone"]
                    c.ex("develop.set", {"control": "light.exposure", "value": v})
                    landed = None
                    while time.perf_counter() - t < 10:
                        i = c.inspect()
                        if landed is None and i["perf"]["rendersDone"] > done0:
                            landed = (time.perf_counter() - t) * 1e3
                            first.append(landed)
                        if landed is not None and quiet(i):
                            break
                        time.sleep(0.002)
                    lat.append((time.perf_counter() - t) * 1e3)
                c.ex("develop.endInteraction", {})
                wait_quiet(c)
                yield {"file": name, "mode": mode, "hq": hq, "updateMs": statistics.median(lat), "firstMs": statistics.median(first) if first else None}
                c.ex("develop.beginInteraction", {"label": "Exposure"})
                # the renders that show the drag: the HDR texture's in HDR shown in HDR, else the loupe's
                kind = "hdrView" if mode == "hdr" else "main"
                perf0 = c.inspect()["perf"]
                done0 = perf0["rendersDone"]
                shown0 = (perf0.get("slots") or {}).get(kind, {}).get("done")
                t0 = time.perf_counter()
                ren, ui = [], []
                for k, v in enumerate(sweep(0.0, 1.5)):
                    while time.perf_counter() < t0 + k / 60:
                        time.sleep(0.0005)
                    c.ex("develop.set", {"control": "light.exposure", "value": v})
                    if k % 6 == 0:
                        p = c.inspect()["perf"]
                        ren.append(p["lastRenderMs"])
                        ui.append(p["updateMs"])
                secs = time.perf_counter() - t0
                perf1 = c.inspect()["perf"]
                done = perf1["rendersDone"] - done0
                shown1 = (perf1.get("slots") or {}).get(kind, {}).get("done")
                c.ex("develop.endInteraction", {})
                yield {
                    "file": name,
                    "mode": mode,
                    "hq": hq,
                    "dragUpdatesPerS": done / secs,
                    "dragShownPerS": (shown1 - (shown0 or 0)) / secs if shown1 is not None else None,
                    "dragRenderMs": statistics.median(ren),
                    "dragUiMsMax": max(ui),
                    "releaseToQuietMs": wait_quiet(c),
                }
        c.ex("develop.hdr", {"enabled": False})
        c.ex("view.hdrPreview", {"on": True})
        c.ex("develop.reset", {})


def probe_step(c, files):
    raws = [pid for pid, _ in import_files(c, files)]
    c.ex("library.select", {"ids": raws})  # only the raws in the filmstrip, so the neighbours are raws
    c.call("ui.set", {"view": "detail"})
    c.ex("library.select", {"ids": [raws[0]], "active": raws[0]})
    wait_quiet(c)
    for rep in range(2):
        for _ in range(len(raws) - 1):
            prev = c.inspect().get("active")
            time.sleep(1.5)  # a look at the photo, while the neighbours are prepared
            c.call("ui.key", {"key": "ArrowRight" if rep == 0 else "ArrowLeft"})
            t = time.perf_counter()
            first = None
            while time.perf_counter() - t < 30:
                i = c.inspect()
                l, active = i.get("loupe") or {}, i.get("active")
                if active != prev and l.get("photo") == active:
                    if first is None and l.get("source"):
                        first = (time.perf_counter() - t) * 1e3
                    if l.get("source") in ("render", "cached", "hdr") and not l.get("pending"):
                        yield {"nextMs": (time.perf_counter() - t) * 1e3, "firstMs": first}
                        break
                time.sleep(0.002)


def probe_release(c, files):
    end = 0.31
    for pid, name in import_files(c, files):
        c.ex("library.select", {"ids": [pid], "active": pid})
        c.call("ui.set", {"view": "detail"})
        wait_quiet(c)
        c.ex("view.zoomLevel", {"level": "1:1"})
        wait_quiet(c)
        for hq in (False, True):
            c.call("ui.set", {"settings": {"highQualityAdjustmentPreviews": hq}})
            wait_quiet(c)
            end += 0.07  # a look no earlier drag ended on: none of its tiles are cached
            c.ex("develop.beginInteraction", {"label": "Exposure"})
            for k in range(1, 31):
                c.ex("develop.set", {"control": "light.exposure", "value": end * k / 30})
                time.sleep(1 / 60)
            time.sleep(0.3)
            t = time.perf_counter()
            c.ex("develop.endInteraction", {})
            visible = None
            while time.perf_counter() - t < 60:
                i = c.inspect()
                pending = [str(x) for x in i["perf"].get("pendingSlots", [])]
                if visible is None and not any(x.startswith("Window") or x == "Main" for x in pending):
                    visible = (time.perf_counter() - t) * 1e3
                if quiet(i):
                    break
                time.sleep(0.003)
            yield {"file": name, "hq": hq, "visibleMs": visible, "quietMs": (time.perf_counter() - t) * 1e3}
        c.ex("view.zoomLevel", {"level": "fit"})
        c.ex("develop.reset", {})


def probe_zoom(c, files):
    c.call("ui.set", {"view": "detail"})
    for pid, name in import_files(c, files):
        c.ex("library.select", {"ids": [pid], "active": pid})
        wait_quiet(c)
        time.sleep(2.5)  # a look at the whole photo
        wait_quiet(c)
        c.ex("view.zoomLevel", {"level": "1:1"})
        yield {"file": name, "zoomMs": wait_quiet(c)}
        c.ex("view.zoomLevel", {"level": "fit"})
        wait_quiet(c)


PROBES = {"drag": probe_drag, "step": probe_step, "release": probe_release, "zoom": probe_zoom}
# the fields a row is grouped by in the summary (the rest are measurements)
KEYS = ("mode", "hq")


def summary(rows_by_build):
    groups = {}
    for build, rows in rows_by_build.items():
        for r in rows:
            key = tuple((k, r[k]) for k in KEYS if k in r)
            for field, v in r.items():
                if field in KEYS or field in ("file", "round") or not isinstance(v, (int, float)) or isinstance(v, bool):
                    continue
                groups.setdefault((key, field), {}).setdefault(build, []).append(v)
    builds = list(rows_by_build)
    print("group".ljust(28), "measurement".ljust(18), *(pathlib.Path(b).stem.rjust(14) for b in builds))
    for (key, field), per in sorted(groups.items(), key=lambda g: (str(g[0][0]), g[0][1])):
        label = " ".join(f"{k}={v}" for k, v in key) or "-"
        cells = [f"{statistics.median(per[b]):.1f}".rjust(14) if per.get(b) else "-".rjust(14) for b in builds]
        print(label.ljust(28), field.ljust(18), *cells)


def wait_ready(proc, port, token_file, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        if proc.poll() is not None:
            raise SystemExit(f"the app exited ({proc.returncode}) before its control server started")
        if token_file.exists():
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                return
            except OSError:
                pass
        time.sleep(0.25)
    raise SystemExit("the app's control server did not start")


def port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def run_ab(args):
    if not port_free(args.port):
        raise SystemExit(f"port {args.port} is taken: stop what listens there, or pass --port")
    files = [str(pathlib.Path(f).resolve()) for f in args.files]
    args.builds = [str(pathlib.Path(b).resolve()) for b in args.builds]
    rows = {b: [] for b in args.builds}
    var = env_prefix() + "_CONFIG_DIR"
    for rnd in range(args.rounds):
        for build in args.builds:
            # (Windows may hold the log a moment after the process exits)
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                cfg, lib = pathlib.Path(tmp, "cfg"), pathlib.Path(tmp, "lib")
                lib.mkdir()
                env = dict(os.environ, **{var: str(cfg)})
                log = open(pathlib.Path(tmp, "app.log"), "w")
                proc = subprocess.Popen([build, "--control", str(args.port), "--library", str(lib)], env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
                try:
                    token_file = cfg / f"control-token-{args.port}"
                    wait_ready(proc, args.port, token_file)
                    c = Client(args.port, token_file.read_text().strip())
                    for row in PROBES[args.probe](c, files):
                        row.update(build=build, round=rnd)
                        rows[build].append(row)
                        print(json.dumps(row), file=sys.stderr)
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    log.close()
                    while not port_free(args.port):
                        time.sleep(0.2)
    if args.out:
        with open(args.out, "w") as f:
            for b in rows.values():
                f.writelines(json.dumps(r) + "\n" for r in b)
    summary(rows)


def run_one(args):
    c = Client(args.port, pathlib.Path(args.token_file).read_text().strip())
    rows = list(PROBES[args.probe](c, [str(pathlib.Path(f).resolve()) for f in args.files]))
    for r in rows:
        print(json.dumps(r))
    summary({"app": rows})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ab = sub.add_parser("ab", help="A/B builds, each in fresh processes")
    ab.add_argument("--builds", nargs="+", required=True)
    ab.add_argument("--probe", choices=PROBES, required=True)
    ab.add_argument("--files", nargs="+", required=True)
    ab.add_argument("--rounds", type=int, default=2)
    ab.add_argument("--port", type=int, default=18731)
    ab.add_argument("--out", help="also write every row as JSON lines")
    one = sub.add_parser("run", help="probe an app that is already running")
    one.add_argument("--probe", choices=PROBES, required=True)
    one.add_argument("--port", type=int, required=True)
    one.add_argument("--token-file", required=True)
    one.add_argument("--files", nargs="+", required=True)
    args = ap.parse_args()
    run_ab(args) if args.cmd == "ab" else run_one(args)


if __name__ == "__main__":
    main()
