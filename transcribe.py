"""Add an English transcript of the speech in each saved reel to saved.db.

    python transcribe.py        # every reel not done yet (safe to stop and rerun)
    python transcribe.py 5      # only the next 5

Streams each video's sound from the link stored by sync.py and transcribes it on
the GPU; nothing is kept on disk. Non-English speech is translated to English so the
English search model in app.py can find it.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

# the pip-installed CUDA DLLs are not on PATH by default on Windows
import nvidia

for _bin in Path(list(nvidia.__path__)[0]).glob("*/bin"):
    os.add_dll_directory(str(_bin))
    os.environ["PATH"] = f"{_bin};{os.environ['PATH']}"

from faster_whisper import WhisperModel  # noqa: E402

from sync import db  # noqa: E402

MODEL = "large-v3"  # fits the 6 GB RTX 4050 at int8_float16; use "small" on CPU
MAX_SECONDS = 600  # only the first 10 minutes of a long video
# lines Whisper makes up when a reel is only music
JUNK = {"thanks for watching", "thank you for watching", "thank you", "you",
        "subtitles by the amara.org community"}


def audio(url):
    """ffmpeg streams the video and returns its sound as 16 kHz mono floats.
    (faster-whisper's own decoder is broken with the installed PyAV.)"""
    run = subprocess.run(
        ["ffmpeg", "-v", "error", "-user_agent", "Mozilla/5.0", "-i", url,
         "-t", str(MAX_SECONDS), "-vn", "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, timeout=300,
    )
    if b"does not contain any stream" in run.stderr:
        return np.zeros(0, np.float32)  # silent video, no audio track
    if run.returncode:
        raise RuntimeError(run.stderr.decode(errors="replace").strip()[-300:])
    return np.frombuffer(run.stdout, np.int16).astype(np.float32) / 32768


def transcribe(model, sound):
    if not len(sound):
        return ""  # video without an audio track
    segments, _ = model.transcribe(
        sound,
        task="translate",
        vad_filter=True,  # skips music and silence, where Whisper invents text
        condition_on_previous_text=False,  # stops one bad line repeating to the end
    )
    said = " ".join(s.text.strip() for s in segments).strip()
    return "" if said.lower().strip(" .!") in JUNK else said


def main(limit=None):
    con = db()
    rows = con.execute(
        "select id, video_url from items where transcript is null and video_url != ''"
        " order by pos" + (f" limit {int(limit)}" if limit else "")
    ).fetchall()
    if not rows:
        print("nothing to transcribe")
        return 0
    model = WhisperModel(MODEL, device="cuda", compute_type="int8_float16")
    failures = 0
    for n, r in enumerate(rows, 1):
        started = time.time()
        try:
            said = transcribe(model, audio(r["video_url"]))
        except Exception as err:  # one unreadable video must not stop the batch
            if "403" in str(err) or "410" in str(err):
                print("video links have expired: run `python sync.py`, then rerun this")
                return 1
            print(f"[{n}/{len(rows)}] {r['id']} failed: {str(err)[-80:]}", flush=True)
            failures += 1
            if failures >= 5:  # network is down or we are being throttled; don't burn the list
                print("5 failures in a row, stopping: rerun later to continue")
                return 1
            time.sleep(10)
            continue
        failures = 0
        # clearing the embedding makes app.py re-read this item with its transcript
        con.execute("update items set transcript=?, embedding=null where id=?", (said, r["id"]))
        con.commit()
        print(f"[{n}/{len(rows)}] {r['id']} {time.time() - started:.0f}s: "
              f"{said[:90].encode('ascii', 'replace').decode() or '(no speech)'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
