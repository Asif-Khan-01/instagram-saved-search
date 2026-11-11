"""Add a description of what is shown in each saved item to saved.db.

    python visuals.py frames    # only grab frames (needs fresh video links from sync.py)
    python visuals.py           # grab missing frames, then describe everything not done yet
    python visuals.py 5         # same, but describe only the next 5

For a reel, four frames (one every 5 seconds) are saved as a single 2x2 image in
frames/. Photo posts use the thumbnail sync.py already saved. A local vision
model in Ollama then describes each image. Safe to stop and rerun.
"""
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

from sync import ROOT, THUMBS, db

FRAMES = ROOT / "frames"
# the plain qwen3-vl:4b tag "thinks" and often returns nothing; -instruct answers directly
MODEL = "qwen3-vl:4b-instruct"  # fits the 6 GB RTX 4050
PROMPT = (
    "This image shows four frames from one short social media video, or a single photo. "
    "In two or three sentences say what is shown: people, objects, place, activity. "
    "Then write any text that appears on screen. Plain factual English, no preamble."
)


def grab(con):
    """Save a 2x2 frame grid for every reel that has none. Returns False if it gave up."""
    FRAMES.mkdir(exist_ok=True)
    rows = con.execute("select id, video_url from items where video_url != '' order by pos").fetchall()
    todo = [r for r in rows if not (FRAMES / f"{r['id']}.jpg").exists()]
    failures = 0
    for n, r in enumerate(todo, 1):
        out = FRAMES / f"{r['id']}.jpg"
        run = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-user_agent", "Mozilla/5.0", "-t", "20",
             "-i", r["video_url"], "-vf", "fps=1/5,scale=480:-2,tile=2x2",
             "-frames:v", "1", "-q:v", "4", str(out)],
            capture_output=True, timeout=300,
        )
        if out.exists() and out.stat().st_size:
            failures = 0
            print(f"frames [{n}/{len(todo)}] {r['id']}", flush=True)
            continue
        err = run.stderr.decode(errors="replace").strip()[-80:]
        if "403" in err or "410" in err:
            print("video links have expired: run `python sync.py`, then rerun this")
            return False
        print(f"frames [{n}/{len(todo)}] {r['id']} failed: {err}", flush=True)
        failures += 1
        if failures >= 5:  # same connection drop-outs as transcribe.py
            print("5 failures in a row, stopping: rerun later to continue")
            return False
        time.sleep(10)
    return True


def describe(image):
    body = json.dumps({
        "model": MODEL, "prompt": PROMPT, "stream": False,
        "images": [base64.b64encode(image.read_bytes()).decode()],
        # num_ctx: a large photo alone can exceed Ollama's default 4096-token window
        "options": {"temperature": 0, "num_predict": 300, "num_ctx": 8192},
    }).encode()
    req = urllib.request.Request("http://localhost:11434/api/generate", body,
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())["response"].strip()


def main(limit=None):
    con = db()
    ok = grab(con)
    if limit == "frames":
        return 0 if ok else 1
    rows = con.execute("select id from items where visual is null order by pos").fetchall()
    done = 0
    for r in rows:
        image = next((p for p in (FRAMES / f"{r['id']}.jpg", THUMBS / f"{r['id']}.jpg") if p.exists()), None)
        if not image:
            continue  # no frames and no thumbnail yet
        started = time.time()
        try:
            seen = describe(image)
        except urllib.error.HTTPError as err:
            if err.code != 400:
                raise  # Ollama down or model missing: stop the run
            print(f"{r['id']} rejected: {err.read()[:120]}", flush=True)  # this image only
            continue
        if not seen:  # leave it null so a later run retries it
            print(f"{r['id']} came back empty, skipped", flush=True)
            continue
        # clearing the embedding makes app.py re-read this item with its description
        con.execute("update items set visual=?, embedding=null where id=?", (seen, r["id"]))
        con.commit()
        done += 1
        print(f"[{done}] {r['id']} {time.time() - started:.0f}s: "
              f"{seen[:100].encode('ascii', 'replace').decode()}", flush=True)
        if limit and done >= int(limit):
            break
    print(f"described {done} items")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
