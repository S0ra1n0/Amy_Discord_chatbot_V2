"""
Piper, run as its own small program beside Amy.

`piper-tts` is GPL-3.0 and Amy is MIT, so Amy's process never imports it: `speech.PiperEngine`
starts this file with `sys.executable`, sends it sentences over a pipe and reads audio back.
This file is Amy's own code (MIT) and imports no other Amy module - only the standard library
and, inside `load_synth`, piper and onnxruntime. The rules are in ARCHITECTURE.md ("Speech").

Started as:
    python piper_worker.py --model PATH --threads N --length L --noise N --noise-w W

The protocol, on the worker's standard streams:
  - On start-up, ONE line on stdout: {"ready": true, "rate": <Hz>, "threads": <n>}
  - A request is ONE line on stdin: {"text": "..."}
  - Every reply is a 4-byte little-endian signed length, then that many bytes. A length of 0 or
    more is that many bytes of 16-bit mono PCM (0 for empty text). A NEGATIVE length is an error
    frame: the next -length bytes are a short UTF-8 message (the exception's type name only;
    the traceback goes to stderr).
  - Logs and tracebacks go to stderr only, never to stdout: the protocol uses a private copy of
    the real stdout, and file descriptor 1 itself is pointed at stderr.
  - End of input on stdin (Amy closed the pipe, or Amy died) is the signal to exit, quietly.
"""
import argparse
import importlib
import json
import os
import struct
import sys
import threading
import time
import traceback
from typing import Any, Callable, Optional, Sequence

HEADER = struct.Struct("<i")


def handle(synth: Callable[[str], bytes], line: bytes) -> bytes:
    """One request line in, one reply frame out. Never raises."""
    try:
        text = json.loads(line)["text"]
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        pcm = synth(text) if text.strip() else b""
        return HEADER.pack(len(pcm)) + pcm
    except Exception as e:      # boundary: any failure becomes an error frame, the worker lives on
        traceback.print_exc(file=sys.stderr)
        message = type(e).__name__.encode("utf-8")
        return HEADER.pack(-len(message)) + message


def serve(synth: Callable[[str], bytes], inp: Any, out: Any) -> None:
    """Answer requests until `inp` reaches end-of-file (or `out` is gone)."""
    while True:
        line = inp.readline()
        if not line:
            return
        if not line.strip():
            continue
        try:
            out.write(handle(synth, line))
            out.flush()
        except OSError:         # Amy is gone (broken pipe): nothing left to do
            return


def watch_parent(original: int, get_parent: Callable[[], int], on_gone: Callable[[], Any],
                 interval: float = 2.0) -> threading.Thread:
    """
    Not on Windows: call `on_gone` once the parent process id is no longer `original` (the
    parent died and the worker was re-parented), so a worker stuck mid-sentence still dies with
    Amy. (Windows does this with a Job Object on Amy's side; os.kill(pid, 0) must never be used
    there - it terminates the process.)
    """
    def run() -> None:
        while True:
            time.sleep(interval)
            if get_parent() != original:
                on_gone()
                return

    thread = threading.Thread(target=run, name="parent-watch", daemon=True)
    thread.start()
    return thread


def load_synth(model: str, threads: int, length: float, noise: float, noise_w: float):
    """Load the voice. Returns (synth, sample_rate, threads actually set on the session)."""
    piper = importlib.import_module("piper")
    ort = importlib.import_module("onnxruntime")
    with open(model + ".json", encoding="utf-8") as f:
        config = piper.PiperConfig.from_dict(json.load(f))
    # Built here rather than with PiperVoice.load(): that one makes its own onnxruntime session,
    # which takes every core, and TTS_THREADS has to bound speech's CPU use.
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(model, sess_options=options, providers=["CPUExecutionProvider"])
    voice = piper.PiperVoice(config=config, session=session)
    cfg = piper.SynthesisConfig(length_scale=length, noise_scale=noise, noise_w_scale=noise_w)

    def synth(text: str) -> bytes:
        return b"".join(chunk.audio_int16_bytes for chunk in voice.synthesize(text, cfg))

    return synth, int(config.sample_rate), int(session.get_session_options().intra_op_num_threads)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Piper worker for Amy (talks to Amy over stdin/stdout)")
    ap.add_argument("--model", required=True)
    ap.add_argument("--threads", type=int, required=True)
    ap.add_argument("--length", type=float, required=True)
    ap.add_argument("--noise", type=float, required=True)
    ap.add_argument("--noise-w", type=float, required=True)
    args = ap.parse_args(argv)
    # Keep a private copy of the real stdout for the protocol, then point file descriptor 1 at
    # stderr: a stray Python print AND a native library writing to fd 1 land in the log and can
    # never corrupt a reply.
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    out = os.fdopen(protocol_fd, "wb")
    inp = sys.stdin.buffer
    if os.name != "nt":
        # Amy's Windows guarantee is a Job Object on her side; elsewhere the worker watches.
        watch_parent(os.getppid(), os.getppid, lambda: os._exit(0))
    try:
        synth, rate, threads = load_synth(args.model, args.threads, args.length, args.noise, args.noise_w)
    except Exception:           # boundary: the traceback is for Amy's log (she reads our stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    out.write((json.dumps({"ready": True, "rate": rate, "threads": threads}) + "\n").encode("ascii"))
    out.flush()
    serve(synth, inp, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
