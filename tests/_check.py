"""
Shared PASS/FAIL recorder for the plain-script suites.

Each suite used to end with its own `if fails: sys.exit(1)`. That gate had to be the last
thing in the file, and in test_queue_cmds.py it wasn't: ~30 checks sat below it for two
sessions, printing FAIL and still exiting 0. Here the gate runs at interpreter exit, so a
failed check makes the process exit 1 no matter where in the file it was recorded.

    from _check import check, finish
    check("label", condition, got_value)
    ...
    finish("ALL FOO TESTS PASSED")   # prints the summary; the exit code doesn't depend on it
"""
import atexit
import os
import sys

fails = []
_gate_registered = False


def _gate():
    if fails:
        sys.stdout.flush()
        sys.stderr.flush()
        # sys.exit() inside an atexit handler is ignored, so this is the only way to turn
        # a would-be exit 0 into a failure.
        os._exit(1)


def record(label, ok):
    """Record a result without printing - for suites that format their own lines."""
    global _gate_registered
    if not _gate_registered:
        # Registered on first use. On failure os._exit skips any handler registered
        # earlier (atexit runs LIFO), which is acceptable: nothing in these suites needs
        # one, and stdout/stderr are flushed by hand above.
        atexit.register(_gate)
        _gate_registered = True
    if not ok:
        fails.append(label)
    return bool(ok)


def check(label, ok, got=""):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok and got != "":
        print("        got:", str(got)[:160])
    return record(label, ok)


def finish(banner):
    """Print the summary. The exit code is set by the gate, not here."""
    print()
    if fails:
        print("%d CHECK(S) FAILED:" % len(fails))
        for label in fails:
            print("   - " + label)
    else:
        print(banner)
