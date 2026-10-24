from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_clop_batch import Testbed

REPO = Path(__file__).resolve().parent.parent
GUI = Path(os.environ.get("CLOP_GUI_TARGET") or (REPO / "clop_gui.py")).resolve()

MODE: list = []

FAILURES: list = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"    ok   {message}")
    else:
        print(f"    FAIL {message}")
        FAILURES.append(message)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Gui:
    def __init__(self, bed: Testbed) -> None:
        self.bed = bed
        self.home = bed.dir / "home"
        self.home.mkdir(exist_ok=True)
        self.port = free_port()
        env = bed.env()
        env["HOME"] = str(self.home)
        env["NO_COLOR"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, str(GUI), *MODE, "--port", str(self.port), "--no-open",
             "--allow-non-macos", "--host", "127.0.0.1"],
            cwd=str(REPO), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.base = f"http://127.0.0.1:{self.port}"
        self._wait_ready()

    def _wait_ready(self, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"gui exited: {self.proc.stdout.read() if self.proc.stdout else ''}")
            try:
                self.get("/api/ping")
                return
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("gui did not start")

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=10) as response:
            return json.loads(response.read().decode())

    def post(self, path: str, payload: dict):
        data = json.dumps(payload).encode()
        request = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def wait_done(self, timeout: float = 90) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.get("/api/status")
            job = status.get("job")
            if job and job["state"] != "running":
                return job
            time.sleep(0.25)
        raise RuntimeError("job did not finish")

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def test_copies_to_output_folder() -> None:
    print("\n[1] optimises copies into the output folder, leaving the source alone")
    bed = Testbed()
    gui = None
    try:
        bed.make_files(7)
        gui = Gui(bed)
        status, body = gui.post("/api/start", {
            "source": str(bed.target), "dest": str(bed.dir / "optimised"),
            "batch_size": 5, "extensions": "png", "order": "path", "compression": "default",
        })
        check(status == 200, f"/api/start accepted the job (got {status}: {body})")
        job = gui.wait_done()
        out = Path(job["dest"])

        check(job["state"] == "done", f"job finished as done (got {job['state']})")
        check(job["processed"] == 7, f"7 files optimised (got {job['processed']})")
        check(job["failed"] == 0, f"no failures (got {job['failed']}: {job['errors'][:2]})")
        copies = sorted(out.glob("*.png"))
        check(len(copies) == 7, f"7 copies in the output folder (got {len(copies)})")
        check(all(p.stat().st_size < 1000 for p in copies), "every copy is smaller than the original")
        check(all(p.stat().st_size == 1000 for p in bed.target.glob("*.png")),
              "the source files are untouched")
        check(job["saved"] >= 7 * 500, f"saved bytes are reported (saved={job['saved']})")

        events = bed.events()
        starts = [e for e in events if e["event"] == "batch_start"]
        check(len(starts) == 2, f"2 Clop sessions were used (got {len(starts)})")
        check(all(len(e["files"]) <= 5 for e in starts), "no session exceeded 5 files")
        check(sum(1 for e in events if e["event"] == "quit") == 2,
              f"Clop was restarted after each batch "
              f"(got {sum(1 for e in events if e['event'] == 'quit')} quits)")
        out_prefix = str(bed.dir / "optimised")
        check(all(p.startswith(out_prefix) for e in starts for p in e.get("paths", [])),
              "Clop was pointed at the copies, not the originals")
    finally:
        if gui:
            gui.close()
        bed.cleanup()


def test_second_run_skips_processed() -> None:
    print("\n[2] a second run skips files already optimised into that folder")
    bed = Testbed()
    gui = None
    try:
        bed.make_files(6)
        gui = Gui(bed)
        target = {"source": str(bed.target), "dest": str(bed.dir / "out"), "extensions": "png"}
        gui.post("/api/start", target)
        first = gui.wait_done()
        before = len(bed.events())

        gui.post("/api/start", target)
        second = gui.wait_done()

        check(first["processed"] == 6, "first run processed everything")
        check(second["state"] == "done" and second["processed"] == 0,
              f"second run processed nothing (got {second['processed']})")
        check(len(bed.events()) == before, "second run never started Clop")
    finally:
        if gui:
            gui.close()
        bed.cleanup()


def test_delete_originals_after_success() -> None:
    print("\n[3] delete-originals removes only files whose copy verified")
    bed = Testbed()
    gui = None
    try:
        bed.make_files(3)
        (bed.target / "bad_file.png").write_bytes(b"y" * 1000)
        os.environ["FAKE_CLOP_FAIL"] = "bad_"
        gui = Gui(bed)
        gui.post("/api/start", {
            "source": str(bed.target), "dest": str(bed.dir / "out"),
            "extensions": "png", "batch_size": 5, "retries": 0,
            "delete_originals": True,
        })
        job = gui.wait_done()
        os.environ.pop("FAKE_CLOP_FAIL", None)

        check(job["processed"] == 3, f"3 files optimised (got {job['processed']})")
        check(job["failed"] == 1, f"1 file failed (got {job['failed']})")
        remaining = sorted(p.name for p in bed.target.glob("*.png"))
        check(remaining == ["bad_file.png"], f"only the failed original is left (got {remaining})")
        check((bed.target / "bad_file.png").stat().st_size == 1000,
              "the failed original is untouched")
        check(len(list((bed.dir / "out").glob("*.png"))) == 4,
              "every file still has a copy in the output folder")
    finally:
        os.environ.pop("FAKE_CLOP_FAIL", None)
        if gui:
            gui.close()
        bed.cleanup()


def test_validation_and_cancel() -> None:
    print("\n[4] bad input is rejected and cancel stops the job")
    bed = Testbed()
    gui = None
    try:
        bed.make_files(4)
        gui = Gui(bed)

        status, body = gui.post("/api/start", {"source": str(bed.dir / "nope")})
        check(status == 400 and "Not a folder" in body.get("error", ""),
              f"a missing source folder is rejected ({status} {body})")

        status, body = gui.post("/api/start", {"source": str(bed.target), "dest": str(bed.target)})
        check(status == 400 and "different" in body.get("error", ""),
              f"an output folder equal to the source is rejected ({status} {body})")

        os.environ["FAKE_CLOP_DELAY"] = "3"
        gui.post("/api/start", {"source": str(bed.target), "dest": str(bed.dir / "slow"),
                                "extensions": "png", "batch_size": 2})
        time.sleep(1.0)
        status, _ = gui.post("/api/cancel", {})
        check(status == 200, f"cancel accepted ({status})")
        job = gui.wait_done(timeout=60)
        os.environ.pop("FAKE_CLOP_DELAY", None)
        check(job["state"] == "cancelled", f"job ends as cancelled (got {job['state']})")
        check(job["processed"] < 4, f"not every file was processed ({job['processed']})")

        status, body = gui.post("/api/cancel", {})
        check(status == 409, f"cancelling twice is a no-op ({status})")
    finally:
        os.environ.pop("FAKE_CLOP_DELAY", None)
        if gui:
            gui.close()
        bed.cleanup()


def test_page_served() -> None:
    print("\n[5] the page and its API are served")
    bed = Testbed()
    gui = None
    try:
        gui = Gui(bed)
        with urllib.request.urlopen(gui.base + "/", timeout=10) as response:
            html = response.read().decode()
        check(response.status == 200, "GET / returns 200")
        check("Clop Batch" in html and "api/start" in html, "the page contains the app shell")
        status = gui.get("/api/status")
        check(status["version"] and "settings" in status, "status exposes version and settings")
        check(status["clop"] is not None, f"the CLI is detected ({status['clop']})")
        check(status["history_count"] == 0, "history starts empty")
    finally:
        if gui:
            gui.close()
        bed.cleanup()


def main() -> int:
    if not GUI.is_file():
        print(f"missing {GUI}", file=sys.stderr)
        return 2
    for test in (
        test_page_served,
        test_copies_to_output_folder,
        test_second_run_skips_processed,
        test_delete_originals_after_success,
        test_validation_and_cancel,
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
