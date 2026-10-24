from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from clop_batch import (
    DEFAULT_EXCLUDED_DIRS,
    DEFAULT_EXTENSIONS,
    IS_MACOS,
    ClopApp,
    ClopCli,
    Logger,
    chunks,
    detect_install,
    discover_files,
    human_bytes,
    human_duration,
    parse_extensions,
)

VERSION = "1.0.0"
APP_TITLE = "Clop Batch"

COMPRESSION_CHOICES: Dict[str, List[str]] = {
    "default": [],
    "quality": ["--compression", "20"],
    "balanced": ["--compression", "50"],
    "smaller": ["--compression", "75"],
    "smallest": ["--compression", "90"],
    "aggressive": ["--aggressive"],
}


def app_data_dir() -> Path:
    if IS_MACOS:
        base = Path.home() / "Library" / "Application Support" / "clop-gui"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "clop-gui"
    base.mkdir(parents=True, exist_ok=True)
    return base


SETTINGS_PATH = app_data_dir() / "settings.json"
HISTORY_PATH = app_data_dir() / "history.json"


class Settings:
    DEFAULTS = {
        "source": "",
        "dest": "",
        "batch_size": 5,
        "extensions": ",".join(DEFAULT_EXTENSIONS),
        "order": "path",
        "compression": "default",
        "retries": 2,
        "timeout": 1800,
        "delete_originals": False,
        "skip_processed": True,
        "include_hidden": False,
        "new_session": True,
        "exclude": "",
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        self.values = dict(self.DEFAULTS)
        self.lock = threading.Lock()
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in self.DEFAULTS:
                    if key in data:
                        self.values[key] = data[key]
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.values, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def update(self, payload: Dict[str, Any]) -> None:
        with self.lock:
            for key, value in payload.items():
                if key in self.DEFAULTS and value is not None:
                    self.values[key] = value
            self.save()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.values)


class History:

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.records: Dict[str, dict] = {}
        self.dirty = False
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                self.records = data["files"]
        except (OSError, json.JSONDecodeError):
            pass

    def save(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"version": 1, "files": self.records},
                                          indent=2, sort_keys=True), encoding="utf-8")
                os.replace(tmp, self.path)
                self.dirty = False
            except OSError:
                pass

    @staticmethod
    def signature(path: Path) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(path)
            return (st.st_size, st.st_mtime_ns)
        except OSError:
            return None

    def is_done(self, src: Path, dest: Path) -> bool:
        with self.lock:
            record = self.records.get(str(src))
        if not record:
            return False
        signature = self.signature(src)
        if signature is None:
            return False
        if record.get("dest_dir") != str(dest):
            return False
        if record.get("size") != signature[0] or record.get("mtime_ns") != signature[1]:
            return False
        try:
            return Path(record.get("dest", "")).is_file()
        except OSError:
            return False

    def mark(self, src: Path, dst: Path, before: int, after: int) -> None:
        signature = self.signature(src)
        with self.lock:
            self.records[str(src)] = {
                "dest": str(dst),
                "dest_dir": str(dst.parent),
                "size": signature[0] if signature else None,
                "mtime_ns": signature[1] if signature else None,
                "before": before,
                "after": after,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.dirty = True

    def count(self) -> int:
        with self.lock:
            return len(self.records)

    def clear(self) -> None:
        with self.lock:
            self.records = {}
            self.dirty = True
        self.save()


@dataclasses.dataclass
class PlanItem:
    src: Path
    dst: Path


class Job:
    def __init__(self, job_id: str, source: Path, dest: Path, options: Dict[str, Any]) -> None:
        self.id = job_id
        self.source = source
        self.dest = dest
        self.options = options
        self.state = "running"
        self.stage = "starting"
        self.message = "Preparing"
        self.total_files = 0
        self.processed = 0
        self.failed = 0
        self.skipped = 0
        self.batches_total = 0
        self.batch_index = 0
        self.bytes_before = 0
        self.bytes_after = 0
        self.errors: List[Dict[str, str]] = []
        self.started_at = time.time()
        self.finished_at = 0.0
        self.app_launches = 0
        self.app_quits = 0

    def snapshot(self) -> Dict[str, Any]:
        elapsed = (self.finished_at or time.time()) - self.started_at
        return {
            "id": self.id,
            "state": self.state,
            "stage": self.stage,
            "message": self.message,
            "source": str(self.source),
            "dest": str(self.dest),
            "total_files": self.total_files,
            "processed": self.processed,
            "failed": self.failed,
            "skipped": self.skipped,
            "batches_total": self.batches_total,
            "batch_index": self.batch_index,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "saved": self.bytes_before - self.bytes_after,
            "errors": self.errors[-50:],
            "elapsed": round(elapsed, 1),
            "elapsed_human": human_duration(elapsed),
            "app_launches": self.app_launches,
            "app_quits": self.app_quits,
            "options": self.options,
        }


class Engine:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.log_lines: List[Dict[str, Any]] = []
        self.log_seq = 0
        self.job: Optional[Job] = None
        self.thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.current_proc: Optional[Any] = None
        self.history = History(HISTORY_PATH)
        self.last_finished: Optional[Job] = None
        self.clop_info: Optional[str] = None
        self.clop_error: Optional[str] = None


    def sink(self, level: str, message: str) -> None:
        with self.log_lock:
            self.log_seq += 1
            self.log_lines.append({
                "seq": self.log_seq,
                "ts": time.strftime("%H:%M:%S"),
                "level": level,
                "text": message,
            })
            if len(self.log_lines) > 4000:
                del self.log_lines[:1000]

    def log_since(self, after: int) -> Dict[str, Any]:
        with self.log_lock:
            lines = [line for line in self.log_lines if line["seq"] > after]
            seq = self.log_seq
        return {"seq": seq, "lines": lines}


    def status(self) -> Dict[str, Any]:
        with self.lock:
            job = self.job
            finished = self.last_finished
        return {
            "version": VERSION,
            "running": bool(job and job.state == "running"),
            "job": job.snapshot() if job else None,
            "last": finished.snapshot() if finished and finished is not job else None,
            "platform": "macOS" if IS_MACOS else sys.platform,
            "clop": self.clop_info,
            "clop_error": self.clop_error,
            "history_count": self.history.count(),
            "settings": settings.snapshot(),
            "compression_choices": list(COMPRESSION_CHOICES),
        }

    def detect(self) -> None:
        probe = Logger(level="warn", color=False, sink=None)
        try:
            install = detect_install(probe)
            self.clop_info = f"{install.app or install.bundle_id} + {install.cli}"
            self.clop_error = None
        except SystemExit as exc:
            self.clop_error = str(exc)
        except Exception as exc:
            self.clop_error = str(exc)


    def start(self, payload: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        with self.lock:
            if self.job is not None and self.job.state == "running":
                return 409, {"error": "a job is already running"}

        settings.update(payload)

        source_raw = str(payload.get("source") or settings.snapshot().get("source") or "").strip()
        dest_raw = str(payload.get("dest") or settings.snapshot().get("dest") or "").strip()
        if not source_raw:
            return 400, {"error": "Choose a folder to optimise"}
        source = Path(source_raw).expanduser().resolve()
        if not source.is_dir():
            return 400, {"error": f"Not a folder: {source}"}

        if not dest_raw:
            dest = source.parent / f"{source.name}-optimised"
        else:
            dest = Path(dest_raw).expanduser().resolve()
        if dest == source:
            return 400, {"error": "The output folder must be different from the source folder"}
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return 400, {"error": f"Cannot create the output folder {dest}: {exc}"}

        try:
            batch_size = max(1, int(payload.get("batch_size") or settings.snapshot().get("batch_size") or 5))
        except (TypeError, ValueError):
            batch_size = 5

        options = {
            "batch_size": batch_size,
            "extensions": parse_extensions([payload.get("extensions")] if payload.get("extensions") else None),
            "order": str(payload.get("order") or "path"),
            "compression": str(payload.get("compression") or "default"),
            "retries": max(0, int(payload.get("retries") or 0)),
            "timeout": float(payload.get("timeout") or 1800),
            "delete_originals": bool(payload.get("delete_originals")),
            "skip_processed": bool(payload.get("skip_processed", True)),
            "include_hidden": bool(payload.get("include_hidden")),
            "new_session": bool(payload.get("new_session", True)),
            "exclude": [part.strip() for part in str(payload.get("exclude") or "").split(",") if part.strip()],
        }
        if options["order"] not in ("path", "mtime", "size"):
            options["order"] = "path"
        if options["compression"] not in COMPRESSION_CHOICES:
            options["compression"] = "default"

        with self.lock:
            self.cancel_event.clear()
            disabled = set(DEFAULT_EXCLUDED_DIRS)
            if dest.is_relative_to(source):
                disabled.add(dest.name)
            options["excluded_dirs"] = sorted(disabled)
            job = Job(uuid.uuid4().hex[:12], source, dest, options)
            self.job = job
            self.thread = threading.Thread(target=self._run, args=(job,), name="clop-job", daemon=True)
        self.sink("info", f"job {job.id}: {source} -> {dest}")
        self.thread.start()
        return 200, {"job": job.id}

    def cancel(self) -> Tuple[int, Dict[str, Any]]:
        with self.lock:
            job = self.job
            proc = self.current_proc
        if job is None or job.state != "running":
            return 409, {"error": "no job is running"}
        self.cancel_event.set()
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self.sink("warn", "cancel requested; finishing the file in flight")
        return 200, {"ok": True}


    @staticmethod
    def _unique_path(directory: Path, name: str, taken: set) -> Path:
        stem, suffix = os.path.splitext(name)
        candidate = directory / name
        counter = 2
        while str(candidate) in taken or candidate.exists():
            candidate = directory / f"{stem} ({counter}){suffix}"
            counter += 1
        taken.add(str(candidate))
        return candidate

    def _build_plan(self, job: Job, files: Sequence[Path], log: Logger) -> List[PlanItem]:
        taken: set = set()
        plan: List[PlanItem] = []
        skipped = 0
        for src in files:
            if job.options["skip_processed"] and self.history.is_done(src, job.dest):
                skipped += 1
                continue
            rel = os.path.relpath(str(src), str(job.source))
            target_dir = job.dest / os.path.dirname(rel)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                log.warn(f"cannot create {target_dir}: {exc}")
                continue
            plan.append(PlanItem(src=src, dst=self._unique_path(target_dir, src.name, taken)))
        job.skipped = skipped
        if skipped:
            log.info(f"skipping {skipped} file(s) already optimised into this folder before")
        return plan


    def _run(self, job: Job) -> None:
        log = Logger(level="info", color=False, sink=self.sink)
        try:
            self._run_inner(job, log)
        except BaseException as exc:
            with self.lock:
                job.state = "error"
                job.stage = "error"
                job.message = f"unexpected error: {exc}"
                job.finished_at = time.time()
                self.last_finished = job
            log.error(f"unexpected error: {exc}")
            log.debug(traceback.format_exc())
            self.history.save()
            return
        finally:
            with self.lock:
                self.current_proc = None

    def _run_inner(self, job: Job, log: Logger) -> None:
        options = job.options
        log.info(f"scanning {job.source}")
        files = discover_files(
            root=job.source,
            extensions=options["extensions"],
            excluded_dirs=options.get("excluded_dirs") or DEFAULT_EXCLUDED_DIRS,
            exclude_globs=options.get("exclude") or [],
            include_hidden=options["include_hidden"],
            order=options["order"],
            reverse=False,
            max_files=None,
            log=log,
        )

        files = [f for f in files if not f.is_relative_to(job.dest)]
        log.info(f"found {len(files)} matching file(s)")

        plan = self._build_plan(job, files, log)
        job.total_files = len(plan)
        if not plan:
            with self.lock:
                job.state = "done"
                job.stage = "done"
                job.message = "Nothing new to process"
                job.finished_at = time.time()
                self.last_finished = job
            log.info("nothing new to process")
            return

        total_bytes = 0
        for item in plan:
            try:
                total_bytes += item.src.stat().st_size
            except OSError:
                pass
        try:
            free = shutil.disk_usage(job.dest).free
            if free < total_bytes * 1.1:
                log.warn(
                    f"the output volume has {human_bytes(free)} free but the copies need about "
                    f"{human_bytes(total_bytes)}; the job may run out of space"
                )
        except OSError:
            pass

        batches = list(chunks(plan, options["batch_size"]))
        job.batches_total = len(batches)

        probe = Logger(level="warn", color=False, sink=None)
        install = detect_install(probe)
        self.clop_info = f"{install.app or install.bundle_id} + {install.cli}"
        self.clop_error = None

        app = ClopApp(install, log, quit_timeout=60, launch_timeout=60, settle=2.0)
        cli = ClopCli(
            install.cli, log,
            extra_args=COMPRESSION_CHOICES.get(options["compression"], []),
            heartbeat=20.0,
            on_proc=self._remember_proc,
        )

        log.info(
            f"plan: {len(plan)} file(s) in {len(batches)} batch(es) of {options['batch_size']}, "
            f"output {job.dest}"
        )

        if options.get("new_session", True) and app.is_running():
            log.info("Clop is already running; restarting it so batch 1 gets the full allowance")
            app.quit(reason="fresh session for batch 1")

        cancelled = False
        for index, batch in enumerate(batches, 1):
            if self.cancel_event.is_set():
                cancelled = True
                break
            with self.lock:
                job.batch_index = index
            log.info(f"batch {index}/{len(batches)}: copying {len(batch)} file(s) to the output folder")
            with self.lock:
                job.stage = "copying"
                job.message = f"Copying batch {index} of {len(batches)}"
            copied, copy_failed = self._copy_batch(batch, log)

            for item, error in copy_failed:
                self._record_failure(job, item, error, log)

            if copied and not self.cancel_event.is_set():
                self._optimise_batch(job, app, cli, copied, index, len(batches), log)
            else:
                for item in copied:
                    self._record_failure(job, item, "cancelled before optimisation", log)

            self.history.save()

            if not self.cancel_event.is_set():
                with self.lock:
                    job.stage = "resetting"
                    job.message = "Restarting Clop for a fresh free-tier session"
                if not app.quit(reason="free-tier session reset"):
                    log.error("Clop could not be stopped; stopping the job")
                    break
                job.app_quits = app.quits

        if cancelled or self.cancel_event.is_set():
            with self.lock:
                job.state = "cancelled"
                job.stage = "cancelled"
                job.message = "Cancelled"
                job.finished_at = time.time()
                self.last_finished = job
            log.warn(f"cancelled after {job.processed} file(s)")
            return

        with self.lock:
            job.state = "done"
            job.stage = "done"
            job.message = (
                f"Optimised {job.processed} file(s)"
                + (f", {job.failed} failed" if job.failed else "")
                + f" - saved {human_bytes(job.bytes_before - job.bytes_after)}"
            )
            job.finished_at = time.time()
            self.last_finished = job
        log.info(job.message)
        log.info(f"output folder: {job.dest}")

    def _remember_proc(self, proc: Any) -> None:
        with self.lock:
            self.current_proc = proc

    def _copy_batch(self, batch: Sequence[PlanItem],
                    log: Logger) -> Tuple[List[PlanItem], List[Tuple[PlanItem, str]]]:
        copied: List[PlanItem] = []
        failed: List[Tuple[PlanItem, str]] = []
        for item in batch:
            if self.cancel_event.is_set():
                failed.append((item, "cancelled"))
                continue
            try:
                item.dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item.src, item.dst)
                copied.append(item)
            except OSError as exc:
                failed.append((item, f"copy failed: {exc}"))
                log.warn(f"copy failed for {item.src.name}: {exc}")
        log.info(f"copied {len(copied)} file(s)")
        return copied, failed

    def _record_failure(self, job: Job, item: PlanItem, error: str, log: Logger) -> None:
        with self.lock:
            job.failed += 1
            job.errors.append({"file": str(item.src), "error": error})
        log.error(f"  x {item.src.name}: {error}")

    def _optimise_batch(self, job: Job, app: ClopApp, cli: ClopCli,
                        items: List[PlanItem], index: int, total_batches: int,
                        log: Logger) -> None:
        pending = list(items)
        attempts = 1 + max(0, job.options["retries"])
        attempt = 0
        outcome_done: Dict[Path, Tuple[int, int]] = {}
        outcome_benign: Dict[Path, str] = {}

        while pending and attempt < attempts:
            attempt += 1
            with self.lock:
                job.stage = "optimising"
                job.message = (
                    f"Batch {index} of {total_batches}"
                    + (f" - attempt {attempt}/{attempts}" if attempt > 1 else "")
                )
            if not app.ensure_running():
                for item in pending:
                    self._record_failure(job, item, "could not start Clop", log)
                return
            job.app_launches = app.launches

            targets = [item.dst for item in pending]
            result = cli.run(targets, job.options["timeout"])
            outcome = cli.interpret(result, targets)
            outcome_done.update(outcome.done)
            outcome_benign.update(outcome.benign)

            if result.timed_out:
                log.warn("that batch exceeded the timeout and was cancelled")

            unresolved = [(item, error) for item in pending
                          for path, error in outcome.failed.items() if path == item.dst]
            if outcome.limit_hit:
                log.warn("Clop reported its free-tier limit; restarting gives the leftovers a fresh session")


            mentioned = set(outcome.done) | set(outcome.benign) | set(outcome.failed)
            for item in pending:
                if item.dst not in mentioned and not result.timed_out:
                    unresolved.append((item, "clop reported no result"))

            if not unresolved:
                break
            if attempt >= attempts or self.cancel_event.is_set():
                for item, error in unresolved:
                    self._record_failure(job, item, error, log)
                break

            log.info(f"retrying {len(unresolved)} file(s) with a fresh Clop session")
            if not app.quit(reason="fresh session for the retry"):
                for item, error in unresolved:
                    self._record_failure(job, item, "could not restart Clop", log)
                break
            job.app_quits = app.quits
            time.sleep(2)
            pending = [item for item, _ in unresolved]
            unresolved = []


        for item in items:
            verified, before, after = self._verify(item, outcome_done, outcome_benign)
            if not verified:
                if item.dst not in outcome_done and item.dst not in outcome_benign:
                    continue
                self._record_failure(job, item, "output missing or unchanged", log)
                continue

            with self.lock:
                job.processed += 1
                job.bytes_before += before
                job.bytes_after += after
            self.history.mark(item.src, item.dst, before, after)
            saved_pct = (1 - after / before) * 100 if before else 0
            log.info(
                f"  + {item.dst.relative_to(job.dest)}  {human_bytes(before)} -> "
                f"{human_bytes(after)}  (-{saved_pct:.1f}%)"
            )
            if job.options["delete_originals"]:
                try:
                    item.src.unlink()
                    log.debug(f"deleted original {item.src}")
                except OSError as exc:
                    log.warn(f"could not delete {item.src}: {exc}")

    def _verify(self, item: PlanItem, done: Dict[Path, Tuple[int, int]],
                benign: Dict[Path, str]) -> Tuple[bool, int, int]:
        before = 0
        try:
            before = item.src.stat().st_size
        except OSError:
            pass
        if item.dst in done:
            old, new = done[item.dst]
            try:
                after = item.dst.stat().st_size
            except OSError:
                after = new
            return True, (old or before), after
        if item.dst in benign:
            try:
                after = item.dst.stat().st_size
            except OSError:
                after = before
            return True, before, after

        try:
            after = item.dst.stat().st_size
        except OSError:
            return False, before, 0
        if before and after and after != before:
            return True, before, after
        return False, before, after


settings = Settings(SETTINGS_PATH)
engine = Engine()


def folder_picker(initial: str = "") -> Optional[str]:
    if IS_MACOS and shutil.which("osascript"):
        script = 'POSIX path of (choose folder with prompt "Choose a folder"'
        if initial and Path(initial).expanduser().is_dir():
            script += f' default location (POSIX file "{initial}")'
        script += ")"
        try:
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                picked = result.stdout.strip()
                return picked or None
            return None
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        picked = filedialog.askdirectory(initialdir=initial or str(Path.home()))
        root.destroy()
        return picked or None
    except Exception:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = f"clop-gui/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("CLOP_GUI_HTTP_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1_000_000))
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json(200, engine.status())
        elif path == "/api/log":
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._json(200, engine.log_since(after))
        elif path == "/api/ping":
            self._json(200, {"ok": True, "version": VERSION})
        elif path == "/favicon.ico":
            self._send(200, FAVICON, "image/svg+xml")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        payload = self._read_json()

        if path == "/api/start":
            status, body = engine.start(payload)
            self._json(status, body)
        elif path == "/api/cancel":
            status, body = engine.cancel()
            self._json(status, body)
        elif path == "/api/settings":
            settings.update(payload)
            self._json(200, {"settings": settings.snapshot()})
        elif path == "/api/browse":
            kind = str(payload.get("kind") or "source")
            initial = str(payload.get("initial") or "")
            picked = folder_picker(initial)
            self._json(200, {"path": picked, "kind": kind})
        elif path == "/api/reveal":
            target = str(payload.get("path") or "")
            if not target:
                self._json(400, {"error": "no path"})
                return
            folder = Path(target).expanduser()
            try:
                if IS_MACOS:
                    subprocess.run(["open", "-R", str(folder)], capture_output=True, timeout=30)
                elif sys.platform.startswith("linux"):
                    subprocess.run(["xdg-open", str(folder)], capture_output=True, timeout=30)
                elif sys.platform == "win32":
                    os.startfile(str(folder))
            except (OSError, subprocess.SubprocessError):
                pass
            self._json(200, {"ok": True})
        elif path == "/api/history/clear":
            engine.history.clear()
            self._json(200, {"ok": True, "history_count": 0})
        elif path == "/api/shutdown":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._json(404, {"error": "not found"})


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clop Batch</title>
<style>
  :root{
    --bg:#0f1216; --panel:#161b22; --panel2:#1c2330; --line:#2a3444;
    --text:#e6edf3; --muted:#8b98a9; --accent:#4c9aff; --green:#3fb950;
    --red:#f85149; --amber:#d29922; --radius:10px;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;}
  .wrap{max-width:980px;margin:0 auto;padding:28px 20px 60px}
  header{display:flex;align-items:center;gap:12px;margin-bottom:22px;flex-wrap:wrap}
  h1{font-size:20px;margin:0;font-weight:650;letter-spacing:-.01em}
  .pill{font-size:12px;padding:3px 10px;border-radius:999px;background:var(--panel2);
    border:1px solid var(--line);color:var(--muted)}
  .pill.ok{color:var(--green);border-color:#1f4d2b;background:#12241a}
  .pill.bad{color:var(--red);border-color:#5a2222;background:#241416}
  .pill.run{color:var(--accent);border-color:#1d3a5f;background:#111d2c}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
    padding:18px;margin-bottom:16px}
  .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
    margin:0 0 14px;font-weight:600}
  label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}
  .row{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap}
  .row>*{min-width:0}
  .grow{flex:1 1 320px}
  input[type=text],input[type=number],select{width:100%;padding:9px 11px;border-radius:8px;
    border:1px solid var(--line);background:#0d1117;color:var(--text);font-size:13px;font-family:inherit}
  input[type=text]:focus,select:focus,input[type=number]:focus{outline:none;border-color:var(--accent)}
  button{padding:9px 16px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);
    color:var(--text);font-size:13px;font-weight:550;cursor:pointer;font-family:inherit;
    transition:background .12s,border-color .12s,opacity .12s}
  button:hover:not(:disabled){background:#232c3a;border-color:#3a475b}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.primary{background:var(--accent);border-color:var(--accent);color:#04101f}
  button.primary:hover:not(:disabled){background:#63a9ff;border-color:#63a9ff}
  button.danger{color:var(--red);border-color:#5a2222;background:#241416}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .check{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--text);
    padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:#0d1117;cursor:pointer}
  .check input{accent-color:var(--accent);margin:0}
  .hint{font-size:12px;color:var(--muted);margin-top:-6px;margin-bottom:12px}
  .bar{height:8px;border-radius:999px;background:#0d1117;border:1px solid var(--line);overflow:hidden}
  .bar>div{height:100%;width:0;background:linear-gradient(90deg,#2f81f7,#4c9aff);
    transition:width .35s ease}
  .status{display:flex;justify-content:space-between;gap:12px;font-size:12.5px;
    color:var(--muted);margin-bottom:8px;flex-wrap:wrap}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px;margin-top:16px}
  .stat{background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
  .stat .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
  .stat .v{font-size:16px;font-weight:600;margin-top:3px}
  .stat .v.green{color:var(--green)}
  .stat .v.red{color:var(--red)}
  #log{height:260px;overflow:auto;background:#0a0d12;border:1px solid var(--line);
    border-radius:8px;padding:10px 12px;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
    white-space:pre-wrap;word-break:break-word}
  #log div{display:flex;gap:10px}
  #log .t{color:#4c5666;flex:none}
  #log .m{flex:1}
  #log .warn .m{color:var(--amber)}
  #log .error .m{color:var(--red)}
  #log .debug .m{color:#6e7b8c}
  .footer{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
  .muted{color:var(--muted);font-size:12px}
  .banner{border:1px solid #5a2222;background:#241416;color:#ffb4b0;border-radius:8px;
    padding:10px 12px;font-size:12.5px;margin-bottom:16px;display:none}
  .banner code{color:#fff;font-size:12px}
  a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Clop Batch</h1>
    <span class="pill" id="platform">-</span>
    <span class="pill" id="state">idle</span>
    <span class="pill" id="clop">checking Clop…</span>
  </header>

  <div class="banner" id="banner"></div>

  <div class="card">
    <h2>Folders</h2>
    <div class="row">
      <div class="grow">
        <label for="source">Folder to optimise (scanned recursively)</label>
        <input type="text" id="source" placeholder="/Users/you/Pictures/export" autocomplete="off">
      </div>
      <div style="display:flex;align-items:flex-end"><button id="browseSource">Browse…</button></div>
    </div>
    <div class="row">
      <div class="grow">
        <label for="dest">Output folder for the optimised copies</label>
        <input type="text" id="dest" placeholder="Leave empty for &lt;folder&gt;-optimised next to the source" autocomplete="off">
      </div>
      <div style="display:flex;align-items:flex-end"><button id="browseDest">Browse…</button></div>
    </div>
    <div class="hint">The originals are copied here first, then Clop optimises the copies. The source folder is left alone unless you tick “delete originals”.</div>
  </div>

  <div class="card">
    <h2>Options</h2>
    <div class="grid">
      <div>
        <label for="batchSize">Files per Clop session</label>
        <input type="number" id="batchSize" min="1" max="200" step="1" value="5">
      </div>
      <div>
        <label for="compression">Compression</label>
        <select id="compression">
          <option value="default">Clop's own setting</option>
          <option value="quality">Best quality (20)</option>
          <option value="balanced">Balanced (50)</option>
          <option value="smaller">Smaller (75)</option>
          <option value="smallest">Smallest (90)</option>
          <option value="aggressive">Aggressive preset</option>
        </select>
      </div>
      <div>
        <label for="order">Order</label>
        <select id="order">
          <option value="path">Path (A→Z)</option>
          <option value="mtime">Oldest first</option>
          <option value="size">Smallest first</option>
        </select>
      </div>
      <div>
        <label for="retries">Retries per batch</label>
        <input type="number" id="retries" min="0" max="10" step="1" value="2">
      </div>
    </div>
    <div class="row" style="margin-top:14px">
      <div class="grow">
        <label for="extensions">File extensions (comma separated)</label>
        <input type="text" id="extensions" autocomplete="off">
      </div>
    </div>
    <div class="row">
      <div class="grow">
        <label for="exclude">Exclude globs (comma separated, optional)</label>
        <input type="text" id="exclude" placeholder="*.tmp, *_raw.png" autocomplete="off">
      </div>
    </div>
    <div class="grid">
      <label class="check"><input type="checkbox" id="newSession"> Restart Clop before batch 1</label>
      <label class="check"><input type="checkbox" id="deleteOriginals"> Delete originals after success</label>
      <label class="check"><input type="checkbox" id="skipProcessed"> Skip already processed</label>
      <label class="check"><input type="checkbox" id="includeHidden"> Include hidden files</label>
    </div>
    <div class="footer">
      <button class="primary" id="start">Start</button>
      <button class="danger" id="cancel" disabled>Cancel</button>
      <button id="openDest">Open output folder</button>
      <button id="clearHistory" title="Forget which files were already processed">Forget processed list</button>
    </div>
  </div>

  <div class="card">
    <h2>Progress</h2>
    <div class="status">
      <span id="stage">Waiting to start</span>
      <span id="counts"></span>
    </div>
    <div class="bar"><div id="barFill"></div></div>
    <div class="stats">
      <div class="stat"><div class="k">Files</div><div class="v" id="sFiles">0 / 0</div></div>
      <div class="stat"><div class="k">Saved</div><div class="v green" id="sSaved">-</div></div>
      <div class="stat"><div class="k">Failed</div><div class="v" id="sFailed">0</div></div>
      <div class="stat"><div class="k">Batches</div><div class="v" id="sBatches">0 / 0</div></div>
      <div class="stat"><div class="k">Elapsed</div><div class="v" id="sElapsed">-</div></div>
    </div>
  </div>

  <div class="card">
    <h2>Log</h2>
    <div id="log"></div>
  </div>

  <p class="muted">Local only: this page talks to a server bound to <span id="host">127.0.0.1</span>.
  Clop is quit and reopened between batches so its per-session free-tier counter resets.</p>
</div>

<script>
(function(){
  "use strict";
  const $ = (id) => document.getElementById(id);
  let lastSeq = 0, running = false, currentJob = null, formTouched = false;

  function fmtBytes(n){
    if(n === null || n === undefined || isNaN(n)) return "-";
    const neg = n < 0; n = Math.abs(n);
    const units = ["B","KB","MB","GB","TB"]; let i = 0;
    while(n >= 1000 && i < units.length-1){ n /= 1000; i++; }
    return (neg?"-":"") + (i===0 ? n.toFixed(0)+" B" : n.toFixed(1)+" "+units[i]);
  }

  async function api(path, body){
    const opts = body === undefined ? {} : {
      method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)
    };
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch(e) {}
    if(!res.ok) throw new Error(data.error || ("request failed: " + res.status));
    return data;
  }

  function fillForm(s){
    if(document.activeElement && document.activeElement.tagName === "INPUT") return;
    $("source").value = s.source || "";
    $("dest").value = s.dest || "";
    $("batchSize").value = s.batch_size || 5;
    $("extensions").value = s.extensions || "";
    $("order").value = s.order || "path";
    $("compression").value = s.compression || "default";
    $("retries").value = s.retries ?? 2;
    $("exclude").value = s.exclude || "";
    $("deleteOriginals").checked = !!s.delete_originals;
    $("skipProcessed").checked = s.skip_processed !== false;
    $("includeHidden").checked = !!s.include_hidden;
    $("newSession").checked = s.new_session !== false;
  }

  function render(job, last){
    const j = job || last;
    if(!job && !last){
      $("stage").textContent = "Waiting to start";
      return;
    }
    if(!j) return;
    const total = j.total_files || 0;
    const doneCount = (j.processed || 0) + (j.failed || 0);
    const pct = total ? Math.min(100, Math.round(doneCount / total * 100)) : 0;
    $("barFill").style.width = pct + "%";
    $("stage").textContent = j.message || j.stage || "";
    $("counts").textContent = total
      ? `${doneCount} / ${total} files (${pct}%)`
      : "";
    $("sFiles").textContent = `${doneCount} / ${total}`;
    $("sSaved").textContent = j.bytes_before ? fmtBytes(j.saved) : "-";
    $("sFailed").textContent = String(j.failed || 0);
    $("sFailed").className = "v" + ((j.failed||0) > 0 ? " red" : "");
    $("sBatches").textContent = `${j.batch_index||0} / ${j.batches_total||0}`;
    $("sElapsed").textContent = j.elapsed_human || "-";
  }

  function setState(s){
    const pill = $("state");
    pill.textContent = s.running ? "running" : (s.job ? s.job.state : "idle");
    pill.className = "pill " + (s.running ? "run" : (s.job && s.job.state === "done") ? "ok" :
                     (s.job && s.job.state === "error") ? "bad" : "");
    running = s.running;
    $("start").disabled = s.running;
    $("cancel").disabled = !s.running;
    ["source","dest","batchSize","extensions","order","compression","retries","exclude",
     "deleteOriginals","skipProcessed","includeHidden","newSession","browseSource","browseDest",
     "openDest","clearHistory"].forEach(id => { const el=$(id); if(el) el.disabled = s.running; });
  }

  function renderClop(s){
    const pill = $("clop");
    if(s.clop){ pill.textContent = "Clop ready"; pill.className = "pill ok"; pill.title = s.clop; }
    else { pill.textContent = "Clop not found"; pill.className = "pill bad"; pill.title = s.clop_error || ""; }
    $("platform").textContent = s.platform;
    const banner = $("banner");
    if(!s.clop && s.clop_error){
      banner.style.display = "block";
      banner.innerHTML = "Clop's command line tool was not found, so batches cannot run. " +
        "In Clop: menu bar icon → <em>Install command line integration</em>, then reload this page.";
    } else banner.style.display = "none";
  }

  async function pollStatus(){
    try{
      const s = await api("/api/status");
      currentJob = s.job ? s.job.id : null;
      render(s.job, s.last);
      renderClop(s);
      setState(s);
      if(!formTouched) fillForm(s.settings);
      $("host").textContent = location.host;
    }catch(e){}
  }

  async function pollLog(){
    try{
      const data = await api("/api/log?after=" + lastSeq);
      if(data.lines && data.lines.length){
        const box = $("log");
        const frag = document.createDocumentFragment();
        data.lines.forEach(line => {
          const row = document.createElement("div");
          row.className = line.level;
          const t = document.createElement("span"); t.className = "t"; t.textContent = line.ts;
          const m = document.createElement("span"); m.className = "m"; m.textContent = line.text;
          row.appendChild(t); row.appendChild(m);
          frag.appendChild(row);
        });
        const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
        box.appendChild(frag);
        lastSeq = data.seq;
        while(box.childElementCount > 1500) box.removeChild(box.firstChild);
        if(stick) box.scrollTop = box.scrollHeight;
      }
    }catch(e){}
  }

  ["source","dest","batchSize","extensions","order","compression","retries","exclude",
   "deleteOriginals","skipProcessed","includeHidden","newSession"].forEach(id => {
    const el = $(id);
    if(el) el.addEventListener("change", () => { formTouched = true; });
    if(el) el.addEventListener("input", () => { formTouched = true; });
  });

  function payload(){ return {
    source: $("source").value.trim(),
    dest: $("dest").value.trim(),
    batch_size: parseInt($("batchSize").value || "5", 10),
    extensions: $("extensions").value.trim(),
    order: $("order").value,
    compression: $("compression").value,
    retries: parseInt($("retries").value || "0", 10),
    exclude: $("exclude").value.trim(),
    delete_originals: $("deleteOriginals").checked,
    skip_processed: $("skipProcessed").checked,
    include_hidden: $("includeHidden").checked,
    new_session: $("newSession").checked
  };}

  $("start").addEventListener("click", async () => {
    const p = payload();
    if(!p.source){ alert("Choose a folder to optimise first."); return; }
    if(p.delete_originals && !confirm("Delete the original files once their optimised copies are verified?")) return;
    try{
      $("start").disabled = true;
      await api("/api/start", p);
      formTouched = false;
      await pollStatus();
    }catch(e){ alert(e.message); $("start").disabled = false; }
  });

  $("cancel").addEventListener("click", async () => {
    if(!confirm("Stop after the file currently being processed?")) return;
    try{ await api("/api/cancel", {}); }catch(e){ alert(e.message); }
  });

  $("browseSource").addEventListener("click", async () => {
    try{
      const r = await api("/api/browse", {kind:"source", initial: $("source").value});
      if(r.path){ $("source").value = r.path; formTouched = true;
        if(!$("dest").value){
          const base = r.path.replace(/\/+$/, "");
          $("dest").value = base + "-optimised";
        }
      }
    }catch(e){ alert(e.message); }
  });

  $("browseDest").addEventListener("click", async () => {
    try{
      const r = await api("/api/browse", {kind:"dest", initial: $("dest").value});
      if(r.path){ $("dest").value = r.path; formTouched = true; }
    }catch(e){ alert(e.message); }
  });

  $("openDest").addEventListener("click", async () => {
    const p = $("dest").value.trim() || $("source").value.trim();
    if(!p){ alert("No folder chosen yet."); return; }
    try{ await api("/api/reveal", {path: p}); }catch(e){ alert(e.message); }
  });

  $("clearHistory").addEventListener("click", async () => {
    if(!confirm("Forget which files were already processed? Re-running will optimise everything again.")) return;
    try{ await api("/api/history/clear", {}); await pollStatus(); }catch(e){ alert(e.message); }
  });

  pollStatus();
  setInterval(pollStatus, 1000);
  setInterval(pollLog, 700);
  pollLog();
})();
</script>
</body>
</html>
"""

FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    b'<rect width="64" height="64" rx="14" fill="#161b22"/>'
    b'<path d="M20 24h24M20 32h24M20 40h16" stroke="#4c9aff" stroke-width="5" '
    b'stroke-linecap="round"/></svg>'
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clop_gui.py",
        description="Local web UI that batch-optimises a folder through Clop, "
                    "quitting and reopening it every 5 files to reset the free tier.",
    )
    parser.add_argument("--port", type=int, default=8765, help="port to listen on (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1; use 0.0.0.0 to expose it)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument("--allow-non-macos", action="store_true",
                        help="run on a non-macOS host (the UI works; Clop will not be found)")
    parser.add_argument("--version", action="version", version=f"clop_gui {VERSION}")
    args = parser.parse_args(argv)

    if not IS_MACOS and not args.allow_non_macos:
        print("This drives the macOS app Clop; run it on macOS.\n"
              "(Pass --allow-non-macos to open the UI anyway, e.g. for a demo.)", file=sys.stderr)
        return 2

    engine.detect()

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"Could not listen on {args.host}:{args.port}: {exc}\n"
              f"Try another port: python3 clop_gui.py --port {args.port + 1}", file=sys.stderr)
        return 2
    server.daemon_threads = True

    host_for_browser = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{host_for_browser}:{args.port}/"
    print(f"clop_gui {VERSION} listening on http://{args.host}:{args.port}/")
    if engine.clop_info:
        print(f"Clop: {engine.clop_info}")
    else:
        print("Clop: not found yet - install the CLI from Clop's menu bar icon", file=sys.stderr)
    print("Press Ctrl-C to stop.")

    if not args.no_open:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()
        settings.save()
        engine.history.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
