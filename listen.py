"""Identify the song playing in saved reels that Instagram labels only "Original audio".

    python listen.py clips      # only save audio clips (needs fresh video links from sync.py)
    python listen.py            # save missing clips, then identify every clip not tried yet
    python listen.py 5          # same, but identify only the next 5

A 12-second clip of each reel is saved in clips/, then sent as a fingerprint to
Shazam through the unofficial shazamio library. Safe to stop and rerun.
"""
import asyncio
import subprocess
import sys
import time

from sync import ROOT, db

CLIPS = ROOT / "clips"
TODO = "video_url != '' and audio_title = ''"  # reels without a song named by Instagram


def grab(con):
    """Save a short audio clip for every reel that has none. Returns False if it gave up."""
    CLIPS.mkdir(exist_ok=True)
    rows = con.execute(f"select id, video_url from items where {TODO} order by pos").fetchall()
    todo = [r for r in rows if not (CLIPS / f"{r['id']}.mp3").exists()]
    failures = 0
    for n, r in enumerate(todo, 1):
        out = CLIPS / f"{r['id']}.mp3"
        # ponytail: one clip from 3-15 s in; a song that only starts later is missed.
        # Try a second window if the match rate turns out low.
        run = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-user_agent", "Mozilla/5.0", "-ss", "3", "-t", "12",
             "-i", r["video_url"], "-vn", "-ac", "1", "-ar", "44100", "-b:a", "64k", str(out)],
            capture_output=True, timeout=300,
        )
        full = run.stderr.decode(errors="replace").strip()
        err = full[-80:]
        if "does not contain any stream" in full or (run.returncode == 0 and not out.stat().st_size):
            out.write_bytes(b"")  # silent or shorter than 3 s: an empty file marks it as done
        if out.exists():
            failures = 0
            print(f"clip [{n}/{len(todo)}] {r['id']}", flush=True)
            continue
        if "403" in err or "410" in err:
            print("video links have expired: run `python sync.py`, then rerun this")
            return False
        print(f"clip [{n}/{len(todo)}] {r['id']} failed: {err}", flush=True)
        failures += 1
        if failures >= 5:  # same connection drop-outs as transcribe.py
            print("5 failures in a row, stopping: rerun later to continue")
            return False
        time.sleep(10)
    return True


async def identify(con, limit):
    from shazamio import Shazam

    shazam = Shazam()
    rows = con.execute(f"select id from items where {TODO} and heard_title is null order by pos").fetchall()
    done = found = failures = 0
    for r in rows:
        clip = CLIPS / f"{r['id']}.mp3"
        if not clip.exists():
            continue  # no clip yet
        title = artist = ""
        if clip.stat().st_size:
            try:
                track = (await shazam.recognize(str(clip))).get("track") or {}
            except Exception as err:  # rate limit or network: leave it untried and slow down
                print(f"{r['id']} lookup failed: {str(err)[:80]}", flush=True)
                failures += 1
                if failures >= 5:
                    print("5 lookups failed in a row, stopping: rerun later to continue")
                    break
                await asyncio.sleep(30)
                continue
            failures = 0
            title, artist = track.get("title") or "", track.get("subtitle") or ""
            await asyncio.sleep(2)  # unofficial service: stay well under its rate limit
        con.execute("update items set heard_title=?, heard_artist=? where id=?", (title, artist, r["id"]))
        if title:  # clearing the embedding makes app.py re-read this item with its song
            con.execute("update items set embedding=null where id=?", (r["id"],))
        con.commit()
        done += 1
        found += bool(title)
        print(f"[{done}] {r['id']}: "
              f"{(title + ' - ' + artist).encode('ascii', 'replace').decode() if title else '(no match)'}",
              flush=True)
        if limit and done >= int(limit):
            break
    print(f"tried {done} reels, found a song in {found}")


def main(limit=None):
    con = db()
    ok = grab(con)
    if limit != "clips":
        asyncio.run(identify(con, limit))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
