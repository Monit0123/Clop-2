# Clop Batch

Batch-optimise a folder through the macOS app [Clop](https://lowtechguys.com/clop/)
while staying inside its free tier. Files are sent 5 at a time; between batches
Clop is quit and reopened, which resets its per-session counter.

Two front ends share one engine:

- `clop_gui.py`: local web UI. Pick folders in a dialog, watch progress, and get
  optimised copies in a separate output folder.
- `clop_batch.py`: command line. Same work, scriptable.

A ready-made macOS app is available from
[Releases](https://github.com/Monit0123/Clop-2/releases/latest). See
[INSTALL.md](INSTALL.md).

## Requirements

1. macOS with Clop installed.
2. Clop's command line tool: menu bar icon, then *Install command line
   integration*. This creates `~/.local/bin/clop`.
3. Python 3.9 or newer when running from source. The DMG bundles its own Python.

## GUI

```bash
python3 clop_gui.py
```

Opens `http://127.0.0.1:8765` and serves that page locally. Choose the folder to
optimise and the output folder, then press Start.

For every batch:

1. Matching files are copied into the output folder, keeping their subfolders.
2. The copies are optimised by Clop. Source files are not modified.
3. Clop quits and reopens so the next batch starts with a fresh allowance.

Form controls:

| Control | Default | Meaning |
|---|---|---|
| Output folder | `<source>-optimised` | Created if missing. Must differ from the source. |
| Files per session | 5 | Clop's free-tier limit. |
| Compression | Clop's setting | Maps to `--compression 20/50/75/90` or `--aggressive`. |
| Order | Path A to Z | Or oldest first, or smallest first. |
| Retries per batch | 2 | Retries only failed files, after a Clop restart. |
| Extensions | images, video, audio, PDF | Comma separated. |
| Exclude globs | none | For example `*.tmp`. |
| Restart Clop first | on | Ensures batch 1 starts with a full allowance. |
| Delete originals | off | Only after the copy is verified on disk. |
| Skip already processed | on | Makes a second run a no-op. |

Cancel stops after the file in flight and keeps finished work. The page binds to
`127.0.0.1`; `--host 0.0.0.0` exposes it to the network, which is usually a bad
idea. Settings and processed-file history live in
`~/Library/Application Support/clop-gui/`.

## Command line

```bash
./clop_batch.py ~/Pictures/export --dry-run
./clop_batch.py ~/Pictures/export --max-files 10 -v
./clop_batch.py ~/Pictures/export
./clop_batch.py ~/Movies/import --ext mov,mp4 --order mtime
./clop_batch.py ~/Pictures/export --clop-arg=--aggressive
```

| Option | Default | Meaning |
|---|---|---|
| `directory` | required | Folder scanned recursively. |
| `-b, --batch-size N` | 5 | Files per Clop session. |
| `-e, --ext LIST` | images, video, audio, PDF | Comma separated. |
| `--exclude GLOB`, `--exclude-dir NAME` | none | Repeatable. |
| `--include-hidden` | off | Scan dot files and folders. |
| `--order path\|mtime\|size` | path | Which files go first. |
| `--reverse` | off | Reverse that order. |
| `--max-files N` | no limit | Stop after N files. |
| `--retries N` | 2 | Extra attempts per batch, each with a fresh Clop session. |
| `--retry-delay S` | 3 | Wait before the first retry, doubled per attempt. |
| `--timeout S` | 1800 | Maximum wait per batch; 0 waits forever. |
| `--quit-timeout`, `--launch-timeout`, `--settle` | 60, 60, 2 | Process timing. |
| `--heartbeat S` | 20 | Progress line interval. |
| `--clop-bin`, `--clop-app` | auto | Explicit paths. |
| `--clop-arg ARG` | none | Extra flag for `clop optimise`, repeatable. |
| `--no-fresh-start` | off | Do not restart an already running Clop first. |
| `--leave-running` | off | Leave Clop running at the end. |
| `--state`, `--no-resume` | auto | Resume bookkeeping. |
| `--report`, `--log-dir` | auto | JSON report and log location. |
| `--dry-run`, `-y`, `-q`, `-v` | | Plan only, no prompt, quiet, debug. |

Exit codes: 0 done, 1 some files failed, 2 setup error, 130 interrupted.

Progress is recorded per target folder under
`~/Library/Application Support/clop-batch/`, so an interrupted run continues
where it stopped. `--no-resume` reprocesses everything.

## How the limit is handled

Clop's CLI is guarded by an in-memory counter created per process
(`OptimisationCounter` with a limit of 5 in `Clop/OptimisationUtils.swift`). A
slot is taken per file and returned if that file fails. Because the counter is
never written to disk, quitting and reopening the app restores the allowance.

`clop optimise` blocks until every file in the request has reported back, so
waiting for the process to exit is the same as waiting for Clop to finish.
`clop optimise --json` reports per-file results, including the error text used
to detect a reached limit.

Failure handling:

- A batch is capped at 5 files, so a clean run never reaches the limit.
- If the running app had already used some slots, the overflow comes back as
  `Pro limits reached`. Those files are retried after a restart.
- Files that fail for other reasons are retried, then reported at the end.
- Files Clop reports as already optimised count as done.

## Building

Merge the two modules into one dependency-free file and check it:

```bash
python3 build.py --check
python3 dist/clop_batch_gui.py                    # browser UI
python3 dist/clop_batch_gui.py cli DIR --dry-run  # command line
```

Build the app and DMG on macOS:

```bash
./build_app.sh --dmg
```

`build_app.sh` installs PyInstaller into `.build/venv`, bundles `dist/Clop
Batch.app`, signs it ad-hoc, and `make_dmg.sh` writes a compressed, verified
`dist/Clop-Batch.dmg`. PyInstaller cannot cross-compile and `hdiutil` only exists
on macOS, so both steps must run there. The workflows in
`.github/workflows/build-macos.yml` do the same on a macOS runner and publish
the DMG when a `v*` tag is pushed.

## Tests

Both suites use a simulated Clop that enforces the 5 files per launch limit,
returns the same JSON shape, and requires the app to be running:

```bash
python3 tests/test_clop_batch.py
python3 tests/test_gui.py
```

They cover batching, restart behaviour, limit recovery, permanent failures,
resume, timeouts, dry runs, file selection, output-folder handling, cancel, and
delete-originals safety. Point them at the merged build with
`CLOP_BATCH_TARGET` and `CLOP_GUI_TARGET`.

## Notes

- The CLI optimises in place; Clop keeps the originals in its own backup folder.
  The GUI only optimises copies in the output folder.
- The app is signed ad-hoc rather than notarised, so the first launch needs
  right-click then Open.
- This automates restarting the app. It does not patch or modify Clop, and it
  depends on the free-tier design documented above.
