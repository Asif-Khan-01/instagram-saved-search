"""Chat assistant over saved.db.    streamlit run app.py"""
import sqlite3
import urllib.error
from urllib.parse import quote_plus

import numpy as np
import streamlit as st
from fastembed import TextEmbedding

import brain
from sync import THUMBS, db, playlist_csv

# ponytail: English-only model; it beat the small multilingual one on test queries.
# Swap to intfloat/multilingual-e5-large (2 GB) if many captions are not English,
# then clear the embedding column so everything is re-read.
MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

@st.cache_resource
def model():
    return TextEmbedding(MODEL)


def embed(texts):
    return np.array(list(model().embed(texts)), dtype=np.float32)


def song_of(r):
    """The item's song as (title, artist). Instagram's own song counts only when it names an
    artist: without one it is an uploader's "original audio" label such as "Coding
    Cheatsheet", and the song found by listening (if any) is the real one."""
    if r["audio_title"] and r["audio_artist"]:
        return r["audio_title"], r["audio_artist"]
    if r["heard_title"]:
        return r["heard_title"], r["heard_artist"]
    return "", ""


def text_of(r):
    title, artist = song_of(r)
    song = f" song: {title} {artist}" if title else ""
    if r["audio_title"] and not r["audio_artist"]:  # the label can still describe the reel
        song += f" audio: {r['audio_title']}"
    shown = f" shown: {r['visual'][:500]}" if r["visual"] else ""
    said = f" spoken: {r['transcript']}" if r["transcript"] else ""
    # caption and visuals are capped so the model's ~2000-character window has room
    # for all three; a long transcript is the part that gets cut
    return f"{r['caption'][:500]} by {r['username']}{song}{shown}{said}"[:2000]


def index_new(con):
    rows = con.execute("select * from items where embedding is null").fetchall()
    if rows:
        with st.spinner(f"Reading {len(rows)} new saves..."):
            for r, v in zip(rows, embed([text_of(r) for r in rows])):
                con.execute("update items set embedding=? where id=?", (v.tobytes(), r["id"]))
            con.commit()


def search(con, query, where="1=1", params=(), k=12):
    # "embedding is not null": transcribe.py may clear one while this page is open
    rows = con.execute(
        f"select * from items where {where} and embedding is not null order by pos", params
    ).fetchall()
    if not rows:
        return []
    m = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
    q = embed([QUERY_PREFIX + query])[0]
    score = m @ q / (np.linalg.norm(m, axis=1) * np.linalg.norm(q) + 1e-9)
    # exact words (usernames, song names) are where embeddings are weakest
    words = [w for w in query.lower().split() if len(w) > 2]
    score += [0.15 * sum(w in text_of(r).lower() for w in words) for r in rows]
    return [rows[i] for i in np.argsort(-score)[:k]]


def cards(rows):
    for col, r in zip(st.columns(4) * (len(rows) // 4 + 1), rows):
        with col:
            thumb = THUMBS / f"{r['id']}.jpg"
            if thumb.exists():
                st.image(str(thumb))
            st.markdown(f"**{r['type']}** · @{r['username']}")
            title, artist = song_of(r)
            if title:
                heard = "heard: " if title == r["heard_title"] and not r["audio_artist"] else ""
                st.caption(f"♪ {heard}{title} — {artist}")
            st.write(r["caption"][:140] + ("…" if len(r["caption"]) > 140 else ""))
            if r["visual"]:
                with st.expander("What is shown"):
                    st.write(r["visual"])
            if r["transcript"]:
                with st.expander("What is said"):
                    st.write(r["transcript"][:1500])
            st.link_button("Open", r["url"])


def song_links(rows):
    """Adds Spotify and YouTube search links to rows that have a title and artist."""
    for s in rows:
        find = quote_plus(f"{s.get('title', '')} {s.get('artist', '')}")
        s["spotify"] = f"https://open.spotify.com/search/{find}"
        s["youtube"] = f"https://www.youtube.com/results?search_query={find}"
    return rows


def table(rows, key):
    links = {c: st.column_config.LinkColumn(display_text="Open")
             for c, v in (rows[0].items() if rows else []) if str(v).startswith("https://")}
    st.dataframe(rows, hide_index=True, column_config=links)
    if rows and {"title", "artist"} <= set(rows[0]):  # a song list: offer it as a playlist
        st.download_button("Download as playlist file", playlist_csv(rows), "playlist.csv",
                           "text/csv", key=f"playlist-{key}",
                           help="Import it into Spotify, YouTube Music or Apple Music with a "
                                "playlist transfer site such as TuneMyMusic or Soundiiz.")


def filters(p):
    """The planner's optional filters as a SQL condition for search()."""
    where, params = ["1=1"], []
    if p["type"] in ("reel", "post"):
        where.append("type = ?"); params.append(p["type"])
    if p["username"]:
        where.append("lower(username) like ?"); params.append(f"%{p['username'].lower().lstrip('@')}%")
    if p["date_from"]:
        where.append("post_date >= ?"); params.append(p["date_from"])
    if p["date_to"]:
        where.append("post_date <= ?"); params.append(p["date_to"] + " 23:59:59")
    return " and ".join(where), params


SUMMARY_ITEMS = 20  # read in batches of 5: four model calls, roughly half a minute


def summarise(q, rows):
    """Points the saved items make about the question, each linked to the item it came
    from. Returns the reply text and the ids of the items that were cited."""
    # date and type are included so the model does not guess them
    notes = [f"{r['type']} by @{r['username']}, posted {r['post_date'][:10]}: {text_of(r)[:700]}"
             for r in rows]
    lines, cited = [], []
    for start in range(0, len(rows), 5):
        for i, point in brain.points(q, notes[start:start + 5]):
            r = rows[start + i]
            made_up = brain.unsupported(point, notes[start + i])
            warn = f" ⚠ *not found in that item: {', '.join(made_up)}*" if made_up else ""
            lines.append(f"- {point} — [@{r['username']}]({r['url']}){warn}")
            if r["id"] not in cited:
                cited.append(r["id"])
    if not lines:
        return f"I read the {len(rows)} closest saves and none of them clearly covers this.", []
    head = f"From {len(cited)} of the {len(rows)} closest saves (each point links to its source):\n\n"
    return head + "\n".join(lines), cited


def respond(con, q, history):
    """One assistant turn: plan with the local model, fetch, then word the reply."""
    p = brain.plan(q, history)
    out = {"q": q, "plan": p, "text": "", "ids": [], "table": []}
    if p["action"] == "sql":
        for attempt in range(2):
            try:
                cols, rows = brain.run_sql(p["sql"])
                break
            except sqlite3.Error as err:  # show the model its mistake once, then give up on SQL
                p = out["plan"] = brain.plan(q, history, error=str(err))
                if p["action"] != "sql":
                    break
        else:
            p["action"], p["query"] = "search", p["query"] or q
    if p["action"] == "sql":
        if cols[:1] == ["id"]:
            out["ids"] = [r[0] for r in rows]
        else:
            out["table"] = [dict(zip(cols, r)) for r in rows]
            if {"title", "artist"} <= set(cols):
                song_links(out["table"])
        out["text"] = brain.answer(q, brain.table_text(cols, rows))
    elif p["action"] == "search":
        where, params = filters(p)
        rows = search(con, p["query"] or q, where, params, k=SUMMARY_ITEMS if p["summarise"] else 12)
        out["ids"] = [r["id"] for r in rows]
        if p["summarise"] and rows:
            out["text"], cited = summarise(q, rows)
            out["ids"] = cited or out["ids"][:8]
        else:
            out["text"] = f"The {len(rows)} closest matches:" if rows else "Nothing matched."
    else:
        out["text"] = brain.answer(q, "No data needed. This tool can find saved Instagram reels and "
                                      "posts by topic, list or count them, list saved songs, and "
                                      "summarise what saved items say.")
    return out


st.set_page_config(page_title="Saved search", layout="wide")
con = db()
index_new(con)
total = con.execute("select count(*) from items").fetchone()[0]
st.sidebar.caption(f"{total} saved items. Run `python update.py` to fetch new ones.")

if st.sidebar.toggle("Song list"):
    songs = song_links([dict(r) for r in con.execute(
        "select title, artist, sources, times_seen, url from song_list")])
    st.subheader(f"{len(songs)} songs")
    table(songs, "all")
    st.stop()

counts = dict(con.execute("select topic, count(*) from item_topics group by topic order by 2 desc"))
if topic := st.sidebar.selectbox("Browse a topic", list(counts), index=None,
                                 format_func=lambda t: f"{t} ({counts[t]})"):
    rows = con.execute(
        """select i.* from items i join item_topics t on t.item_id = i.id
           where t.topic = ? order by i.pos""", (topic,)).fetchall()
    st.subheader(f"{topic}: {len(rows)} saved items")
    if len(rows) > 48:
        st.caption("Showing the 48 most recently saved.")
    cards(rows[:48])
    st.stop()

if "chat" not in st.session_state:
    st.session_state.chat = []
if q := st.chat_input("Ask about your saved items"):
    with st.spinner("Thinking..."):
        history = [(m["q"], m["plan"]) for m in st.session_state.chat if m["plan"]]
        try:
            st.session_state.chat.append(respond(con, q, history))
        except (urllib.error.URLError, ValueError, KeyError) as err:
            # Ollama not running, or the model wrote a broken plan: plain search still works
            st.session_state.chat.append({
                "q": q, "plan": None, "table": [], "ids": [r["id"] for r in search(con, q)],
                "text": f"The assistant model was unavailable ({err}), so this is a plain search."})
if not st.session_state.chat:
    st.info("Try: list all my saved songs · how many reels per account · reels about AI tools "
            "from 2026 · summarise the career advice I saved")
for n, m in enumerate(st.session_state.chat):
    st.chat_message("user").write(m["q"])
    with st.chat_message("assistant"):
        st.write(m["text"])
        if m["table"]:
            table(m["table"], n)
        if m["ids"]:
            marks = ",".join("?" * len(m["ids"]))
            found = {r["id"]: r for r in con.execute(f"select * from items where id in ({marks})", m["ids"])}
            shown = [found[i] for i in m["ids"] if i in found][:48]
            if len(found) > len(shown):
                st.caption(f"Showing the first {len(shown)} of {len(found)}.")
            cards(shown)
        if m["plan"]:
            with st.expander("How I read this"):
                st.json({k: v for k, v in m["plan"].items() if v not in ("", "any", False)})
