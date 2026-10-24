from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

VERSION = "1.0.0"

APP_NAME = "Clop"
BUNDLE_IDS = ("com.lowtechguys.Clop", "com.lowtechguys.Clop-setapp")
PROCESS_NAMES = ("Clop", "Clop-setapp")

APP_CANDIDATES = (
    "/Applications/Clop.app",
    "~/Applications/Clop.app",
    "/Applications/Setapp/Clop.app",
    "~/Applications/Setapp/Clop.app",
)

CLI_CANDIDATES = (
    "~/.local/bin/clop",
    "/opt/homebrew/bin/clop",
    "/usr/local/bin/clop",
)

DEFAULT_EXTENSIONS = (

    "png", "jpg", "jpeg", "gif", "tif", "tiff", "bmp", "heic", "heif",
    "webp", "avif", "jxl",

    "mov", "mp4", "m4v", "avi", "mkv", "webm", "mpg", "mpeg", "wmv", "flv", "3gp",

    "mp3", "wav", "aif", "aiff", "flac", "opus", "ogg", "m4a", "aac",

    "pdf",
)

DEFAULT_EXCLUDED_DIRS = (
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".Trash", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
)


LIMIT_PATTERNS = (
    "pro limits reached",
    "pro limit reached",
    "limit reached",
    "upgrade to pro",
    "pro version",
)


BENIGN_PATTERNS = (
    "already optimis",
    "already optimi",
    "already resized",
    "already compressed",
    "no optimisation needed",
    "no optimization needed",
)

NOT_RUNNING_PATTERNS = (
    "is not running",
    "not running",
    "no such app",
)

IS_MACOS = sys.platform == "darwin"


def human_bytes(num: float) -> str:
    value = float(num)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "B":
                return f"{sign}{value:.0f} B"
            return f"{sign}{value:.1f} {unit}"
        value /= 1000.0
    return f"{sign}{value:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def chunks(seq: Sequence[Path], size: int) -> Iterator[List[Path]]:
    for start in range(0, len(seq), size):
        yield list(seq[start:start + size])


def shorten(path: str, root: Optional[Path] = None, width: int = 78) -> str:
    shown = path
    if root is not None:
        try:
            rel = os.path.relpath(path, str(root))
            if not rel.startswith(".."):
                shown = rel
        except ValueError:
            pass
    if len(shown) > width:
        shown = "..." + shown[-(width - 3):]
    return shown


class Logger:

    LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
    COLORS = {"debug": "\033[2m", "info": "\033[36m", "warn": "\033[33m", "error": "\033[31m"}
    RESET = "\033[0m"

    def __init__(self, level: str = "info", color: Optional[bool] = None,
                 file_path: Optional[Path] = None,
                 sink: Optional[Callable[[str, str], None]] = None) -> None:
        self.sink = sink
        self.threshold = self.LEVELS.get(level, 20)
        if color is None:
            color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
        self.color = color
        self.fh = None
        if file_path is not None:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            self.fh = open(file_path, "a", encoding="utf-8")

    def close(self) -> None:
        if self.fh:
            self.fh.close()
            self.fh = None

    def _emit(self, level: str, message: str) -> None:
        if self.LEVELS[level] < self.threshold:
            return
        stamp = time.strftime("%H:%M:%S")
        plain = f"[{stamp}] {level.upper():<5} {message}"
        if self.sink is not None:
            try:
                self.sink(level, message)
            except Exception:
                pass
        if self.fh:
            self.fh.write(plain + "\n")
            self.fh.flush()
        if self.color:
            print(f"\033[2m[{stamp}]\033[0m {self.COLORS[level]}{level.upper():<5}{self.RESET} {message}")
        else:
            print(plain)

    def debug(self, msg: str) -> None:
        self._emit("debug", msg)

    def info(self, msg: str) -> None:
        self._emit("info", msg)

    def warn(self, msg: str) -> None:
        self._emit("warn", msg)

    def error(self, msg: str) -> None:
        self._emit("error", msg)

    def plain(self, msg: str = "") -> None:
        if self.sink is not None:
            try:
                self.sink("plain", msg)
            except Exception:
                pass
        if self.fh:
            self.fh.write(msg + "\n")
            self.fh.flush()
        print(msg)


@dataclasses.dataclass
class ClopInstall:
    cli: Path
    app: Optional[Path]
    bundle_id: Optional[str]


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def _bundle_id_of(app: Path) -> Optional[str]:
    info = app / "Contents" / "Info.plist"
    try:
        with open(info, "rb") as handle:
            data = plistlib.load(handle)
        bundle = data.get("CFBundleIdentifier")
        return str(bundle) if bundle else None
    except Exception:
        return None


def detect_install(log: Logger, cli_override: Optional[str] = None,
                   app_override: Optional[str] = None) -> ClopInstall:
    if cli_override and not Path(cli_override).expanduser().is_file():
        raise SystemExit(f"--clop-bin does not point to a file: {cli_override}")
    if app_override and not Path(app_override).expanduser().is_dir():
        raise SystemExit(f"--clop-app does not point to an app bundle: {app_override}")

    cli: Optional[Path] = None
    for candidate in (cli_override, os.environ.get("CLOP_BIN"), shutil.which("clop")):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file():
                cli = path.resolve()
                break

    app: Optional[Path] = None
    app_candidates: List[Path] = []
    if app_override:
        app_candidates.append(Path(app_override).expanduser())
    if os.environ.get("CLOP_APP"):
        app_candidates.append(Path(os.environ["CLOP_APP"]).expanduser())
    app_candidates.extend(_expand(p) for p in APP_CANDIDATES)

    for path in app_candidates:
        if path.is_dir() and (path / "Contents" / "Info.plist").is_file():
            app = path
            break

    if cli is None:
        for path in (_expand(p) for p in CLI_CANDIDATES):
            if path.is_file():
                cli = path
                break
    if cli is None:
        for path in app_candidates:
            bundled = path / "Contents" / "SharedSupport" / "ClopCLI"
            if bundled.is_file():
                cli = bundled
                break

    if cli is None:
        raise SystemExit(
            "Could not find the `clop` command-line tool.\n"
            "  Install it from Clop's menu bar icon -> 'Install command line integration',\n"
            "  or point the script at it:  --clop-bin /Applications/Clop.app/Contents/SharedSupport/ClopCLI"
        )

    if app is None:


        try:
            parts = cli.parts
            idx = parts.index("Contents")
            if idx >= 1 and parts[idx - 1] in ("Clop.app", "Clop-Setapp.app"):
                app = Path(*parts[:idx])
        except ValueError:
            app = None

    bundle_id = _bundle_id_of(app) if app else None
    if bundle_id is None:
        bundle_id = BUNDLE_IDS[0]

    log.info(f"clop CLI: {cli}")
    log.info(f"Clop app: {app if app else '(not found, will launch by bundle id ' + bundle_id + ')'}")
    return ClopInstall(cli=cli, app=app, bundle_id=bundle_id)


class ClopApp:

    def __init__(self, install: ClopInstall, log: Logger,
                 quit_timeout: float = 60.0, launch_timeout: float = 60.0,
                 settle: float = 2.0) -> None:
        self.install = install
        self.log = log
        self.quit_timeout = quit_timeout
        self.launch_timeout = launch_timeout
        self.settle = settle
        self._pgrep = shutil.which("pgrep") or "/usr/bin/pgrep"
        self._pgrep_reliable = True
        self._killall = shutil.which("killall") or "/usr/bin/killall"
        self._osascript = shutil.which("osascript")
        self._open = shutil.which("open") or "/usr/bin/open"
        self.launches = 0
        self.quits = 0


    def pids(self) -> List[int]:
        found: List[int] = []
        self._pgrep_reliable = True
        for name in PROCESS_NAMES:
            found.extend(self._pgrep_args("-x", name))
        if self.install.app is not None:
            found.extend(self._pgrep_args("-f", str(self.install.app / "Contents" / "MacOS")))
        return sorted(set(found))

    def _pgrep_args(self, *args: str) -> List[int]:
        try:
            result = subprocess.run([self._pgrep, *args], capture_output=True,
                                    text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            self._pgrep_reliable = False
            return []
        if result.returncode == 1:
            return []
        if result.returncode not in (0, 1):
            self._pgrep_reliable = False
            return []
        pids = []
        for token in result.stdout.split():
            try:
                pids.append(int(token))
            except ValueError:
                pass
        return pids

    def is_running(self) -> bool:
        if self.pids():
            return True
        if getattr(self, "_pgrep_reliable", True):
            return False


        return self._killall_zero()

    def _killall_zero(self) -> bool:
        for name in PROCESS_NAMES:
            try:
                result = subprocess.run([self._killall, "-0", name],
                                        capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                return True
        return False


    def quit(self, reason: str = "") -> bool:
        if not self.is_running():
            return True
        suffix = f" ({reason})" if reason else ""
        self.log.info(f"quitting Clop{suffix}")
        self._applescript_quit()
        if self._wait_gone(self.quit_timeout * 0.6, "graceful quit"):
            self.quits += 1
            self.log.debug("Clop exited")
            return True

        self.log.warn("Clop did not quit gracefully, sending SIGTERM")
        self._signal_pids(signal.SIGTERM)
        if self._wait_gone(self.quit_timeout * 0.3, "SIGTERM"):
            self.quits += 1
            return True

        self.log.warn("Clop is still running, sending SIGKILL")
        self._signal_pids(signal.SIGKILL)
        if self._wait_gone(self.quit_timeout * 0.1 + 2, "SIGKILL"):
            self.quits += 1
            return True

        self.log.error("Could not stop Clop; the free-tier counter will not reset")
        return False

    def _applescript_quit(self) -> None:
        if not self._osascript:
            return
        bundle = self.install.bundle_id
        script = (f'tell application id "{bundle}" to quit'
                  if bundle else f'tell application "{APP_NAME}" to quit')
        try:
            result = subprocess.run([self._osascript, "-e", script],
                                    capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.debug(f"osascript failed: {exc}")
            return
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            self.log.debug(f"AppleScript quit failed: {detail}")

    def _signal_pids(self, sig: int) -> None:
        for pid in self.pids():
            try:
                os.kill(pid, sig)
            except OSError:
                pass

        for name in PROCESS_NAMES:
            try:
                subprocess.run([self._killall, "-" + ("TERM" if sig == signal.SIGTERM else "KILL"), name],
                               capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                pass

    def _wait_gone(self, timeout: float, what: str) -> bool:
        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() < deadline:
            if not self.is_running():
                return True
            time.sleep(0.25)
        if not self.is_running():
            return True
        self.log.debug(f"still running after {what}")
        return False


    def launch(self) -> bool:
        if self.is_running():
            return True
        cmd = [self._open, "-g"]
        if self.install.app is not None:
            cmd += ["-a", str(self.install.app)]
        else:
            cmd += ["-b", self.install.bundle_id or BUNDLE_IDS[0]]
        self.log.debug("exec: " + " ".join(shlex.quote(part) for part in cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.error(f"could not start Clop: {exc}")
            return False
        if result.returncode != 0:
            self.log.error(f"could not start Clop: {(result.stderr or result.stdout).strip()}")
            return False

        deadline = time.monotonic() + self.launch_timeout
        while time.monotonic() < deadline:
            if self.is_running():
                self.launches += 1


                if self.settle > 0:
                    time.sleep(self.settle)
                self.log.info("Clop launched (fresh free-tier session)")
                return True
            time.sleep(0.25)
        self.log.error(f"Clop did not appear within {self.launch_timeout:g}s")
        return False

    def ensure_running(self) -> bool:
        return True if self.is_running() else self.launch()


@dataclasses.dataclass
class CliResult:
    cmd: List[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float


@dataclasses.dataclass
class BatchOutcome:
    done: Dict[Path, Tuple[int, int]]
    benign: Dict[Path, str]
    failed: Dict[Path, str]
    limit_hit: bool
    app_not_running: bool
    parsed: bool
    seconds: float


def _normalise_report_path(raw: str) -> Optional[str]:
    if not raw:
        return None
    if raw.startswith("file://"):
        parsed = urllib.parse.urlparse(raw)
        path = urllib.parse.unquote(parsed.path)
    else:
        path = raw
    return path if os.path.isabs(path) else None


def run_cli_command(cmd: Sequence[str], timeout: float, heartbeat: float,
                    log: "Logger", label: str = "",
                    on_proc: Optional[Callable[[Any], None]] = None) -> CliResult:
    log.debug("exec: " + " ".join(shlex.quote(part) for part in cmd))
    started = time.monotonic()
    try:
        proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace")
    except OSError as exc:
        return CliResult(list(cmd), 127, "", f"could not run clop: {exc}", False, 0.0)

    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:
            pass

    out = err = ""
    timed_out = False
    last_beat = started
    try:
        while True:
            now = time.monotonic()
            if timeout and timeout > 0 and now - started >= timeout:
                timed_out = True
                break
            window = max(2.0, heartbeat)
            if timeout and timeout > 0:
                window = min(window, max(0.05, timeout - (now - started)))
            try:
                out, err = proc.communicate(timeout=window)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if timeout and timeout > 0 and now - started >= timeout:
                    timed_out = True
                    break
                if now - last_beat >= max(2.0, heartbeat):
                    last_beat = now
                    suffix = f", {label}" if label else ""
                    log.info(
                        f"    ... clop still working ({human_duration(now - started)} elapsed{suffix})"
                    )
        if timed_out:
            log.warn(
                f"batch exceeded the {timeout:g}s timeout; asking the CLI to cancel "
                "(SIGTERM, which Clop handles as 'stop these requests')"
            )
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
    except KeyboardInterrupt:


        log.warn("interrupted; asking the CLI to cancel the current batch")
        proc.terminate()
        try:
            proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
        raise

    return CliResult(list(cmd), proc.returncode, out or "", err or "",
                     timed_out, time.monotonic() - started)


class ClopCli:
    def __init__(self, cli: Path, log: Logger, extra_args: Sequence[str] = (),
                 heartbeat: float = 20.0,
                 on_proc: Optional[Callable[[Any], None]] = None) -> None:
        self.cli = cli
        self.log = log
        self.extra_args = list(extra_args)
        self.heartbeat = max(2.0, heartbeat)
        self.on_proc = on_proc

    def command(self, files: Sequence[Path]) -> List[str]:
        return [str(self.cli), "optimise", "--json", "--no-progress",
                *self.extra_args, "--", *[str(f) for f in files]]

    def run(self, files: Sequence[Path], timeout: float) -> CliResult:
        cmd = self.command(files)
        return run_cli_command(cmd, timeout, self.heartbeat, self.log,
                               label=f"{len(files)} file(s) in this batch",
                               on_proc=self.on_proc)


    def interpret(self, result: CliResult, requested: Sequence[Path]) -> BatchOutcome:
        requested_map = {str(p): p for p in requested}
        done: Dict[Path, Tuple[int, int]] = {}
        benign: Dict[Path, str] = {}
        failed: Dict[Path, str] = {}

        payload = _extract_json(result.stdout)

        if result.timed_out:
            for path in requested:
                failed[path] = f"timed out after {result.seconds:.0f}s"
            return BatchOutcome(done, benign, failed, False, False, False, result.seconds)

        if not isinstance(payload, dict):
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            message = stderr or stdout or f"clop exited with code {result.returncode}"
            if any(p in message.lower() for p in NOT_RUNNING_PATTERNS):
                for path in requested:
                    failed[path] = "Clop is not running"
                return BatchOutcome(done, benign, failed, False, True, False, result.seconds)
            for path in requested:
                failed[path] = message.splitlines()[-1] if message else "unknown clop error"
            return BatchOutcome(done, benign, failed, False, False, False, result.seconds)

        unmatched = dict(requested_map)

        def match(item: dict) -> Optional[Path]:
            for key in ("forURL", "path"):
                candidate = _normalise_report_path(str(item.get(key) or ""))
                if not candidate:
                    continue
                if candidate in unmatched:
                    return unmatched.pop(candidate)
                real = os.path.realpath(candidate)
                for key_path, path in list(unmatched.items()):
                    if os.path.realpath(key_path) == real:
                        return unmatched.pop(key_path)
                same_name = [p for q, p in unmatched.items() if os.path.basename(q) == os.path.basename(candidate)]
                if len(same_name) == 1:
                    unmatched.pop(str(same_name[0]))
                    return same_name[0]
            return None

        for item in payload.get("done") or []:
            if not isinstance(item, dict):
                continue
            path = match(item)
            if path is None:
                self.log.debug(f"unmatched result for {item.get('forURL') or item.get('path')}")
                continue
            try:
                old = int(item.get("oldBytes") or 0)
                new = int(item.get("newBytes") or 0)
            except (TypeError, ValueError):
                old = new = 0
            done[path] = (old, new)

        for item in payload.get("failed") or []:
            if not isinstance(item, dict):
                continue
            path = match(item)
            if path is None:
                continue
            error = str(item.get("error") or "unknown error").strip()
            lowered = error.lower()
            if any(pattern in lowered for pattern in BENIGN_PATTERNS):
                benign[path] = error
            else:
                failed[path] = error

        for path in unmatched.values():
            failed[path] = "clop did not report a result for this file"

        limit_hit = any(any(p in e.lower() for p in LIMIT_PATTERNS) for e in failed.values())
        not_running = any(any(p in e.lower() for p in NOT_RUNNING_PATTERNS) for e in failed.values())
        return BatchOutcome(done, benign, failed, limit_hit, not_running, True, result.seconds)


def _extract_json(text: str) -> Optional[dict]:
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


class StateStore:

    SCHEMA = 1

    def __init__(self, path: Path, root: Path, log: Logger) -> None:
        self.path = path
        self.root = root
        self.log = log
        self.records: Dict[str, dict] = {}
        self.dirty = False

    def load(self, resume: bool = True) -> None:
        if not resume or not self.path.is_file():
            self.records = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warn(f"ignoring unreadable state file {self.path}: {exc}")
            self.records = {}
            return
        if data.get("schema") != self.SCHEMA:
            self.log.warn("state file from an older version; starting with a clean record")
            self.records = {}
            return
        if data.get("root") and data.get("root") != str(self.root):
            self.log.warn(f"state file belongs to {data.get('root')}; ignoring it")
            self.records = {}
            return
        self.records = {k: v for k, v in (data.get("files") or {}).items() if isinstance(v, dict)}
        self.log.debug(f"loaded {len(self.records)} record(s) from {self.path}")

    def _key(self, path: Path) -> str:
        return str(path)

    def _stat(self, path: Path) -> Optional[os.stat_result]:
        try:
            return os.stat(path)
        except OSError:
            return None

    def is_done(self, path: Path) -> bool:
        record = self.records.get(self._key(path))
        if not record or record.get("status") != "ok":
            return False
        st = self._stat(path)
        if st is None:
            return False

        return record.get("size") == st.st_size and record.get("mtime_ns") == st.st_mtime_ns

    def mark_ok(self, path: Path, old_bytes: int, new_bytes: int) -> None:
        st = self._stat(path)
        self.records[self._key(path)] = {
            "status": "ok",
            "size": st.st_size if st else None,
            "mtime_ns": st.st_mtime_ns if st else None,
            "old_bytes": old_bytes,
            "new_bytes": new_bytes,
            "attempts": self.records.get(self._key(path), {}).get("attempts", 0),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.dirty = True

    def mark_failed(self, path: Path, error: str, attempts: int = 0) -> None:
        st = self._stat(path)
        self.records[self._key(path)] = {
            "status": "failed",
            "size": st.st_size if st else None,
            "mtime_ns": st.st_mtime_ns if st else None,
            "attempts": attempts,
            "error": error,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self.dirty = True

    def bump_attempts(self, path: Path) -> int:
        record = self.records.setdefault(self._key(path), {"status": "pending", "attempts": 0})
        record["attempts"] = int(record.get("attempts") or 0) + 1
        self.dirty = True
        return record["attempts"]

    def save(self, force: bool = False) -> None:
        if not self.dirty and not force:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": self.SCHEMA,
            "root": str(self.root),
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "files": self.records,
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False


def discover_files(root: Path, extensions: Iterable[str], excluded_dirs: Iterable[str],
                   exclude_globs: Sequence[str], include_hidden: bool,
                   order: str, reverse: bool, max_files: Optional[int],
                   log: Logger) -> List[Path]:
    wanted = {ext.lower().lstrip(".") for ext in extensions}
    skip_dirs = set(excluded_dirs)
    found: List[Tuple[Path, os.stat_result]] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in sorted(dirnames):
            if name in skip_dirs:
                continue
            if not include_hidden and name.startswith("."):
                continue
            kept.append(name)
        dirnames[:] = kept

        for name in sorted(filenames):
            if name.startswith("._"):
                continue
            if not include_hidden and name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext not in wanted:
                continue
            full = Path(dirpath) / name
            if full.is_symlink():
                continue
            rel = os.path.relpath(str(full), str(root))
            if any(fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(name, glob) for glob in exclude_globs):
                continue
            try:
                st = os.stat(full)
            except OSError as exc:
                log.warn(f"skipping {full}: {exc}")
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
                continue
            found.append((full, st))

    if order == "mtime":
        found.sort(key=lambda pair: (pair[1].st_mtime_ns, str(pair[0])))
    elif order == "size":
        found.sort(key=lambda pair: (pair[1].st_size, str(pair[0])))
    else:
        found.sort(key=lambda pair: str(pair[0]).lower())
    if reverse:
        found.reverse()

    files = [pair[0] for pair in found]
    if max_files is not None:
        files = files[:max_files]
    return files


@dataclasses.dataclass
class Stats:
    total_files: int = 0
    skipped_done: int = 0
    batches: int = 0
    optimised: int = 0
    unchanged: int = 0
    failed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    started: float = dataclasses.field(default_factory=time.monotonic)
    aborted: bool = False


class BatchRunner:
    def __init__(self, args: argparse.Namespace, log: Logger, app: ClopApp,
                 cli: ClopCli, state: StateStore, stats: Stats) -> None:
        self.args = args
        self.log = log
        self.app = app
        self.cli = cli
        self.state = state
        self.stats = stats
        self.root = Path(args.directory).resolve()
        self.results: Dict[str, dict] = {}
        self.initial_sizes: Dict[str, int] = {}


    def _record_ok(self, path: Path, old_bytes: int, new_bytes: int, note: str = "") -> None:
        st = None
        try:
            st = os.stat(path)
        except OSError:
            pass
        after = st.st_size if st else new_bytes
        before = old_bytes if old_bytes and old_bytes > 0 else self.initial_sizes.get(str(path), after)
        self.state.mark_ok(path, before, after)
        self.results[str(path)] = {
            "status": "ok",
            "before": before,
            "after": after,
            "saved": before - after,
            "note": note,
        }
        self.stats.bytes_before += before
        self.stats.bytes_after += after
        if note:
            self.stats.unchanged += 1
            self.log.info(f"  = {shorten(str(path), self.root)}  ({note})")
        elif after < before:
            self.stats.optimised += 1
            pct = (1 - after / before) * 100 if before else 0
            self.log.info(
                f"  + {shorten(str(path), self.root)}  "
                f"{human_bytes(before)} -> {human_bytes(after)}  (-{pct:.1f}%)"
            )
        else:
            self.stats.unchanged += 1
            self.log.info(f"  = {shorten(str(path), self.root)}  (no change)")

    def _record_failed(self, path: Path, error: str) -> None:
        attempts = int(self.state.records.get(str(path), {}).get("attempts") or 0)
        self.state.mark_failed(path, error, attempts)
        self.results[str(path)] = {
            "status": "failed",
            "error": error,
            "attempts": attempts,
        }
        self.stats.failed += 1
        self.log.error(f"  x {shorten(str(path), self.root)}  ({error})")


    def run_batch(self, batch: Sequence[Path], index: int, total: int) -> None:
        pending = list(batch)
        max_attempts = 1 + max(0, int(self.args.retries))
        attempt = 0

        while pending:
            attempt += 1
            self.log.info(
                f"batch {index}/{total}: {len(pending)} file(s), attempt {attempt}/{max_attempts}"
            )
            if not self.app.ensure_running():
                for path in pending:
                    self._record_failed(path, "could not start Clop")
                return

            for path in pending:
                self.state.bump_attempts(path)

            result = self.cli.run(pending, self.args.timeout)
            outcome = self.cli.interpret(result, pending)

            for path, (old, new) in outcome.done.items():
                self._record_ok(path, old, new)
            for path, note in outcome.benign.items():
                self._record_ok(path, 0, 0, note=note)

            if result.timed_out:
                self.log.warn(
                    "the batch was cancelled because it exceeded --timeout; "
                    "the files that were mid-flight will be retried"
                )

            unresolved = sorted(outcome.failed.items(), key=lambda item: str(item[0]))
            if not unresolved:
                pending = []
                break

            if outcome.limit_hit:
                self.log.warn(
                    "hit the free-tier limit inside this batch (the session was already "
                    "partly used); a restart will give the remaining files a fresh allowance"
                )
            if outcome.app_not_running:
                self.log.warn("Clop was not reachable for this batch")

            if attempt >= max_attempts:
                for path, error in unresolved:
                    self._record_failed(path, error)
                pending = []
                if self.args.fail_fast:
                    self.stats.aborted = True
                break


            if not self.app.quit(reason="fresh session for the retry"):
                for path, _ in unresolved:
                    self._record_failed(path, "could not restart Clop for a fresh session")
                return
            delay = max(0.0, self.args.retry_delay) * (2 ** (attempt - 1))
            if delay:
                self.log.info(
                    f"retrying {len(unresolved)} file(s) in {delay:.0f}s with a fresh Clop session"
                )
                time.sleep(delay)
            pending = [path for path, _ in unresolved]


    def run(self, files: Sequence[Path]) -> None:
        batches = list(chunks(list(files), max(1, int(self.args.batch_size))))
        self.stats.batches = len(batches)

        for index, batch in enumerate(batches, 1):
            self.run_batch(batch, index, len(batches))
            self.state.save(force=True)

            if self.stats.aborted:
                self.log.error("stopping after this batch because of --fail-fast")
                self.app.quit(reason="end of run")
                break

            last_batch = index == len(batches)
            if last_batch and self.args.leave_running:
                self.log.info("leaving Clop running (--leave-running)")
                break


            if not self.app.quit(reason="free-tier session reset"):
                if not last_batch:
                    self.log.error(
                        "Clop could not be stopped, so the free-tier counter cannot be "
                        "reset; stopping instead of burning through failing batches"
                    )
                    self.stats.aborted = True
                break
            self.state.save()


    def write_report(self) -> Optional[Path]:
        path = Path(self.args.report).expanduser() if self.args.report else None
        if path is None and self.args.log_dir:
            path = Path(self.args.log_dir) / "reports" / (time.strftime("%Y-%m-%dT%H-%M-%S") + ".json")
        if path is None:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": VERSION,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "root": str(self.root),
            "batch_size": self.args.batch_size,
            "extensions": sorted(self.args.extensions),
            "stats": {
                "files_total": self.stats.total_files,
                "skipped_already_done": self.stats.skipped_done,
                "batches": self.stats.batches,
                "optimised": self.stats.optimised,
                "unchanged": self.stats.unchanged,
                "failed": self.stats.failed,
                "bytes_before": self.stats.bytes_before,
                "bytes_after": self.stats.bytes_after,
                "app_launches": self.app.launches,
                "app_quits": self.app.quits,
                "seconds": round(time.monotonic() - self.stats.started, 1),
            },
            "files": self.results,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path


def parse_extensions(values: Optional[Sequence[str]]) -> List[str]:
    if not values:
        return list(DEFAULT_EXTENSIONS)
    result: List[str] = []
    for value in values:
        for piece in str(value).replace(";", ",").split(","):
            ext = piece.strip().lower().lstrip(".").lstrip("*")
            if ext and ext not in result:
                result.append(ext)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clop_batch.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Optimise every file in a directory through Clop, 5 at a time, quitting and\n"
            "reopening Clop between batches so the free-tier counter resets."
        ),
        epilog=(
            "examples:\n"
            "  ./clop_batch.py ~/Pictures/export\n"
            "  ./clop_batch.py ~/Movies -b 5 --ext mov,mp4 --order mtime\n"
            "  ./clop_batch.py ~/Downloads/inbox --dry-run\n"
            "  ./clop_batch.py ~/Pictures/export --clop-arg=--aggressive\n"
            "\n"
            "exit codes: 0 all good, 1 some files failed, 2 setup error, 130 interrupted\n"
        ),
    )
    parser.add_argument("directory", help="directory to scan recursively")

    behaviour = parser.add_argument_group("batching")
    behaviour.add_argument("-b", "--batch-size", type=int, default=5,
                           help="files per Clop session (default: 5, the free-tier limit)")
    behaviour.add_argument("--retries", type=int, default=2,
                           help="extra attempts per batch after the first (default: 2); "
                                "each attempt gets a fresh Clop session")
    behaviour.add_argument("--retry-delay", type=float, default=3.0,
                           help="seconds before the first retry, doubled per attempt (default: 3)")
    behaviour.add_argument("--fail-fast", action="store_true",
                           help="stop after the first batch that still has failures")

    selection = parser.add_argument_group("file selection")
    selection.add_argument("-e", "--ext", action="append", metavar="LIST",
                           help="comma-separated extensions to include "
                                "(default: images, video, audio, PDF)")
    selection.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                           help="skip files matching this glob (repeatable), "
                                "e.g. --exclude '*.tmp'")
    selection.add_argument("--exclude-dir", action="append", default=[], metavar="NAME",
                           help="skip directories with this name (repeatable)")
    selection.add_argument("--include-hidden", action="store_true",
                           help="also scan dot-files and dot-directories")
    selection.add_argument("--order", choices=("path", "mtime", "size"), default="path",
                           help="processing order (default: path)")
    selection.add_argument("--reverse", action="store_true", help="reverse the order")
    selection.add_argument("--max-files", type=int, default=None,
                           help="stop after N files (handy for a trial run)")

    timing = parser.add_argument_group("timing")
    timing.add_argument("--timeout", type=float, default=1800.0,
                        help="max seconds to wait for one batch, 0 = wait forever (default: 1800)")
    timing.add_argument("--quit-timeout", type=float, default=60.0,
                        help="max seconds to wait for Clop to quit (default: 60)")
    timing.add_argument("--launch-timeout", type=float, default=60.0,
                        help="max seconds to wait for Clop to appear after launch (default: 60)")
    timing.add_argument("--settle", type=float, default=2.0,
                        help="extra seconds to let a freshly launched Clop initialise (default: 2)")
    timing.add_argument("--heartbeat", type=float, default=20.0,
                        help="print a progress line this often while a batch runs (default: 20)")

    integrations = parser.add_argument_group("Clop integration")
    integrations.add_argument("--clop-bin", metavar="PATH",
                              help="path to the `clop` CLI (default: auto-detect)")
    integrations.add_argument("--clop-app", metavar="PATH",
                              help="path to Clop.app (default: auto-detect)")
    integrations.add_argument("--clop-arg", action="append", default=[], metavar="ARG",
                              help="extra argument passed to `clop optimise` (repeatable), "
                                   "e.g. --clop-arg=--aggressive")
    integrations.add_argument("--no-fresh-start", dest="fresh_start", action="store_false",
                              help="do not quit an already-running Clop before batch 1")
    integrations.add_argument("--leave-running", action="store_true",
                              help="leave Clop running at the end of the run")
    integrations.set_defaults(fresh_start=True)

    state = parser.add_argument_group("state and output")
    state.add_argument("--state", metavar="PATH", help="state file (default: per-target, in ~/Library)")
    state.add_argument("--no-resume", action="store_true",
                       help="ignore previous state and process everything again")
    state.add_argument("--report", metavar="PATH", help="write a JSON report here")
    state.add_argument("--log-dir", metavar="DIR",
                       help="directory for state/logs/reports (default: auto)")
    state.add_argument("--no-log-file", action="store_true", help="do not write a log file")

    misc = parser.add_argument_group("misc")
    misc.add_argument("--dry-run", action="store_true",
                      help="show the plan without starting Clop")
    misc.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    misc.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")
    misc.add_argument("-v", "--verbose", action="store_true", help="more detail")
    misc.add_argument("--allow-non-macos", action="store_true",
                      help="run anyway on a non-macOS host (for testing the logic)")
    misc.add_argument("--version", action="version", version=f"clop_batch {VERSION}")
    return parser


def default_state_dir(root: Path) -> Path:
    if IS_MACOS:
        base = Path.home() / "Library" / "Application Support" / "clop-batch"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "clop-batch"
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return base / digest


def confirm(args: argparse.Namespace, files: Sequence[Path], batches: int,
            log: Logger) -> bool:
    log.plain("")
    log.plain(f"  target : {Path(args.directory).resolve()}")
    log.plain(f"  files  : {len(files)}")
    log.plain(f"  plan   : {batches} batch(es) of up to {args.batch_size} file(s), "
              f"one Clop restart between batches")
    log.plain(f"  formats: {', '.join(sorted(args.extensions))}")
    log.plain(f"  order  : {args.order}{' (reversed)' if args.reverse else ''}")
    log.plain("")
    log.plain("  Files are optimised in place. Clop moves the originals into its own")
    log.plain("  backup folder, so a change can still be undone from the app.")
    log.plain("")
    try:
        answer = input("  Continue? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log.plain("")
        return False
    return answer in ("y", "yes")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.extensions = parse_extensions(args.ext)

    if not IS_MACOS and not args.allow_non_macos:
        print("This script drives a macOS app; run it on macOS.\n"
              "(For testing the logic elsewhere, pass --allow-non-macos.)", file=sys.stderr)
        return 2

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    if args.batch_size < 1:
        print("--batch-size must be at least 1", file=sys.stderr)
        return 2

    if args.log_dir:
        log_dir = Path(args.log_dir).expanduser()
    elif args.state:
        log_dir = Path(args.state).expanduser().parent
    else:
        log_dir = default_state_dir(root)
    state_path = Path(args.state).expanduser() if args.state else log_dir / "state.json"
    args.log_dir = str(log_dir)

    level = "debug" if args.verbose else ("warn" if args.quiet else "info")
    log_path = None if args.no_log_file else log_dir / "logs" / f"{time.strftime('%Y-%m-%dT%H-%M-%S')}.log"
    try:
        log = Logger(level=level, file_path=log_path)
    except OSError as exc:
        print(f"Could not open the log file {log_path}: {exc}", file=sys.stderr)
        return 2

    try:
        log.debug(f"root={root} state={state_path}")
        if args.batch_size > 5:
            log.warn(
                f"--batch-size {args.batch_size} is above the free-tier limit of 5; "
                "the extra files will come back as 'Pro limits reached' and be retried "
                "after a restart (harmless, just slower)"
            )

        excluded = list(DEFAULT_EXCLUDED_DIRS) + list(args.exclude_dir or [])
        files = discover_files(
            root=root,
            extensions=args.extensions,
            excluded_dirs=excluded,
            exclude_globs=args.exclude,
            include_hidden=args.include_hidden,
            order=args.order,
            reverse=args.reverse,
            max_files=args.max_files,
            log=log,
        )
        log.info(f"found {len(files)} matching file(s) in {root}")
        if not files:
            log.info("nothing to do")
            return 0

        state = StateStore(state_path, root, log)
        state.load(resume=not args.no_resume)

        pending = [path for path in files if not state.is_done(path)]
        skipped = len(files) - len(pending)
        if skipped:
            log.info(f"skipping {skipped} file(s) already processed in an earlier run")

        if not pending:
            log.info("everything in this directory is already recorded as done")
            log.info("run with --no-resume to process it again")
            return 0

        batches = max(1, -(-len(pending) // max(1, args.batch_size)))
        if args.dry_run:
            log.plain("")
            log.plain(f"  dry run: {len(pending)} file(s), {batches} batch(es) of {args.batch_size}")
            for index, batch in enumerate(chunks(pending, args.batch_size), 1):
                head = ", ".join(shorten(str(p), root, 40) for p in batch[:3])
                more = "" if len(batch) <= 3 else f", ... (+{len(batch) - 3})"
                log.plain(f"    batch {index}: {head}{more}")
            log.plain("")
            return 0

        if not args.yes and sys.stdin.isatty():
            if not confirm(args, pending, batches, log):
                log.info("cancelled")
                return 130

        install = detect_install(log, args.clop_bin, args.clop_app)
        app = ClopApp(install, log, quit_timeout=args.quit_timeout,
                      launch_timeout=args.launch_timeout, settle=args.settle)
        cli = ClopCli(install.cli, log, extra_args=args.clop_arg, heartbeat=args.heartbeat)

        stats = Stats(total_files=len(files), skipped_done=skipped)
        runner = BatchRunner(args, log, app, cli, state, stats)
        for path in pending:
            try:
                runner.initial_sizes[str(path)] = os.stat(path).st_size
            except OSError:
                pass

        if args.fresh_start and app.is_running():
            log.info("Clop is already running; restarting it so batch 1 has the full allowance")
            app.quit(reason="fresh session for batch 1")

        log.info(
            f"starting: {len(pending)} file(s) in {batches} batch(es), "
            f"{args.batch_size} per Clop session"
        )
        try:
            runner.run(pending)
        except KeyboardInterrupt:
            log.warn("interrupted by the user")
            state.save(force=True)
            report = runner.write_report()
            log.info("progress saved; run the same command again to continue")
            if report:
                log.info(f"report: {report}")
            return 130
        finally:
            state.save(force=True)

        report = runner.write_report()

        elapsed = time.monotonic() - stats.started
        saved = stats.bytes_before - stats.bytes_after
        log.plain("")
        log.plain("  summary")
        log.plain("  -------")
        log.plain(f"  files scanned     : {stats.total_files}")
        log.plain(f"  skipped (done)    : {stats.skipped_done}")
        log.plain(f"  optimised         : {stats.optimised}")
        log.plain(f"  unchanged         : {stats.unchanged}")
        log.plain(f"  failed            : {stats.failed}")
        if stats.bytes_before:
            pct = saved / stats.bytes_before * 100 if stats.bytes_before else 0
            log.plain(f"  size              : {human_bytes(stats.bytes_before)} -> "
                      f"{human_bytes(stats.bytes_after)}  "
                      f"({human_bytes(saved)} saved, {pct:.1f}%)")
        log.plain(f"  batches / sessions: {stats.batches} / "
                  f"{app.launches} launch(es), {app.quits} quit(s)")
        log.plain(f"  elapsed           : {human_duration(elapsed)}")
        if report:
            log.plain(f"  report            : {report}")
        if log_path:
            log.plain(f"  log               : {log_path}")
        log.plain("")

        failures = {p: r for p, r in runner.results.items() if r["status"] == "failed"}
        if failures:
            log.plain("  files that still failed:")
            for path, record in sorted(failures.items()):
                log.plain(f"    {path}: {record.get('error', 'unknown error')}")
            log.plain("")
            return 1
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        log.error(f"unexpected error: {exc}")
        if args.verbose:
            raise
        return 2
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
