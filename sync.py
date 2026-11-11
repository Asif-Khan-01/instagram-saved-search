"""Pull Instagram saved posts into saved.db using gallery-dl and the Firefox login.

    python sync.py          # everything
    python sync.py new      # only saves made since the last sync (what update.py uses)
    python sync.py 50       # only the 50 most recently saved posts
    python sync.py songs    # the Saved > Audio list (songs you saved), into table songs
    python sync.py prune    # list items you have unsaved on Instagram (full fetch; by hand only)
    python sync.py prune apply  # ...and move them into table items_removed
    python sync.py songlist # rebuild the merged song list (normally done automatically)
    python sync.py playlist # write every known song to playlist.csv for playlist import sites
    python sync.py --cached # re-read the last fetch without contacting Instagram
    python sync.py --check  # self-test, no network
"""
import csv
import io
import json
import re
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
    # every song once, merged across sources; rebuilt by rebuild_song_list()
    con.execute(
        """create table if not exists song_list(
            title text, artist text, sources text, times_seen integer, url text)"""
    )
    # one row per topic an item has; written by tag.py
    con.execute("create table if not exists item_topics(item_id text, topic text)")
    con.execute("create index if not exists item_topics_item on item_topics(item_id)")
    con.execute("drop view if exists topic_counts")
    con.execute(
        """create view topic_counts as
           select topic, count(*) as items from item_topics group by topic order by items desc"""
    )
    # every song mention, one row per source and reel, duplicates included
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
    rebuild_song_list(con)  # new saves can bring new songs
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


def unsaved(known, posts, errors, floor=0.9):
    """Which known items a FULL fetch says were unsaved, or why it is not safe to tell.
    A fetch cut short by an error or rate limit looks exactly like mass unsaving, so
    nothing is reported unless the fetch had no errors and returned most known items."""
    if errors:
        return set(), f"the fetch had errors ({errors[0]})"
    fetched = {p["id"] for p in posts}
    if len(fetched & known) < floor * len(known):
        return set(), f"the fetch returned only {len(fetched & known)} of {len(known)} known items"
    return known - fetched, ""


def prune(apply=False):
    """Move items you have unsaved on Instagram into table items_removed. Needs a full
    fetch of the saved list, the heavy request: run it by hand now and then, not nightly.
    Without "apply" it only lists what it would move. Archived rows keep their transcripts
    and descriptions, and the assistant, search and song list stop seeing them."""
    con = db()
    known = {r[0] for r in con.execute("select id from items")}
    posts, errors = parse(fetch())
    gone, problem = unsaved(known, posts, errors)
    if problem:
        print(f"Nothing changed: {problem}.")
        return 1
    for r in con.execute(f"select id, username, substr(caption, 1, 60) from items where id in "
                         f"({','.join('?' * len(gone))}) order by pos", sorted(gone)):
        print(f"  unsaved: {r[0]} @{r[1]} {r[2]!a}")
    if not apply:
        print(f"{len(gone)} unsaved items found. Nothing changed; run `python sync.py prune apply` to archive them.")
        return 0
    cols = [r[1] for r in con.execute("pragma table_info(items)")]
    con.execute("create table if not exists items_removed(removed_on text)")
    have = {r[1] for r in con.execute("pragma table_info(items_removed)")}
    for col in cols:
        if col not in have:
            con.execute(f"alter table items_removed add column {col}")
    names = ", ".join(cols)
    for item_id in gone:
        con.execute(f"insert into items_removed(removed_on, {names}) "
                    f"select date('now'), {names} from items where id = ?", (item_id,))
        con.execute("delete from items where id = ?", (item_id,))
        con.execute("delete from item_topics where item_id = ?", (item_id,))
    con.commit()
    store(con, posts)  # the full fetch also puts the save order (pos) right
    print(f"archived {len(gone)} unsaved items; {len(known) - len(gone)} remain")
    return 0


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
    mentions, distinct = rebuild_song_list(con)
    print(f"song list: {distinct} distinct songs from {mentions} mentions")
    return 0


# Title parts that name the same recording differently between sources. Version words
# (Acoustic, Slowed, Remix, Live, Movie Ver.) are kept: those are different recordings.
_TITLE_NOISE = re.compile(
    r"\s*[\(\[](?:feat\.?|ft\.?|with|from)\b[^)\]]*[\)\]]"   # (feat. X) (with X) (From "Film")
    r"|\s*[\(\[][^)\]]*remaster[^)\]]*[\)\]]"                # (2015 Remaster)
    r"|\s+-\s+[^-]*remaster.*$",                            # - Remastered 2011
    re.I,
)
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\band\b|\bx\b|\bfeat\.?|\bft\.?|\bwith\b)\s*", re.I)


def song_key(title):
    t = _TITLE_NOISE.sub("", title.lower())
    return " ".join(re.sub(r"[^\w ]", " ", t).split())


def artist_set(artist):
    return {a.strip().lower() for a in _ARTIST_SPLIT.split(artist or "") if a.strip()}


def merge_songs(rows):
    """Song mentions -> one entry per song. rows: dicts with title, artist, source, url,
    Saved-audio rows first. Two mentions are the same song when their titles match after
    song_key() and they share at least one artist. Two Saved-audio rows are never merged:
    the user saved them separately. Mentions with no artist are Instagram "original audio"
    names, not songs, and are left out."""
    songs, by_key = [], {}
    for r in rows:
        title, artist = (r["title"] or "").strip(), (r["artist"] or "").strip()
        if not title or not artist:
            continue
        key, names, saved = song_key(title), artist_set(artist), r["source"] == "Saved audio"
        match = next((s for s in by_key.get(key, [])
                      if s["names"] & names and not (saved and "Saved audio" in s["sources"])), None)
        if match:
            match["times_seen"] += 1
            match["names"] |= names
            if r["source"] not in match["sources"]:
                match["sources"].append(r["source"])
            continue
        song = {"title": title, "artist": artist, "sources": [r["source"]], "times_seen": 1,
                "url": r["url"], "names": names}
        songs.append(song)
        by_key.setdefault(key, []).append(song)
    return [{k: (", ".join(v) if k == "sources" else v) for k, v in s.items() if k != "names"}
            for s in songs]


def rebuild_song_list(con):
    rows = con.execute(
        """select title, artist, source, url from all_songs
           order by source != 'Saved audio', source""").fetchall()
    merged = merge_songs([dict(r) for r in rows])
    con.execute("delete from song_list")
    con.executemany("insert into song_list values(:title, :artist, :sources, :times_seen, :url)", merged)
    con.commit()
    return len(rows), len(merged)


def playlist_csv(rows):
    """Songs as the CSV that playlist transfer sites import: one row per distinct song."""
    out, seen = io.StringIO(), set()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Track name", "Artist name"])
    for r in rows:
        title, artist = (r["title"] or "").strip(), (r["artist"] or "").strip()
        # no artist means an Instagram "original audio" name such as "PC Tips (Part 21)",
        # which a music service would match to something random
        if title and artist and (title.lower(), artist.lower()) not in seen:
            seen.add((title.lower(), artist.lower()))
            w.writerow([title, artist])
    return out.getvalue()


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
    listed = playlist_csv([{"title": "A, b", "artist": "X"}, {"title": "a, B ", "artist": "x"},
                           {"title": "", "artist": "Y"}, {"title": "C", "artist": None}])
    assert listed == 'Track name,Artist name\n"A, b",X\n', listed
    known = {f"k{i}" for i in range(20)}
    full = [{"id": f"k{i}"} for i in range(19)] + [{"id": "new"}]
    assert unsaved(known, full, []) == ({"k19"}, "")
    assert unsaved(known, full, ["rate limited"])[0] == set()  # an error: never trust it
    cut_short = [{"id": f"k{i}"} for i in range(10)]
    gone, why = unsaved(known, cut_short, [])
    assert gone == set() and "10 of 20" in why, why  # half missing looks like a failed fetch
    S, U, H = "Saved audio", "Used in a saved reel", "Heard in a saved reel"
    merged = merge_songs([
        {"title": "Aarzu (with Asim Azhar)", "artist": "Asim Azhar, Noor, Khan, Madhurxo", "source": S, "url": "a"},
        {"title": "Main tera ho gaya", "artist": "Karneast, Riya", "source": S, "url": "b"},
        {"title": "Main tera ho gaya (Acoustic)", "artist": "Karneast, Riya", "source": S, "url": "c"},
        {"title": "Darkhaast (feat. Arijit Singh, Sunidhi Chauhan)", "artist": "Mithoon", "source": S, "url": "d"},
        {"title": "Ae Ajnabee | Coke Studio Bharat", "artist": "Aditya Rikhari, Ravator, Kutle Khan", "source": S, "url": "e"},
        {"title": "Main tera ho gaya", "artist": "Karneast", "source": S, "url": "o"},
        {"title": "Purple Rain (2015 Paisley Park Remaster)", "artist": "Prince & The Revolution", "source": U, "url": "f"},
        {"title": "Runaway (feat. Pusha T)", "artist": "Kanye West", "source": U, "url": "g"},
        {"title": "Coding Cheatsheet", "artist": "", "source": U, "url": "h"},
        {"title": "Aarzu", "artist": "Noor, Khan & Madhurxo", "source": H, "url": "i"},
        {"title": 'Darkhaast (From "Shivaay") (feat. Sunidhi Chauhan)', "artist": "Mithoon & Arijit Singh", "source": H, "url": "j"},
        {"title": "Ae Ajnabee | Coke Studio Bharat", "artist": "Aditya Rikhari, Ravator & Kutle Khan", "source": H, "url": "k"},
        {"title": "Purple Rain", "artist": "Prince & The Revolution", "source": H, "url": "l"},
        {"title": "Runaway", "artist": "AURORA", "source": H, "url": "m"},
        {"title": "Runaway (feat. Pusha T)", "artist": "Kanye West", "source": H, "url": "n"},
    ])
    got = sorted((m["title"], m["times_seen"], m["sources"]) for m in merged)
    assert got == sorted([
        ("Aarzu (with Asim Azhar)", 2, f"{S}, {H}"),
        ("Main tera ho gaya", 1, S),
        ("Main tera ho gaya", 1, S),  # saved twice on purpose: never merged
        ("Main tera ho gaya (Acoustic)", 1, S),  # a different recording
        ("Darkhaast (feat. Arijit Singh, Sunidhi Chauhan)", 2, f"{S}, {H}"),
        ("Ae Ajnabee | Coke Studio Bharat", 2, f"{S}, {H}"),
        ("Purple Rain (2015 Paisley Park Remaster)", 2, f"{U}, {H}"),
        ("Runaway (feat. Pusha T)", 2, f"{U}, {H}"),
        ("Runaway", 1, H),  # same title, different artist: a different song
    ]), got
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
    elif arg == "prune":
        sys.exit(prune(apply=sys.argv[2:] == ["apply"]))
    elif arg == "songlist":
        print("%d mentions -> %d distinct songs" % rebuild_song_list(db()))
    elif arg == "playlist":
        con = db()
        (ROOT / "playlist.csv").write_text(
            playlist_csv(con.execute("select title, artist from song_list").fetchall()), encoding="utf-8")
        print("wrote playlist.csv")
    elif arg == "songs":
        sys.exit(songs())
    elif arg == "--cached":
        sys.exit(main(cached=True))
    else:
        sys.exit(main(int(arg) if arg else None))
