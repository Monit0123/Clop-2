from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SCRIPT = Path(os.environ.get("CLOP_BATCH_TARGET") or (REPO / "clop_batch.py")).resolve()

MODE = ["cli"] if os.environ.get("CLOP_BATCH_TARGET") else []

FAKE_CLOP = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time

state = pathlib.Path(os.environ["FAKE_CLOP_STATE"])
running = state / "running"
used_file = state / "used"
events_file = state / "events.log"
limit = int(os.environ.get("FAKE_CLOP_LIMIT", "5"))
delay = float(os.environ.get("FAKE_CLOP_DELAY", "0.01"))
fail_prefixes = [p for p in os.environ.get("FAKE_CLOP_FAIL", "").split(",") if p]


def log(event, **extra):
    record = {"t": time.time(), "event": event}
    record.update(extra)
    with open(events_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def read_used():
    try:
        return int(used_file.read_text().strip() or "0")
    except Exception:
        return 0


def write_used(value):
    used_file.write_text(str(value))


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] != "optimise":
        print("fake clop: only 'optimise' is implemented", file=sys.stderr)
        return 64

    files = []
    after_separator = False
    for arg in argv[1:]:
        if arg == "--":
            after_separator = True
            continue
        if after_separator:
            files.append(arg)
        elif not arg.startswith("-"):
            files.append(arg)

    if not running.exists():
        print("Error: Clop is not running. Start Clop and try again.", file=sys.stderr)
        return 1

    log("batch_start", files=[os.path.basename(f) for f in files],
        paths=[str(f) for f in files], used=read_used())
    used = read_used()
    done, failed = [], []

    for name in files:
        path = pathlib.Path(name)
        if not path.exists():
            failed.append({"error": "file not found", "forURL": path.as_uri()})
            continue
        if used >= limit:
            failed.append({"error": "Pro limits reached", "forURL": path.as_uri()})
            continue

        used += 1
        write_used(used)
        log("file_start", file=path.name)
        time.sleep(delay)

        if any(path.name.startswith(prefix) for prefix in fail_prefixes):
            log("file_fail", file=path.name)
            failed.append({"error": "unsupported format", "forURL": path.as_uri()})
            used -= 1
            write_used(used)
            continue

        old = path.stat().st_size
        data = path.read_bytes()
        path.write_bytes(data[: max(1, len(data) // 2)])
        new = path.stat().st_size
        done.append({"path": str(path), "forURL": path.as_uri(), "oldBytes": old, "newBytes": new})
        log("file_end", file=path.name)

    log("batch_end", done=len(done), failed=len(failed), used=read_used())
    print(json.dumps({"done": done, "failed": failed}))
    return 0 if done else 1


sys.exit(main())
"""

FAKE_PGREP = r"""#!/usr/bin/env python3
import os, pathlib, sys
state = pathlib.Path(os.environ["FAKE_CLOP_STATE"])
if not (state / "running").exists():
    sys.exit(1)
print("4242")
sys.exit(0)
"""

FAKE_OPEN = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
state = pathlib.Path(os.environ["FAKE_CLOP_STATE"])
(state / "running").write_text("1")
(state / "used").write_text("0")
with open(state / "events.log", "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"t": time.time(), "event": "open", "args": sys.argv[1:]}) + "\n")
sys.exit(0)
"""

FAKE_OSASCRIPT = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
state = pathlib.Path(os.environ["FAKE_CLOP_STATE"])
script = " ".join(sys.argv[1:]).lower()
if "quit" in script:
    (state / "running").unlink(missing_ok=True)
    with open(state / "events.log", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t": time.time(), "event": "quit", "via": "osascript"}) + "\n")
sys.exit(0)
"""

FAKE_KILLALL = r"""#!/usr/bin/env python3
import json, os, pathlib, sys, time
state = pathlib.Path(os.environ["FAKE_CLOP_STATE"])
running = state / "running"
if "-0" in sys.argv[1:]:
    sys.exit(0 if running.exists() else 1)
running.unlink(missing_ok=True)
with open(state / "events.log", "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"t": time.time(), "event": "killall", "args": sys.argv[1:]}) + "\n")
sys.exit(0)
"""

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.lowtechguys.Clop</string>
  <key>CFBundleName</key><string>Clop</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
"""


class Testbed:
    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="clop-batch-test-"))
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.state = self.dir / "state"
        self.state.mkdir()
        self.logs = self.dir / "logs"
        self.logs.mkdir()
        self.app = self.dir / "Clop.app"
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents" / "SharedSupport").mkdir()
        (self.app / "Contents" / "Info.plist").write_text(INFO_PLIST, encoding="utf-8")

        self._write_tool("clop", FAKE_CLOP)
        self._write_tool("pgrep", FAKE_PGREP)
        self._write_tool("open", FAKE_OPEN)
        self._write_tool("osascript", FAKE_OSASCRIPT)
        self._write_tool("killall", FAKE_KILLALL)
        shutil.copy(self.bin / "clop", self.app / "Contents" / "SharedSupport" / "ClopCLI")
        (self.app / "Contents" / "SharedSupport" / "ClopCLI").chmod(0o755)

        self.target = self.dir / "target"
        self.target.mkdir()

    def _write_tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)

    def make_files(self, count: int, prefix: str = "img_", size: int = 1000) -> None:
        for index in range(count):
            (self.target / f"{prefix}{index:03d}.png").write_bytes(b"x" * size)

    def env(self) -> dict:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["FAKE_CLOP_STATE"] = str(self.state)
        env["NO_COLOR"] = "1"
        env.pop("CLOP_BIN", None)
        env.pop("CLOP_APP", None)
        return env

    def run(self, *extra: str, timeout: float = 120) -> subprocess.CompletedProcess:
        cmd = [
            sys.executable, str(SCRIPT), *MODE, str(self.target),
            "--allow-non-macos", "-y",
            "--clop-app", str(self.app),
            "--log-dir", str(self.logs),
            "--retry-delay", "0",
            "--settle", "0",
            "--heartbeat", "2",
            *extra,
        ]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=self.env())

    def events(self) -> list:
        path = self.state / "events.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def set_used(self, value: int) -> None:
        (self.state / "used").write_text(str(value))

    def mark_running(self) -> None:
        (self.state / "running").write_text("1")

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


FAILURES: list = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"    ok   {message}")
    else:
        print(f"    FAIL {message}")
        FAILURES.append(message)


def batches(events: list) -> list:
    result, current = [], None
    for event in events:
        if event["event"] == "batch_start":
            current = event["files"]
        elif event["event"] == "batch_end":
            result.append(current)
            current = None
    return result


def assert_no_quit_mid_batch(events: list) -> None:
    inside = False
    bad = []
    for event in events:
        if event["event"] == "batch_start":
            inside = True
        elif event["event"] == "batch_end":
            inside = False
        elif event["event"] in ("quit", "killall") and inside:
            bad.append(event)
    check(not bad, "Clop is never quit while a batch is in flight")


def test_happy_path() -> None:
    print("\n[1] 12 files, batch size 5 -> 3 sessions, quota never exceeded")
    bed = Testbed()
    try:
        bed.make_files(12)
        result = bed.run()
        out = result.stdout
        events = bed.events()
        sizes = batches(events)

        check(result.returncode == 0, f"exit code 0 (got {result.returncode})")
        check(len(sizes) == 3, f"3 batches were sent (got {len(sizes)}: {[len(b) for b in sizes]})")
        check(all(len(b) <= 5 for b in sizes), "no batch exceeded the free-tier size of 5")
        check(sum(len(b) for b in sizes) == 12, "all 12 files were sent exactly once")
        check(len({f for b in sizes for f in b}) == 12, "no file was sent twice")
        check(sum(1 for e in events if e["event"] == "open") == 3, "Clop was (re)launched once per batch")
        check(sum(1 for e in events if e["event"] == "quit") == 3, "Clop was quit after every batch")
        check(all(p.stat().st_size < 1000 for p in bed.target.glob("*.png")), "every file got smaller")
        assert_no_quit_mid_batch(events)

        state = json.loads((bed.logs / "state.json").read_text())
        check(len([r for r in state["files"].values() if r["status"] == "ok"]) == 12,
              "state file records all 12 files as done")
        check("summary" in out and "failed            : 0" in out, "summary reports no failures")
    finally:
        bed.cleanup()


def test_stale_session_limit_recovery() -> None:
    print("\n[2] a partly used session: 3 of 5 slots gone, no fresh start")
    bed = Testbed()
    try:
        bed.make_files(5)
        bed.mark_running()
        bed.set_used(3)
        result = bed.run("--no-fresh-start")
        events = bed.events()
        sizes = batches(events)

        check(result.returncode == 0, f"exit code 0 after recovering (got {result.returncode})")
        check(len({f for b in sizes for f in b}) == 5, "all 5 files were eventually processed")
        check(any("Pro limits reached" in line for line in [result.stdout]) or len(sizes) > 1,
              "the limit error triggered a retry after a restart")
        check(sum(1 for e in events if e["event"] == "open") == 1,
              "exactly one relaunch provided the fresh allowance")
        check(all(p.stat().st_size < 1000 for p in bed.target.glob("*.png")),
              "the files that hit the limit on attempt 1 were retried successfully")
        assert_no_quit_mid_batch(events)
    finally:
        bed.cleanup()


def test_permanent_failure_is_reported_and_others_finish() -> None:
    print("\n[3] a file that always fails: retried, reported, others still done")
    bed = Testbed()
    try:
        bed.make_files(4, prefix="good_")
        bed.make_files(2, prefix="bad_")
        os.environ["FAKE_CLOP_FAIL"] = "bad_"
        result = bed.run("--retries", "1", "--batch-size", "3")
        events = bed.events()
        try:
            del os.environ["FAKE_CLOP_FAIL"]
        except KeyError:
            pass

        check(result.returncode == 1, f"exit code 1 when files remain failed (got {result.returncode})")
        check("files that still failed" in result.stdout, "failures are listed at the end")
        check(all(p.stat().st_size < 1000 for p in bed.target.glob("good_*.png")),
              "the good files were still optimised")
        check(all(p.stat().st_size == 1000 for p in bed.target.glob("bad_*.png")),
              "the bad files were left untouched")
        started = [e for e in events if e["event"] == "file_start" and e["file"].startswith("bad_")]
        check(len(started) >= 2, f"the failing file was retried (attempts: {len(started)})")
        assert_no_quit_mid_batch(events)
    finally:
        os.environ.pop("FAKE_CLOP_FAIL", None)
        bed.cleanup()


def test_resume_skips_finished_work() -> None:
    print("\n[4] running the same target again skips what is already done")
    bed = Testbed()
    try:
        bed.make_files(7)
        first = bed.run()
        before = len(bed.events())
        second = bed.run()

        check(first.returncode == 0, "first run succeeded")
        check(second.returncode == 0, "second run succeeded")
        check("already recorded as done" in second.stdout, "second run reports nothing to do")
        check(len(bed.events()) == before, "second run did not start Clop at all")
    finally:
        bed.cleanup()


def test_timeout_does_not_hang() -> None:
    print("\n[5] --timeout stops a stuck batch instead of hanging forever")
    bed = Testbed()
    try:
        bed.make_files(1)
        os.environ["FAKE_CLOP_DELAY"] = "30"
        started = time.monotonic()
        result = bed.run("--timeout", "2", "--retries", "0")
        elapsed = time.monotonic() - started
        os.environ.pop("FAKE_CLOP_DELAY", None)

        check(result.returncode == 1, f"exit code 1 for a timed-out batch (got {result.returncode})")
        check(elapsed < 20, f"gave up quickly instead of hanging ({elapsed:.1f}s)")
        check("timed out" in result.stdout, "the timeout is reported per file")
    finally:
        os.environ.pop("FAKE_CLOP_DELAY", None)
        bed.cleanup()


def test_dry_run_does_not_touch_clop() -> None:
    print("\n[6] --dry-run only prints the plan")
    bed = Testbed()
    try:
        bed.make_files(8)
        result = bed.run("--dry-run")
        check(result.returncode == 0, "dry run exits 0")
        check("batch 1" in result.stdout and "batch 2" in result.stdout, "the plan shows both batches")
        check(bed.events() == [], "Clop was never launched")
    finally:
        bed.cleanup()


def test_bad_batch_size_is_handled() -> None:
    print("\n[7] a batch size above 5 warns but still completes")
    bed = Testbed()
    try:
        bed.make_files(8)
        result = bed.run("--batch-size", "8")
        check(result.returncode == 0, f"exit code 0 (got {result.returncode})")
        check("above the free-tier limit of 5" in result.stdout, "the user is warned")
        time.sleep(0.01)
        check(all(p.stat().st_size < 1000 for p in bed.target.glob("*.png")),
              "all files were still optimised by retrying the overflow")
        check(len(batches(bed.events())) > 1, "the overflow was retried in a fresh session")
    finally:
        bed.cleanup()


def test_fresh_start_restarts_a_running_app() -> None:
    print("\n[8] a Clop that is already running is restarted so batch 1 gets 5 free slots")
    bed = Testbed()
    try:
        bed.make_files(5)
        bed.mark_running()
        bed.set_used(4)
        result = bed.run()
        events = bed.events()
        check(result.returncode == 0, f"exit code 0 (got {result.returncode})")
        check(bool(events) and events[0]["event"] == "quit",
              "the already-running app was quit before the first batch")
        check(all(p.stat().st_size < 1000 for p in bed.target.glob("*.png")),
              "the batch succeeded because the stale session was reset")
    finally:
        bed.cleanup()


def test_selection_options() -> None:
    print("\n[9] --ext, --max-files and --order control what is sent")
    bed = Testbed()
    try:
        (bed.target / "keep.png").write_bytes(b"x" * 500)
        (bed.target / "keep.jpg").write_bytes(b"x" * 500)
        (bed.target / "skip.txt").write_bytes(b"x" * 500)
        (bed.target / "skip.pdf").write_bytes(b"x" * 500)
        os.utime(bed.target / "keep.png", (1_600_000_000, 1_600_000_000))
        os.utime(bed.target / "keep.jpg", (1_700_000_000, 1_700_000_000))

        result = bed.run("--ext", "png,jpg", "--max-files", "1", "--order", "mtime")
        files = [f for batch in batches(bed.events()) for f in batch]
        check(result.returncode == 0, f"exit code 0 (got {result.returncode})")
        check(files == ["keep.png"], f"only the oldest PNG was sent (got {files})")
        check((bed.target / "skip.txt").stat().st_size == 500, "txt files were not touched")
        check((bed.target / "skip.pdf").stat().st_size == 500, "pdf files were not touched")
        check((bed.target / "keep.jpg").stat().st_size == 500, "--max-files stopped after one file")
    finally:
        bed.cleanup()


def main() -> int:
    if not SCRIPT.is_file():
        print(f"missing {SCRIPT}", file=sys.stderr)
        return 2
    for test in (
        test_happy_path,
        test_stale_session_limit_recovery,
        test_permanent_failure_is_reported_and_others_finish,
        test_resume_skips_finished_work,
        test_timeout_does_not_hang,
        test_dry_run_does_not_touch_clop,
        test_bad_batch_size_is_handled,
        test_fresh_start_restarts_a_running_app,
        test_selection_options,
    ):
        test()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
