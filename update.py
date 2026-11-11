"""Bring saved.db up to date with one command.

    python update.py                    # every step
    python update.py sync visuals       # only the named steps

Each step is its own script and skips whatever is already done, so this is safe to
stop and rerun. A failed step does not stop the later ones, except the first:
without a sync there is nothing new to work on.
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STEPS = {
    "sync": ("Fetching new saves", ["sync.py", "new"]),
    "songs": ("Fetching your saved songs list", ["sync.py", "songs"]),
    "transcribe": ("Transcribing speech", ["transcribe.py"]),
    "visuals": ("Describing what is shown", ["visuals.py"]),
    "listen": ("Identifying songs by listening", ["listen.py"]),
}


def free_gpu():
    """Whisper and Ollama's model do not both fit in 6 GB, so ask Ollama to unload first."""
    try:
        tags = json.loads(urllib.request.urlopen("http://localhost:11434/api/ps", timeout=5).read())
        for m in tags.get("models", []):
            body = json.dumps({"model": m["name"], "keep_alive": 0}).encode()
            urllib.request.urlopen(urllib.request.Request(
                "http://localhost:11434/api/generate", body, {"Content-Type": "application/json"}), timeout=30)
    except OSError:
        pass  # Ollama not running: nothing to unload


def main(names):
    unknown = [n for n in names if n not in STEPS]
    if unknown:
        print(f"unknown step {unknown}; choose from {list(STEPS)}")
        return 2
    failed = []
    for name in names or STEPS:
        label, args = STEPS[name]
        print(f"\n=== {label} ===", flush=True)
        if name == "transcribe":
            free_gpu()
        started = time.time()
        code = subprocess.run([sys.executable, *args], cwd=ROOT).returncode
        print(f"--- {name} {'ok' if code == 0 else 'FAILED'} in {time.time() - started:.0f}s", flush=True)
        if code:
            failed.append(name)
            if name == "sync":
                break
    print("\nDone." if not failed else f"\nFinished with problems in: {', '.join(failed)}. Rerun to retry.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
