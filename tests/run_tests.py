#!/usr/bin/env python
"""
Run Amy's test suites.

    python tests/run_tests.py             offline suites only (fast, no network)
    python tests/run_tests.py --network   also the ones that hit YouTube/FFmpeg
    python tests/run_tests.py --live      also start the real bot against Discord
    python tests/run_tests.py --all       everything

Offline suites need nothing but the venv, so they are safe to run anywhere. The network
and live tiers are opt-in because they are slow and depend on services being reachable.
"""
import io
import os
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TESTS = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(TESTS)

OFFLINE = [
    "test_music.py",
    "test_voice.py",
    "test_ui.py",
    "test_queue_mgmt.py",
    "test_queue_cmds.py",
    "test_voice_cmds.py",
    "test_regression.py",
    "test_security.py",
    "test_commands_audit.py",
    "test_slash.py",
    "test_docs_audit.py",
]
NETWORK = [
    "test_resolve.py",       # yt-dlp against YouTube
    "test_search.py",        # yt-dlp search
    "test_audio_path.py",    # yt-dlp + FFmpeg decoding
]
LIVE = [
    "smoke_start.py",        # connects to Discord with the real token
]


def interpreter() -> str:
    venv = os.path.join(PROJ, ".venv", "Scripts", "python.exe")
    if os.path.isfile(venv):
        return venv
    venv_posix = os.path.join(PROJ, ".venv", "bin", "python")
    if os.path.isfile(venv_posix):
        return venv_posix
    return sys.executable


def run(name: str, py: str) -> bool:
    t0 = time.time()
    proc = subprocess.run([py, os.path.join(TESTS, name)], cwd=PROJ,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    ok = proc.returncode == 0
    dt = time.time() - t0
    # Show the suite's own last line - each one ends with its summary
    tail = [l for l in (proc.stdout or "").strip().splitlines() if l.strip()]
    summary = tail[-1].strip() if tail else "(no output)"
    print("  %s %-26s %6.1fs  %s" % ("PASS" if ok else "FAIL", name, dt, summary[:70]))
    if not ok:
        for line in (proc.stdout or "").splitlines()[-15:]:
            print("        " + line)
        for line in (proc.stderr or "").splitlines()[-15:]:
            print("        " + line)
    return ok


def main() -> int:
    args = set(sys.argv[1:])
    selected = list(OFFLINE)
    if args & {"--network", "--all"}:
        selected += NETWORK
    if args & {"--live", "--all"}:
        selected += LIVE

    py = interpreter()
    print("interpreter:", py)
    print("running %d suite(s)" % len(selected))
    print()

    results = [(n, run(n, py)) for n in selected]
    failed = [n for n, ok in results if not ok]

    print()
    if failed:
        print("%d of %d FAILED: %s" % (len(failed), len(results), ", ".join(failed)))
        return 1
    print("all %d suite(s) passed" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
