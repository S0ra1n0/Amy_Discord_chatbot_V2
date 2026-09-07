"""Start the real bot, confirm it reaches on_ready, then shut it down."""
import io, os, subprocess, sys, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
# Resolve the project root from this file, so the suite runs from any checkout
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(PROJ, ".venv", "Scripts", "python.exe")
if not os.path.isfile(PY):
    PY = sys.executable   # fall back to whatever interpreter is running us

proc = subprocess.Popen([PY, "-u", "Amy_chatbot_V2.py"], cwd=PROJ,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace", bufsize=1)
TIMEOUT, online, lines = 45, False, []
start = time.time()
try:
    while time.time() - start < TIMEOUT:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None: break
            continue
        line = line.rstrip(); lines.append(line)
        if "is online!" in line: online = True
        if online and "Auto-prune task started" in line:
            time.sleep(2); break
finally:
    proc.terminate()
    try: proc.wait(timeout=10)
    except subprocess.TimeoutExpired: proc.kill()

print("=== BOT STARTUP OUTPUT ===")
for l in lines: print("  " + l)
print()
print("=== RESULT ===")
print("  SUCCESS - bot connected and reached on_ready" if online
      else "  FAILED - never came online (exit %s)" % proc.returncode)
sys.exit(0 if online else 1)
