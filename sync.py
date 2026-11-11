"""Pull Instagram saved posts into saved.db using gallery-dl and the Firefox login.

    python sync.py          # everything
    python sync.py new      # only saves made since the last sync (what update.py uses)
    python sync.py 50       # only the 50 most recently saved posts
    python sync.py songs    # the Saved > Audio list (songs you saved), into table songs
    python sync.py --cached # re-read the last fetch without contacting Instagram
    python sync.py --check  # self-test, no network
"""
import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
DB = ROOT / "saved.db"
THUMBS = ROOT / "thumbs"
RAW = ROOT / "last_fetch.json"
BROWSER = "firefox"  # the browser that is logged in to Instagram


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute(
        """create table if not exists items(
            id text primary key, platform text, type text, username text,
            caption text, tags text, audio_title text, audio_artist text,
            url text, thumb_url text, post_date text, pos integer,
            embedding blob)"""
    )
    # added after the first version; transcript null = not tried yet, '' = no speech
    # visual (what is shown) follows the same null / '' convention
    for col in ("video_url text", "transcript text", "visual text",
                "heard_title text", "heard_artist text",  # song found by listen.py
                "topics text"):  # "ai, tech": same data as item_topics, for join-free queries
        try:
            con.execute(f"alter table items add column {col}")
        except sqlite3.OperationalError:
            pass  # already there
    con.execute(
        """create table if not exists songs(
            id text primary key, title text, artist text, url text,
            seconds integer, pos integer)"""
    )
    # every song the tool knows about, whatever its source; brain.py queries this
    # one row per topic an item has; written by tag.py
    con.execute("create table if not exists item_topics(item_id text, topic text)")
    con.execute("create index if not exists item_topics_item on item_topics(item_id)")
    con.execute("drop view if exists topic_counts")
    con.execute(
        """create view topic_counts as
           select topic, count(*) as items from item_topics group by topic order by items desc"""
    )
    con.execute("drop view if exists all_songs")  # recreated so changes to it take effect
    con.execute(
        """create view all_songs as
           select title, artist, 'Saved audio' as source, url from songs
           union all
           select audio_title, audio_artist, 'Used in a saved ' || type, url
           from items where audio_title != ''
           union all
           select heard_title, heard_artist, 'Heard in a saved ' || type, url
           from items where heard_title != ''"""
    )
    return con


def text(v):
    """Instagram sometimes sends a list (several artists) where a string is expected."""
    return ", ".join(map(str, v)) if isinstance(v, list) else str(v or "")


def parse(entries):
    """gallery-dl -j output -> one dict per post, in saved order (newest first)."""
    posts, errors = {}, []
    for e in entries:
        m = e[-1] if isinstance(e[-1], dict) else {}
        if "error" in m:
            errors.append(m.get("message", m["error"]))
        sc = m.get("post_shortcode")
        if e[0] != 3 or not sc or sc in posts:  # 3 = media file; keep first per post
            continue
        posts[sc] = dict(
            id=sc,
            platform="instagram",
            # Instagram labels many videos "post", so go by the file itself
            type="reel" if m.get("video_url") or m.get("type") == "reel" else "post",
            username=text(m.get("username")),
            caption=text(m.get("description")),
            tags=" ".join(m.get("tags") or []),
            audio_title=text(m.get("audio_title")).replace("Original audio", ""),
            audio_artist=text(m.get("audio_artist")),
            url=m.get("post_url") or f"https://www.instagram.com/p/{sc}/",
            thumb_url=m.get("display_url") or "",
            video_url=m.get("video_url") or "",  # signed link, expires after a while
            post_date=str(m.get("post_date") or m.get("date") or ""),
            pos=len(posts),
        )
    return list(posts.values()), errors


def fetch(limit=None):
    # ponytail: -j prints everything at the end, so Ctrl+C loses the run.
    # Switch to --write-metadata files if runs get long enough to need resuming.
    cmd = [sys.executable, "-m", "gallery_dl", "--cookies-from-browser", BROWSER, "-j"]
    if limit:
        cmd += ["--post-range", f"1-{limit}"]
    cmd.append("https://www.instagram.com/me/saved/")
    out = subprocess.run(cmd, stdout=subprocess.PIPE, encoding="utf-8").stdout
    RAW.write_text(out or "[]", encoding="utf-8")  # so a later crash never costs a refetch
    return json.loads(out or "[]")


def save_thumb(item):
    path = THUMBS / f"{item['id']}.jpg"
    if path.exists() or not item["thumb_url"].startswith("https://"):
        return
    try:
        req = urllib.request.Request(item["thumb_url"], headers={"User-Agent": "Mozilla/5.0"})
        path.write_bytes(urllib.request.urlopen(req, timeout=30).read())
    except OSError as err:
        print(f"  thumbnail failed for {item['id']}: {err}")


def store(con, posts):
    THUMBS.mkdir(exist_ok=True)
    for p in posts:
        # keeps an existing embedding; app.py re-embeds only rows without one
        con.execute(
            """insert into items(id,platform,type,username,caption,tags,audio_title,
                   audio_artist,url,thumb_url,video_url,post_date,pos)
               values(:id,:platform,:type,:username,:caption,:tags,:audio_title,
                   :audio_artist,:url,:thumb_url,:video_url,:post_date,:pos)
               on conflict(id) do update set pos=excluded.pos, thumb_url=excluded.thumb_url,
                   video_url=excluded.video_url,
                   type=excluded.type, audio_title=excluded.audio_title""",
            p,
        )
    con.commit()  # before the slow thumbnail downloads, so app.py is not locked out
    for p in posts:
        save_thumb(p)


def main(limit=None, cached=False):
    raw = json.loads(RAW.read_text(encoding="utf-8")) if cached else fetch(limit)
    posts, errors = parse(raw)
    con = db()
    store(con, posts)
    total = con.execute("select count(*) from items").fetchone()[0]
    print(f"fetched {len(posts)} posts, {total} in saved.db")
    for e in errors:
        print("ERROR from Instagram:", e)
    return 1 if errors else 0


def fresh(posts, known):
    """The posts not in the database yet, and whether older unseen saves may remain
    (true when every fetched post was new, so the fetch did not reach known ground)."""
    new = [p for p in posts if p["id"] not in known]
    return new, bool(posts) and len(new) == len(posts)


def new(first=30):
    """Fetch only saves made since the last sync. Saves come newest first, so it reads
    a small batch and widens it only while every post in the batch is new."""
    con = db()
    known = {r[0] for r in con.execute("select id from items")}
    n = first
    while True:
        posts, errors = parse(fetch(n))
        added, maybe_more = fresh(posts, known)
        if errors or not maybe_more or len(posts) < n:
            break
        n *= 4
    # everything already stored moves down by the number of new saves (pos 0 = newest)
    con.execute("update items set pos = pos + ?", (len(added),))
    store(con, posts)
    print(f"{len(added)} new saves, {len(known) + len(added)} in saved.db")
    for e in errors:
        print("ERROR from Instagram:", e)
    return 1 if errors else 0


def parse_songs(items, start=0):
    """One page of Instagram's saved-audio list -> rows for the songs table."""
    rows = []
    for it in items:
        t = it.get("track") or it.get("original_sound") or {}
        sid = text(t.get("audio_cluster_id") or t.get("id"))
        title = text(t.get("title") or t.get("original_audio_title"))
        if not sid or not title:
            continue
        rows.append(dict(
            id=sid,
            title=title,
            artist=text(t.get("display_artist") or (t.get("ig_artist") or {}).get("username")),
            url=f"https://www.instagram.com/reels/audio/{sid}/",
            seconds=int(t.get("duration_in_ms") or 0) // 1000,
            pos=start + len(rows),
        ))
    return rows


def songs():
    """The web page's own request for Saved > Audio, made through gallery-dl's session."""
    from gallery_dl import config, extractor

    config.set((), "cookies", [BROWSER])
    ex = extractor.find("https://www.instagram.com/me/saved/")
    ex.initialize()
    me = ex.api.user_by_id(ex.cookies.get("ds_user_id", domain=".instagram.com"))["username"]
    path = f"/{me}/saved/audio"
    lsd, dtsg = ex.api._extract_fb_tokens(path)
    headers = {
        "Accept": "*/*", "X-CSRFToken": ex.csrf_token, "X-IG-App-ID": "936619743392459",
        "X-FB-LSD": lsd, "X-ASBD-ID": "359341", "Origin": ex.root, "Referer": f"{ex.root}{path}/",
    }
    rows, cursor, skipped = [], "", 0
    while True:
        reply = ex.request(
            ex.root + "/api/v1/music/playlist/bookmarked/", method="POST", headers=headers,
            data={"cursor": cursor, "fb_dtsg": dtsg, "lsd": lsd, "__a": "1"},
        ).text
        page = json.loads(reply.removeprefix("for (;;);"))["payload"]
        got = parse_songs(page["items"], len(rows))
        skipped += len(page["items"]) - len(got)
        rows += got
        info = page.get("page_info") or {}
        cursor = info.get("next_max_id")
        if not info.get("more_available") or not cursor:
            break
        time.sleep(3)  # be gentle: this is the logged-in account
    con = db()
    for r in rows:
        con.execute(
            """insert into songs values(:id,:title,:artist,:url,:seconds,:pos)
               on conflict(id) do update set pos=excluded.pos""", r)
    con.commit()
    print(f"fetched {len(rows)} saved songs" + (f", {skipped} entries not understood" if skipped else ""))
    return 0


def check():
    sample = [
        [2, {"post_shortcode": "A"}],
        [3, "u1", {"post_shortcode": "A", "type": "reel", "username": "x", "description": "hi",
                   "tags": ["#a", "#b"], "audio_title": "Song", "audio_artist": ["p", "q"],
                   "display_url": "https://t/1"}],
        [3, "u2", {"post_shortcode": "A", "display_url": "https://t/2"}],  # carousel 2nd file
        [3, "u3", {"post_shortcode": "B"}],
        [-1, {"error": "AbortExtraction", "message": "boom"}],
    ]
    posts, errors = parse(sample)
    assert [p["id"] for p in posts] == ["A", "B"], posts
    assert posts[0]["thumb_url"] == "https://t/1" and posts[0]["tags"] == "#a #b"
    assert posts[1]["type"] == "post" and posts[1]["pos"] == 1
    assert posts[0]["audio_artist"] == "p, q"
    assert errors == ["boom"]
    known = {"A"}
    assert fresh(posts, known) == ([posts[1]], False)  # reached a known save: stop
    assert fresh(posts, set()) == (posts, True)  # all new: older unseen saves may remain
    assert fresh([], known) == ([], False)
    got = parse_songs([
        {"track": {"audio_cluster_id": "9", "title": "T", "display_artist": "A", "duration_in_ms": 61500}},
        {"original_sound": {"audio_cluster_id": "8", "original_audio_title": "O", "ig_artist": {"username": "u"}}},
        {"metadata": {}},
    ], start=5)
    assert [(r["id"], r["title"], r["artist"], r["seconds"], r["pos"]) for r in got] == [
        ("9", "T", "A", 61, 5), ("8", "O", "u", 0, 6)], got
    print("ok")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--check":
        check()
    elif arg == "new":
        sys.exit(new())
    elif arg == "songs":
        sys.exit(songs())
    elif arg == "--cached":
        sys.exit(main(cached=True))
    else:
        sys.exit(main(int(arg) if arg else None))
