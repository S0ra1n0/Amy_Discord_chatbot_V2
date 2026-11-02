"""
Piper as a separate process: the client in speech.py (PiperEngine) against the REAL
piper_worker.py, with stand-in `piper` and `onnxruntime` packages so nothing heavy is needed.

The stand-ins live in a temp dir that is put on sys.path (so the worker finds them and so a
stray `import piper` in Amy's own process would succeed - and be caught). They behave through
environment variables: which request numbers crash, hang or fail, and where to log.
The real engine is exercised by test_piper_live.py.
"""
import ast
import io
import json
import logging
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import piper_worker
import speech
from _check import check, finish

SENTENCE = "Hello there, number one."
FAKE_RATE = 16000

FAKE_PIPER = '''
import os, sys, time


class PiperConfig:
    def __init__(self, d):
        self.sample_rate = d["sample_rate"]

    @classmethod
    def from_dict(cls, d):
        return cls(d)


def _log(line):
    path = os.environ.get("FAKE_PIPER_LOG")
    if path:
        with open(path, "a") as f:
            f.write(line + chr(10))


class SynthesisConfig:
    def __init__(self, length_scale=1.0, noise_scale=0.0, noise_w_scale=0.0):
        _log("cfg length=%s noise=%s noise_w=%s" % (length_scale, noise_scale, noise_w_scale))
        _log("env " + " ".join("%s=%s" % (k, "present" if k in os.environ else "absent")
                               for k in ("DISCORD_TOKEN", "MY_API_KEY", "PATH", "KEEP_ME")))


class _Chunk:
    def __init__(self, data):
        self.audio_int16_bytes = data


def _numbers(name):
    return {int(x) for x in os.environ.get(name, "").split(",") if x.strip()}


class PiperVoice:
    def __init__(self, config, session):
        self.config = config

    def synthesize(self, text, cfg):
        state = os.environ["FAKE_PIPER_STATE"]
        n = int(open(state).read() or "0") + 1
        open(state, "w").write(str(n))
        if n in _numbers("FAKE_PIPER_CRASH"):
            sys.stderr.write("fake worker crashing" + chr(10))
            sys.stderr.flush()
            os._exit(3)
        if n in _numbers("FAKE_PIPER_HANG"):
            time.sleep(3600)
        if n in _numbers("FAKE_PIPER_SLOW"):
            time.sleep(1.0)
        if n in _numbers("FAKE_PIPER_NATIVE"):
            os.write(1, b"NATIVE NOISE FROM C CODE" + bytes([10]))
        if n in _numbers("FAKE_PIPER_ERROR"):
            raise ValueError("boom " + text)
        if os.environ.get("FAKE_PIPER_NOISE"):
            for _ in range(400):
                sys.stderr.write("x" * 1000 + chr(10))
            sys.stderr.flush()
        print("a stray print from a library", flush=True)
        yield _Chunk(bytes([1, 0]) * (len(text) * 10))
        yield _Chunk(bytes([2, 0]) * 5)
'''

FAKE_ORT = '''
import os


class SessionOptions:
    intra_op_num_threads = 0
    inter_op_num_threads = 0


class InferenceSession:
    def __init__(self, path, sess_options=None, providers=None):
        self._options = sess_options
        path_log = os.environ.get("FAKE_PIPER_LOG")
        if path_log:
            with open(path_log, "a") as f:
                f.write("session threads=%s inter=%s providers=%s" % (
                    sess_options.intra_op_num_threads, sess_options.inter_op_num_threads, providers) + chr(10))

    def get_session_options(self):
        return self._options
'''


def expected_pcm(text):
    return bytes([1, 0]) * (len(text) * 10) + bytes([2, 0]) * 5


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))

    def warnings(self):
        return [m for lvl, m in self.records if lvl >= logging.WARNING]

    def everything(self):
        return " ".join(m for _, m in self.records)


def pid_gone(pid, seconds=15.0):
    """True once process `pid` has exited (Windows: wait on the handle; else poll)."""
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)        # SYNCHRONIZE
        if not handle:
            return True                                          # no such process any more
        try:
            return k32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
        finally:
            k32.CloseHandle(handle)
    end = time.time() + seconds
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


with tempfile.TemporaryDirectory() as tmp:
    fake_pkgs = os.path.join(tmp, "pkgs")
    os.makedirs(os.path.join(fake_pkgs, "piper"))
    os.makedirs(os.path.join(fake_pkgs, "onnxruntime"))
    with open(os.path.join(fake_pkgs, "piper", "__init__.py"), "w", encoding="utf-8") as f:
        f.write(FAKE_PIPER)
    with open(os.path.join(fake_pkgs, "onnxruntime", "__init__.py"), "w", encoding="utf-8") as f:
        f.write(FAKE_ORT)
    model = os.path.join(tmp, "voice.onnx")
    open(model, "wb").close()
    with open(model + ".json", "w", encoding="utf-8") as f:
        json.dump({"sample_rate": FAKE_RATE}, f)
    state = os.path.join(tmp, "count.txt")
    logfile = os.path.join(tmp, "fake.log")
    sys.path.insert(0, fake_pkgs)         # for the worker's children AND (on purpose) for this process
    os.environ["PYTHONPATH"] = fake_pkgs + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.environ["FAKE_PIPER_STATE"] = state
    os.environ["FAKE_PIPER_LOG"] = logfile

    speech_log = logging.getLogger("amy.speech")
    records = _Records()
    speech_log.addHandler(records)
    speech_log.setLevel(logging.DEBUG)

    def scenario(crash="", hang="", error="", noise="", slow="", native="", bigheader="", shortbody=""):
        with open(state, "w") as fh:
            fh.write("0")
        if os.path.exists(logfile):
            os.remove(logfile)
        records.records.clear()
        os.environ["FAKE_PIPER_CRASH"] = crash
        os.environ["FAKE_PIPER_HANG"] = hang
        os.environ["FAKE_PIPER_ERROR"] = error
        os.environ["FAKE_PIPER_NOISE"] = noise
        os.environ["FAKE_PIPER_SLOW"] = slow
        os.environ["FAKE_PIPER_NATIVE"] = native
        os.environ["PW_BIGHEADER"] = bigheader        # for the stand-in worker script
        os.environ["PW_SHORTBODY"] = shortbody

    def make(**kw):
        kw.setdefault("request_timeout", 1.5)
        kw.setdefault("startup_timeout", 20.0)
        return speech.PiperEngine(model, pitch=0.0, soft=False, threads=3, **kw)

    print("=== the worker protocol, without a process ===")
    HDR = piper_worker.HEADER
    _real_stderr = sys.stderr
    sys.stderr = io.StringIO()            # the worker prints tracebacks for the errors below
    check("a request gets a length-prefixed PCM frame",
          piper_worker.handle(lambda t: b"abcd", b'{"text": "hi"}\n') == HDR.pack(4) + b"abcd")
    check("empty text gets length 0 and never reaches the voice",
          piper_worker.handle(lambda t: 1 / 0, b'{"text": "  "}\n') == HDR.pack(0))
    frame = piper_worker.handle(lambda t: 1 / 0, b'{"text": "x"}\n')
    (n,) = HDR.unpack(frame[:4])
    check("a synthesis error is an error frame: negative length + the exception's type name only",
          n < 0 and frame[4:] == b"ZeroDivisionError" and -n == len(frame[4:]), frame)
    check("bad JSON and a non-string text are error frames too",
          all(HDR.unpack(piper_worker.handle(lambda t: b"", bad)[:4])[0] < 0
              for bad in (b"not json\n", b'{"text": 5}\n', b'{"nope": 1}\n')))
    class _Input(io.BytesIO):
        """Fails the test, rather than spinning forever, if serve keeps reading past the end."""
        past_end = 0

        def readline(self, *a):
            line = super().readline(*a)
            if not line:
                self.past_end += 1
                if self.past_end > 20:
                    raise RuntimeError("serve kept reading after end of input")
            return line

    fin, fout = _Input(b'{"text": "a"}\n\n{"text": "bb"}\n'), io.BytesIO()
    try:
        piper_worker.serve(lambda t: t.encode(), fin, fout)
        serve_error = ""
    except RuntimeError as e:
        serve_error = str(e)
    check("serve answers each request, skips blank lines and returns at end of input",
          fout.getvalue() == HDR.pack(1) + b"a" + HDR.pack(2) + b"bb" and not serve_error,
          (fout.getvalue(), serve_error))

    class _GoneOut:
        def write(self, b):
            raise BrokenPipeError()

        def flush(self):
            pass

    piper_worker.serve(lambda t: b"", io.BytesIO(b'{"text": "a"}' + bytes([10])), _GoneOut())
    sys.stderr = _real_stderr
    check("serve stops quietly when Amy's end of the pipe is gone", True)

    print()
    print("=== the client against the real worker ===")
    scenario()
    caught = []
    real_register = speech.atexit.register
    speech.atexit.register = lambda fn, *a, **k: caught.append(fn)
    eng = make()
    try:
        eng.load()
    finally:
        speech.atexit.register = real_register
    try:
        check("load starts the worker and takes the sample rate from its ready line",
              eng.rate == FAKE_RATE and eng._worker is not None and eng._worker.proc.poll() is None, eng.rate)
        check("load registers close() with atexit as a safety net", eng.close in caught, caught)
        log_lines = open(logfile, encoding="utf-8").read().splitlines()
        check("the worker was told the thread count, length and noise settings",
              "session threads=3 inter=1 providers=['CPUExecutionProvider']" in log_lines
              and "cfg length=1.07 noise=0.25 noise_w=0.25" in log_lines, log_lines)
        check("load warmed the voice up with one request", "0" != open(state).read(), open(state).read())
        out = eng.synthesize(SENTENCE)
        check("synthesize round-trips: Piper's chunks come back as one PCM block",
              out == expected_pcm(SENTENCE), (len(out), len(expected_pcm(SENTENCE))))
        check("a stray print inside the worker doesn't corrupt the stream (second sentence still fine)",
              eng.synthesize("Again.") == expected_pcm("Again."))
        check("empty text reaches the worker as length 0 and gives no audio", eng._raw("") == b"")
        check("text with nothing speakable never calls the worker",
              eng.synthesize("   ") == b"")
        non_ascii = "Café – 你好"
        check("non-ASCII text survives the JSON line",
              eng.synthesize(non_ascii) == expected_pcm(speech.piper_text(non_ascii)))
        pid = eng._worker.proc.pid
        check("no warnings so far", records.warnings() == [], records.warnings())

        results = []

        def say(text):
            for _ in range(5):
                results.append((text, eng.synthesize(text)))

        threads = [threading.Thread(target=say, args=(t,)) for t in ("One.", "Two two two two.")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        check("two threads at once never mix up replies on the pipe",
              len(results) == 10 and all(r == expected_pcm(t) for t, r in results)
              and eng._worker.proc.pid == pid, len(results))

        print()
        print("=== no Amy process imports piper ===")
        check("after load() and synthesize(), neither piper nor onnxruntime is in Amy's process",
              "piper" not in sys.modules and "onnxruntime" not in sys.modules,
              [m for m in ("piper", "onnxruntime") if m in sys.modules])
    finally:
        eng.close()

    print()
    print("=== close() and exit on end of input ===")
    check("close() ends the worker process", eng._worker is None and pid_gone(pid, 5.0))
    try:
        eng.synthesize("hi")
        after = ""
    except speech.SpeechUnavailable as e:
        after = str(e)
    check("after close() synthesize says it isn't loaded (no silent restart)", "isn't loaded" in after, after)
    eng.close()
    check("close() twice is harmless", True)

    scenario()
    eng = make()
    eng.load()
    sp = speech.Speaker(eng)
    held = eng._worker
    sp.close()
    check("Speaker.close() closes its engine (and so the worker)",
          eng._worker is None and held is not None and held.proc.poll() is not None)

    scenario()
    eng = make()
    eng.load()
    w = eng._worker
    w.stdin.close()
    try:
        code = w.proc.wait(10)
    except subprocess.TimeoutExpired:
        code = "still running"
        w.kill()
    check("the worker exits by itself, cleanly (code 0), when its stdin reaches end of input",
          code == 0, code)
    w.dispose()
    eng.close()

    # Amy killed outright: the pipe closes with her, and the worker must follow.
    parent_src = os.path.join(tmp, "parent.py")
    with open(parent_src, "w", encoding="utf-8") as f:
        f.write("import subprocess, sys, time" + chr(10)
                + "p = subprocess.Popen([sys.executable, sys.argv[1], '--model', sys.argv[2], '--threads', '1',"
                  " '--length', '1.0', '--noise', '0.2', '--noise-w', '0.2'],"
                  " stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)" + chr(10)
                + "p.stdout.readline()" + chr(10)
                + "print(p.pid, flush=True)" + chr(10)
                + "time.sleep(600)" + chr(10))
    scenario()
    parent = subprocess.Popen([sys.executable, parent_src, speech.PIPER_WORKER, model],
                              stdout=subprocess.PIPE, creationflags=speech._NO_WINDOW)
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    parent.kill()
    parent.wait(10)
    gone = pid_gone(child_pid, 15.0)
    if not gone:
        subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], capture_output=True)
    check("when the process that started the worker is killed, the worker exits too", gone, child_pid)
    parent.stdout.close()

    print()
    print("=== crashes, hangs and errors ===")
    def bounded(fn, seconds, unblock=None):
        """Run fn on a thread; if it hasn't finished in `seconds`, call unblock() and flag it hung."""
        box = {}

        def run():
            try:
                box["result"] = fn()
            except Exception as e:
                box["error"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(seconds)
        if t.is_alive():
            box["hung"] = True
            if unblock:
                unblock()
            t.join(20)
        return box

    def call(engine, text, seconds=40.0):
        """engine.synthesize(text) that can never hang the suite: (result, error, hung)."""
        def unblock():
            w = engine._worker
            if w is not None:
                w.kill()
        box = bounded(lambda: engine.synthesize(text), seconds, unblock)
        return box.get("result"), box.get("error"), box.get("hung", False)

    scenario(crash="2")                       # request 1 is the warm-up; the first sentence crashes
    eng = make()
    eng.load()
    first_pid = eng._worker.proc.pid
    try:
        out, err, hung = call(eng, SENTENCE)
        check("a crash mid-request: the worker is restarted once and the sentence still comes back",
              out == expected_pcm(SENTENCE) and err is None and not hung, (err, hung))
        check("...with a different worker process", eng._worker is not None and eng._worker.proc.pid != first_pid)
        warns = records.warnings()
        check("...logged as ONE warning that says it is restarting, with the exit code but not the stderr",
              len(warns) == 1 and "restarting" in warns[0] and "exit code 3" in warns[0]
              and "fake worker crashing" not in warns[0], warns)
        check("...the worker's stderr tail is kept at DEBUG",
              any(lvl == logging.DEBUG and "fake worker crashing" in m for lvl, m in records.records),
              records.records)
        check("...and the sentence text is never logged", "number one" not in records.everything())
        out, err, hung = call(eng, "Fine.")
        check("...the next sentence just works", out == expected_pcm("Fine."), err)
    finally:
        eng.close()

    scenario(crash="2,3")                     # the retry crashes too
    eng = make()
    eng.load()
    try:
        out, err, hung = call(eng, SENTENCE)
        failure = str(err) if isinstance(err, RuntimeError) else ""
        check("a second crash on the retry raises (Speaker then skips the sentence)",
              "failed twice" in failure and out is None, (out, err))
        check("...after exactly one restart (two requests reached workers)",
              open(state).read() == "3", open(state).read())
        out, err, hung = call(eng, "Recovered.")
        check("...and the sentence after that gets a fresh worker and works",
              out == expected_pcm("Recovered."), err)
    finally:
        eng.close()

    scenario(hang="2")
    eng = make(request_timeout=1.5)
    eng.load()
    first_pid = eng._worker.proc.pid
    try:
        t0 = time.perf_counter()
        out, err, hung = call(eng, SENTENCE, seconds=30.0)
        took = time.perf_counter() - t0
        check("a hung worker is killed by the watchdog and replaced; the sentence comes back",
              out == expected_pcm(SENTENCE) and not hung and err is None
              and eng._worker is not None and eng._worker.proc.pid != first_pid, (err, hung))
        check("...after about the request timeout, not forever", 1.4 <= took < 12.0, took)
        check("...with a warning that names the timeout", any("no answer within" in w for w in records.warnings()),
              records.warnings())
        check("...and the hung process is really gone", pid_gone(first_pid, 5.0))
    finally:
        eng.close()

    scenario(error="2")
    eng = make()
    eng.load()
    first_pid = eng._worker.proc.pid
    try:
        out, err, hung = call(eng, SENTENCE)
        failure = str(err) if isinstance(err, RuntimeError) else ""
        check("an error frame raises with the exception's type name", "ValueError" in failure, (out, err))
        check("...but the worker is kept (no restart, no warning) and the retry isn't wasted",
              eng._worker is not None and eng._worker.proc.pid == first_pid and records.warnings() == []
              and open(state).read() == "2", (records.warnings(), open(state).read()))
        out, err, hung = call(eng, "Still here.")
        check("...and the next sentence works on the same worker",
              out == expected_pcm("Still here.") and eng._worker is not None
              and eng._worker.proc.pid == first_pid, err)
        check("the error text of the worker never carries the sentence", "number one" not in failure)
    finally:
        eng.close()

    scenario(noise="1")
    eng = make(request_timeout=20.0)
    eng.load()
    try:
        t0 = time.perf_counter()
        out, err, hung = call(eng, SENTENCE, seconds=25.0)
        check("a worker that writes 400 KB to stderr per sentence doesn't hang the pipe",
              out == expected_pcm(SENTENCE) and time.perf_counter() - t0 < 10.0 and records.warnings() == [],
              (err, hung, records.warnings()))
    finally:
        eng.close()

    print()
    print("=== the lock, bad frames, stdout, environment, log hygiene ===")
    PROTO_WORKER = '''
import json, os, queue, struct, sys, threading, time
out = sys.stdout.buffer
lines = queue.Queue()


def numbers(name):
    return {int(x) for x in os.environ.get(name, "").split(",") if x.strip()}


def read_requests():
    for raw in sys.stdin.buffer:
        lines.put(raw)
    lines.put(None)


threading.Thread(target=read_requests, daemon=True).start()
out.write(b'{"ready": true, "rate": 16000, "threads": 1}' + bytes([10]))
out.flush()
while True:
    line = lines.get()
    if line is None:
        break
    state = os.environ["FAKE_PIPER_STATE"]
    n = int(open(state).read() or "0") + 1
    open(state, "w").write(str(n))
    text = json.loads(line)["text"]
    pcm = bytes([1, 0]) * (len(text) * 10)
    if n in numbers("PW_SLOW"):
        time.sleep(1.0)
        if not lines.empty():             # a second request arrived before this one was answered
            open(os.environ["PW_OVERLAP"], "a").write("overlap" + chr(10))
    if n in numbers("PW_BIGHEADER"):
        out.write(struct.pack("<i", 64 * 1024 * 1024 + 1))
        out.flush()
        time.sleep(60)
    if n in numbers("PW_SHORTBODY"):
        out.write(struct.pack("<i", len(pcm)) + pcm[:10])
        out.flush()
        time.sleep(60)
    out.write(struct.pack("<i", len(pcm)) + pcm)
    out.flush()
'''
    proto = os.path.join(tmp, "proto_worker.py")
    with open(proto, "w", encoding="utf-8") as f:
        f.write(PROTO_WORKER)

    def proto_pcm(text):
        return bytes([1, 0]) * (len(text) * 10)

    overlap_file = os.path.join(tmp, "overlap.txt")
    os.environ["PW_OVERLAP"] = overlap_file
    scenario()
    os.environ["PW_SLOW"] = "2"               # the first sentence takes a second to answer
    if os.path.exists(overlap_file):
        os.remove(overlap_file)
    eng = make(worker_script=proto, request_timeout=5.0)
    eng.load()
    try:
        slow_results = {}

        def speak(key, text):
            slow_results[key] = eng.synthesize(text)

        ta = threading.Thread(target=speak, args=("a", "The slow sentence is long."), daemon=True)
        tb = threading.Thread(target=speak, args=("b", "Quick."), daemon=True)
        ta.start()
        time.sleep(0.3)                       # the slow request is in the worker; now a second caller arrives
        tb.start()
        ta.join(30)
        tb.join(30)
        check("a second caller during a slow sentence gets its OWN reply",
              slow_results.get("a") == proto_pcm("The slow sentence is long.")
              and slow_results.get("b") == proto_pcm("Quick."), {k: len(v) for k, v in slow_results.items()})
        check("...because it waited its turn: the worker never saw two requests at once (the lock)",
              not os.path.exists(overlap_file), open(overlap_file).read() if os.path.exists(overlap_file) else "")
    finally:
        eng.close()
        os.environ.pop("PW_SLOW", None)

    scenario(bigheader="2")
    eng = make(worker_script=proto)
    eng.load()
    first_pid = eng._worker.proc.pid
    try:
        t0 = time.perf_counter()
        out, err, hung = call(eng, SENTENCE, seconds=30.0)
        took = time.perf_counter() - t0
        check("a frame header over PIPER_MAX_FRAME is rejected at once, the worker replaced, the sentence retried",
              out == proto_pcm(SENTENCE) and err is None and not hung
              and eng._worker is not None and eng._worker.proc.pid != first_pid, (err, hung))
        check("...without waiting for a body that never comes (well under the request timeout)",
              took < 1.2, took)
        check("...with one warning that names the frame",
              len(records.warnings()) == 1 and "frame of" in records.warnings()[0], records.warnings())
    finally:
        eng.close()

    scenario(shortbody="2")
    eng = make(worker_script=proto)
    eng.load()
    first_pid = eng._worker.proc.pid
    try:
        t0 = time.perf_counter()
        out, err, hung = call(eng, SENTENCE, seconds=30.0)
        took = time.perf_counter() - t0
        check("a short body followed by a hang: the watchdog kills the worker, it is replaced, the sentence comes back",
              out == proto_pcm(SENTENCE) and err is None and not hung
              and eng._worker is not None and eng._worker.proc.pid != first_pid, (err, hung))
        check("...after the request timeout, with a warning that names it",
              took >= 1.4 and any("no answer within" in w for w in records.warnings()), (took, records.warnings()))
    finally:
        eng.close()

    # close() racing a restart: a worker that finishes starting after close() ran must not survive
    scenario(crash="2")
    eng = make()
    eng.load()
    started = []
    real_start = eng._start_worker

    def start_then_close():
        w = real_start()
        started.append(w)
        eng.close()                           # close() runs while the replacement is starting
        return w

    eng._start_worker = start_then_close      # type: ignore[method-assign]
    out, err, hung = call(eng, SENTENCE, seconds=30.0)
    check("close() during a restart: the new worker is disposed, not adopted, and the call is refused",
          isinstance(err, speech.SpeechUnavailable) and eng._worker is None, (out, err))
    check("...and that new worker process is really dead",
          len(started) == 1 and started[0].proc.wait(10) is not None and pid_gone(started[0].proc.pid, 5.0))

    scenario()
    eng = make()
    started = []
    real_start = eng._start_worker

    def start_then_close_load():
        w = real_start()
        started.append(w)
        eng.close()
        return w

    eng._start_worker = start_then_close_load      # type: ignore[method-assign]
    try:
        eng.load()
        refused = ""
    except speech.SpeechUnavailable as e:
        refused = str(e)
    check("close() during load(): the worker is disposed and load() refuses",
          "isn't loaded" in refused and eng._worker is None and not eng._loaded and len(started) == 1
          and started[0].proc.wait(10) is not None, refused)

    # a failed warm-up must not leave a worker running
    scenario(error="1")
    eng = make()
    started = []
    real_start = eng._start_worker

    def start_recording():
        w = real_start()
        started.append(w)
        return w

    eng._start_worker = start_recording        # type: ignore[method-assign]
    try:
        eng.load()
        failure = ""
    except RuntimeError as e:
        failure = str(e)
    check("a failed warm-up raises out of load()", "ValueError" in failure, failure)
    check("...and the worker it started is stopped, not left running",
          eng._worker is None and not eng._loaded and len(started) == 1 and started[0].proc.poll() is not None
          and pid_gone(started[0].proc.pid, 5.0))

    # native code writing to file descriptor 1 must not corrupt the protocol
    scenario(native="2,3")
    eng = make()
    eng.load()
    try:
        out, err, hung = call(eng, SENTENCE)
        out2, err2, hung2 = call(eng, "Again.")
        check("a native write to fd 1 inside the worker goes to stderr, not into the replies",
              out == expected_pcm(SENTENCE) and out2 == expected_pcm("Again.") and records.warnings() == [],
              (err, err2, records.warnings()))
    finally:
        eng.close()

    # secrets are not handed to the worker
    os.environ["DISCORD_TOKEN"] = "not-a-real-token"
    os.environ["MY_API_KEY"] = "not-a-real-key"
    os.environ["KEEP_ME"] = "harmless"
    try:
        scenario()
        eng = make()
        eng.load()
        env_line = [l for l in open(logfile, encoding="utf-8").read().splitlines() if l.startswith("env ")]
        eng.close()
        check("the worker's environment has no DISCORD_TOKEN or other secret-looking name, but keeps the rest",
              env_line == ["env DISCORD_TOKEN=absent MY_API_KEY=absent PATH=present KEEP_ME=present"], env_line)
        cleaned = speech.piper_worker_env({"DISCORD_TOKEN": "x", "Some_Secret": "x", "DB_PASSWORD": "x",
                                           "OPENAI_API_KEY": "x", "PATH": "p", "TTS_THREADS": "6"})
        check("piper_worker_env drops secret-looking names (any case) and nothing else",
              cleaned == {"PATH": "p", "TTS_THREADS": "6"}, cleaned)
    finally:
        for k in ("DISCORD_TOKEN", "MY_API_KEY", "KEEP_ME"):
            os.environ.pop(k, None)

    # a crash must not put an earlier sentence's text into a WARNING
    scenario(error="2", crash="3")
    eng = make()
    eng.load()
    try:
        call(eng, "The zebra quartz marker sentence.")         # an error frame: its traceback is in the worker's stderr
        out, err, hung = call(eng, "Second sentence.")          # the worker crashes, is replaced, retries
        warns_text = " ".join(records.warnings())
        check("a crash after an error-frame sentence recovers",
              out == expected_pcm("Second sentence.") and len(records.warnings()) == 1, (err, records.warnings()))
        check("...and no WARNING contains any part of an earlier sentence (stderr stays at DEBUG)",
              "zebra" not in warns_text and "quartz" not in warns_text and "Traceback" not in warns_text, warns_text)
        check("...while the DEBUG log does keep the stderr tail for diagnosis",
              "fake worker crashing" in records.everything())
    finally:
        eng.close()

    # the worker dies with Amy, even stuck mid-sentence (Windows: a kill-on-close Job Object)
    if sys.platform == "win32":
        DEAF_WORKER = '''
import os, struct, sys, time
out = sys.stdout.buffer
open(os.environ["PW_PIDFILE"], "w").write(str(os.getpid()))
out.write(b'{"ready": true, "rate": 16000, "threads": 1}' + bytes([10]))
out.flush()
n = 0
for line in sys.stdin.buffer:
    n += 1
    if n > 1:
        time.sleep(3600)          # stuck mid-sentence: never looks at stdin again
    out.write(struct.pack("<i", 2) + bytes([1, 0]))
    out.flush()
time.sleep(3600)                  # and it ignores end-of-input too
'''
        deaf = os.path.join(tmp, "deaf_worker.py")
        with open(deaf, "w", encoding="utf-8") as f:
            f.write(DEAF_WORKER)
        amy_src = os.path.join(tmp, "fake_amy.py")
        with open(amy_src, "w", encoding="utf-8") as f:
            f.write("import sys, threading, time" + chr(10)
                    + "sys.path.insert(0, sys.argv[1])" + chr(10)
                    + "import speech" + chr(10)
                    + "eng = speech.PiperEngine(sys.argv[3], pitch=0.0, soft=False, worker_script=sys.argv[2],"
                      " request_timeout=600)" + chr(10)
                    + "eng.load()" + chr(10)
                    + "threading.Thread(target=lambda: eng.synthesize('Hello there.'), daemon=True).start()" + chr(10)
                    + "print('asking', flush=True)" + chr(10)
                    + "time.sleep(600)" + chr(10))
        pidfile = os.path.join(tmp, "deaf.pid")
        os.environ["PW_PIDFILE"] = pidfile
        scenario()
        amy = subprocess.Popen([sys.executable, amy_src, PROJ, deaf, model], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=speech._NO_WINDOW)
        assert amy.stdout is not None
        amy.stdout.readline()                 # 'asking': the request is on its way to a worker that won't answer
        time.sleep(1.0)
        stuck_pid = int(open(pidfile).read())
        alive_before = not pid_gone(stuck_pid, 0.05)
        amy.kill()
        amy.wait(10)
        gone = pid_gone(stuck_pid, 15.0)
        if not gone:
            subprocess.run(["taskkill", "/F", "/PID", str(stuck_pid)], capture_output=True)
        amy.stdout.close()
        os.environ.pop("PW_PIDFILE", None)
        check("(setup) the stuck worker was running while Amy was alive", alive_before, stuck_pid)
        check("a worker stuck mid-sentence, ignoring end-of-input, still dies when Amy is hard-killed (Job Object)",
              gone, stuck_pid)

        class _NoJob:
            def __init__(self):
                raise OSError("jobs are not allowed here")

        real_job = speech._KillOnCloseJob
        speech._KillOnCloseJob = _NoJob       # type: ignore[misc,assignment]
        try:
            scenario(crash="2")
            eng = make()
            eng.load()
            out, err, hung = call(eng, SENTENCE)      # crash -> restart: a second worker start
            job_warns = [w for w in records.warnings() if "tie the worker" in w]
            check("if the Job Object can't be made: ONE warning, and speech works (stdin-EOF rule only)",
                  len(job_warns) == 1 and out == expected_pcm(SENTENCE), (job_warns, err))
            eng.close()
        finally:
            speech._KillOnCloseJob = real_job     # type: ignore[misc]
    else:
        print("  (not Windows: the Job Object check is skipped; the worker watches its parent instead)")

    print()
    print("=== the worker's parent watch (non-Windows guarantee) ===")
    parents = [100, 100, 7]
    fired = []
    thread = piper_worker.watch_parent(100, lambda: parents.pop(0) if len(parents) > 1 else parents[0],
                                       lambda: fired.append(True), interval=0.02)
    thread.join(10)
    check("watch_parent calls its exit hook once the parent id changes", fired == [True] and not thread.is_alive(),
          fired)

    print()
    print("=== start-up failures ===")
    early = os.path.join(tmp, "early_exit.py")
    with open(early, "w", encoding="utf-8") as f:
        f.write("import sys" + chr(10) + "sys.stderr.write('no such voice file' + chr(10))" + chr(10)
                + "sys.exit(2)" + chr(10))
    scenario()
    eng = make(worker_script=early)
    try:
        eng.load()
        msg = ""
    except speech.SpeechUnavailable as e:
        msg = str(e)
    check("a worker that exits before 'ready' -> SpeechUnavailable with its stderr tail and exit code",
          "no such voice file" in msg and "code 2" in msg, msg)
    check("...and nothing is left running or registered", eng._worker is None and not eng._loaded)
    sleepy = os.path.join(tmp, "sleepy.py")
    with open(sleepy, "w", encoding="utf-8") as f:
        f.write("import sys, time" + chr(10) + "sys.stderr.write('loading forever' + chr(10))" + chr(10)
                + "sys.stderr.flush()" + chr(10) + "time.sleep(25)" + chr(10))
    eng = make(worker_script=sleepy, startup_timeout=1.5)
    t0 = time.perf_counter()
    try:
        eng.load()
        msg = ""
    except speech.SpeechUnavailable as e:
        msg = str(e)
    took = time.perf_counter() - t0
    check("a worker that never says 'ready' is killed after the start-up timeout, with its stderr tail",
          "within 2s" in msg and "loading forever" in msg and 1.4 <= took < 12.0, (msg, took))
    babble = os.path.join(tmp, "babble.py")
    with open(babble, "w", encoding="utf-8") as f:
        f.write("print('hello there', flush=True)" + chr(10) + "import time" + chr(10) + "time.sleep(600)" + chr(10))
    eng = make(worker_script=babble, startup_timeout=5.0)
    try:
        eng.load()
        msg = ""
    except speech.SpeechUnavailable as e:
        msg = str(e)
    check("a first line that isn't the ready message is refused", "unexpected first line" in msg, msg)
    eng = make(worker_script=os.path.join(tmp, "missing.py"))
    try:
        eng.load()
        msg = ""
    except speech.SpeechUnavailable as e:
        msg = str(e)
    check("a missing worker file is a clear SpeechUnavailable", "worker program isn't there" in msg, msg)
    scenario()
    broken_model = os.path.join(tmp, "nothing.onnx")
    eng = speech.PiperEngine(broken_model, worker_script=speech.PIPER_WORKER)
    try:
        eng.load()
        msg = ""
    except speech.SpeechUnavailable as e:
        msg = str(e)
    check("a missing voice file is still refused by Amy before any worker starts",
          "isn't there" in msg or "aren't downloaded" in msg, msg)

    speech_log.removeHandler(records)

print()
print("=== the source rules ===")
worker_src = open(os.path.join(PROJ, "piper_worker.py"), encoding="utf-8").read()
tree = ast.parse(worker_src)
imported = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        imported |= {a.name.split(".")[0] for a in node.names}
    elif isinstance(node, ast.ImportFrom):
        imported.add((node.module or "").split(".")[0])
amy_modules = {f[:-3] for f in os.listdir(PROJ) if f.endswith(".py") and f != "piper_worker.py"}
check("piper_worker.py imports no Amy module (it is a separate program)",
      not (imported & amy_modules), sorted(imported & amy_modules))
check("...only the standard library at the top (piper/onnxruntime come in through import_module)",
      imported <= {"argparse", "importlib", "json", "os", "struct", "sys", "threading", "time", "traceback",
                   "typing"}, sorted(imported))

speech_tree = ast.parse(open(os.path.join(PROJ, "speech.py"), encoding="utf-8").read())
offending = []
for node in ast.walk(speech_tree):
    if isinstance(node, ast.Import):
        offending += [a.name for a in node.names if a.name.split(".")[0] in ("piper", "onnxruntime")]
    elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in ("piper", "onnxruntime"):
        offending.append(node.module)
    elif isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "import_module" and node.args \
            and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).split(".")[0] in (
                "piper", "onnxruntime"):
        offending.append(str(node.args[0].value))
check("speech.py never imports piper or onnxruntime, by any spelling", not offending, offending)

finish("ALL PIPER WORKER TESTS PASSED")
