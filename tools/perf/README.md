# Interactive performance probes

`appperf.py` measures what a photographer feels in the desktop app, through its control channel
(`docs/control-protocol.md`): slider drags and single updates in SDR, HDR and HDR-on-SDR editing with
high-quality adjustment previews off and on, stepping through photos, letting go of a drag at 1:1, and the first
zoom to 1:1. `cargo xtask bench` covers the engine without the app; these cover the app's scheduling, caches and
display path on top of it.

```bash
cargo build --release -p lcc-app
cp target/release/app target/app-b       # the build to compare (app.exe on Windows)
git stash && cargo build --release -p lcc-app && cp target/release/app target/app-a && git stash pop
python tools/perf/appperf.py ab --builds target/app-a target/app-b --probe drag \
    --files corpus/raw/arw-sony-a7m3-compressed.arw corpus/raw/dng-ricoh-gr3.dng corpus/raw/raf-fuji-xt5-6122.raf
```

- `ab` runs the builds back to back (A, B, A, B, ...), each in a new process with a fresh library and settings
  folder (`<PREFIX>_CONFIG_DIR`, read from `brand.toml`) on its own control port (default 18731), and stops each
  process it started before the next one starts. It refuses to run when the port is taken.
- `run` probes an app that is already running (`--port`, `--token-file`: `control-token-<port>` in its settings
  folder).
- It prints the median of each measurement per build, grouped by mode and HQ setting; `--out` keeps every row.
- The window must be on screen: the app runs a command only after it has drawn a frame.

Read the results as an A/B within one run. The same build measured ~10 % apart in two sessions on the same machine,
and single updates are quantised by the display's frame time. The probes and what they report are listed in the
script's docstring (`python tools/perf/appperf.py --help`). Numbers from the 2026-10 review are in `docs/perf.md`
→ Interactive editing.
