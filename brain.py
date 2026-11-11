"""The assistant's brain: a local Ollama model that reads a question, decides how
to answer it from saved.db, and writes the reply. app.py does the searching.

    python brain.py "how many reels did I save per account?"   # show the plan and SQL result
    python brain.py --check                                     # self-test, no model needed
"""
import datetime
import difflib
import json
import re
import sqlite3
import sys
import urllib.request

from sync import DB, db

MODEL = "qwen3-vl:4b-instruct"  # already installed for visuals.py; swap for a bigger one if plans are poor
HIDE = {"embedding", "thumb_url", "video_url", "tags"}  # never useful in an answer
MAX_ROWS = 500
# Fixed so that counts add up; drawn up from a sample of real saves. After editing,
# run `python tag.py redo`.
TOPICS = [
    "ai", "coding", "tech", "career", "business and money", "design and art", "anime",
    "movies and tv", "music", "gaming", "books and poetry", "motivation", "mental health",
    "love", "humor", "spirituality", "education", "health and fitness", "food", "travel",
    "fashion", "other",
]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"enum": ["search", "sql", "chat"]},
        "query": {"type": "string"},
        "type": {"enum": ["any", "reel", "post"]},
        "username": {"type": "string"},
        "date_from": {"type": "string"},
        "date_to": {"type": "string"},
        "summarise": {"type": "boolean"},
        "sql": {"type": "string"},
    },
    "required": ["action", "query", "type", "username", "date_from", "date_to", "summarise", "sql"],
}

PLANNER = """You plan how to answer questions about a person's saved Instagram items. Today is {today}.
Reply with JSON only.

Choose "action":
- "search": the question is about a TOPIC or what items are about (cooking, AI tools, sad songs,
  advice on money). Put the topic in "query" as a few plain words. Optional filters:
  "type" (reel/post/any), "username" (account name without @, else ""),
  "date_from"/"date_to" as YYYY-MM-DD for when it was posted (else "").
  Set "summarise" true only if the user wants the content explained, summarised or compared.
- "sql": the question needs counting, grouping, ranking, "all"/"every"/"list", songs or music
  lists, or exact filters with no topic. Write ONE SQLite SELECT in "sql".
- "chat": greetings or questions about yourself. No data needed.

Tables for "sql":
items(id, type, username, caption, audio_title, audio_artist, url, post_date, pos, transcript, visual, topics)
  one saved item. type is 'reel' (any video) or 'post' (photo). username is the account that
  posted it. post_date is text 'YYYY-MM-DD HH:MM:SS' (when it was POSTED; the save date is
  unknown). pos is save order: 0 = saved most recently. transcript = what is said,
  visual = what is shown; both are English text and may be '' or NULL.
  topics holds one or two of: {topics}
  as text like 'ai, tech'. Match with topics like '%anime%'. Use it when the user asks for all
  saves in one of these broad topics; a narrower subject (pasta recipes, resume tips) is a "search".
all_songs(title, artist, source, url)
  every known song. source is 'Saved audio' (the user's saved songs list),
  'Used in a saved reel', 'Used in a saved post' (named by Instagram) or
  'Heard in a saved reel' (identified by listening).
topic_counts(topic, items)
  how many saved items each topic has. Use it for "what topics do I save most".
songs(title, artist, seconds, url, pos)
  only the user's saved songs list, with each song's length in seconds. pos 0 = saved most recently.

SQL rules: SELECT only. Never select embedding. When the answer is a set of saved items, select
id first (select id, username, ... from items). Use lower(x) like '%word%' for text matching.
Use strftime('%Y', post_date) for years. No LIMIT unless the user asks for a number or "top".
Names are never exact: an artist cell can hold several artists, so ALWAYS match artist, title and
username with lower(x) like '%name%', never with =.
A topic plus a date, type or account is still "search": put the topic in "query" and use the filters.
For follow-up questions, change the previous plan instead of starting again.
Unused text fields are "" and "sql" is "" unless action is "sql".

Examples:
"funny cat reels from 2025" -> {{"action":"search","query":"funny cats","type":"reel","username":"","date_from":"2025-01-01","date_to":"2025-12-31","summarise":false,"sql":""}}
"songs by Drake" -> {{"action":"sql","query":"","type":"any","username":"","date_from":"","date_to":"","summarise":false,"sql":"select title, artist, source, url from all_songs where lower(artist) like '%drake%'"}}
"everything from nasa" -> {{"action":"sql","query":"","type":"any","username":"","date_from":"","date_to":"","summarise":false,"sql":"select id, username, caption from items where lower(username) like '%nasa%' order by pos"}}
"what topics do I save most?" -> {{"action":"sql","query":"","type":"any","username":"","date_from":"","date_to":"","summarise":false,"sql":"select topic, items from topic_counts"}}
"show all my anime saves" -> {{"action":"sql","query":"","type":"any","username":"","date_from":"","date_to":"","summarise":false,"sql":"select id, username, caption from items where topics like '%anime%' order by pos"}}
"what do my saved reels say about saving money?" -> {{"action":"search","query":"saving money","type":"reel","username":"","date_from":"","date_to":"","summarise":true,"sql":""}}"""

ANSWERER = """You are the assistant inside a tool that searches a person's saved Instagram items.
Answer the user's question in plain English using ONLY the data given. Be brief: one to four
sentences, or a short bullet list for a summary. The user already sees the full table or the
item cards below your reply, so do not repeat long lists; state the count and what stands out.
When the data says more rows are not shown, you are seeing only a sample: give the total count
but do not claim anything about "all" rows. If the data is empty, say nothing matched. Never invent items, accounts, songs or numbers. Do not calculate percentages, sums or
averages yourself: report only numbers that appear in the data."""


def ask(messages, schema=None, max_tokens=500):
    body = {"model": MODEL, "messages": messages, "stream": False,
            "options": {"temperature": 0, "num_ctx": 8192, "num_predict": max_tokens}}
    if schema:
        body["format"] = schema
    req = urllib.request.Request("http://localhost:11434/api/chat", json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())["message"]["content"].strip()


def plan(question, history=(), error=None):
    """history: earlier (question, plan) pairs, oldest first. error: why the last SQL failed."""
    messages = [{"role": "system", "content": PLANNER.format(today=datetime.date.today(), topics=", ".join(TOPICS))}]
    for q, p in list(history)[-3:]:
        messages += [{"role": "user", "content": q}, {"role": "assistant", "content": json.dumps(p)}]
    messages.append({"role": "user", "content": question})
    if error:
        messages.append({"role": "user", "content": f"That SQL failed: {error}. Reply with corrected JSON."})
    p = json.loads(ask(messages, PLAN_SCHEMA))
    p["sql"] = fix_names(p["sql"], question)
    return p


SCHEMA_WORDS = {"reel", "post", "saved", "audio", "used", "heard"} | set(" ".join(TOPICS).split())  # values the SQL must keep as is


def fix_names(sql, question):
    """Local models misspell names they copy ("Arijit" became "arjit" with two different
    models). Inside quoted SQL text, a word the user did not type is swapped for the closest
    word they did type."""
    asked = re.findall(r"[a-z0-9_.]+", question.lower())

    def word(m):
        w = m.group(0).lower()
        if w in asked or w in SCHEMA_WORDS or len(w) < 4:
            return m.group(0)
        near = difflib.get_close_matches(w, asked, n=1, cutoff=0.8)
        return near[0] if near else m.group(0)

    return re.sub(r"'[^']*'", lambda q: re.sub(r"[A-Za-z0-9_.]+", word, q.group(0)), sql)


def run_sql(sql):
    """Runs model-written SQL. The connection is read-only, so it cannot change saved.db."""
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description or []]
        keep = [i for i, c in enumerate(cols) if c.lower() not in HIDE]
        rows = [[r[i] for i in keep] for r in cur.fetchmany(MAX_ROWS)]
        return [cols[i] for i in keep], rows
    finally:
        con.close()


def answer(question, data):
    return ask([{"role": "system", "content": ANSWERER},
                {"role": "user", "content": f"Question: {question}\n\nData:\n{data}"}])


EXTRACTOR = """You report what a person's saved Instagram items say about their question.
The items are numbered. For each item that is relevant to the question, write one or two short
points in plain English: a concrete claim, tip or fact taken from that item. Give the item's
number as "source". Skip items that are not relevant; an empty list is a fine answer.
Use only what the item's text says. No outside knowledge, and no names, tools or numbers that
are not in that item's text. Reply with JSON."""


def points(question, notes):
    """What a batch of items says about the question, as (index into notes, point) pairs.
    The schema only allows the batch's own numbers as sources, so a point cannot cite
    an item that was not read."""
    schema = {
        "type": "object",
        "properties": {"points": {"type": "array", "maxItems": 2 * len(notes), "items": {
            "type": "object",
            "properties": {"source": {"enum": list(range(1, len(notes) + 1))}, "point": {"type": "string"}},
            "required": ["source", "point"]}}},
        "required": ["points"],
    }
    numbered = "\n\n".join(f"[{n}] {text}" for n, text in enumerate(notes, 1))
    reply = ask([{"role": "system", "content": EXTRACTOR},
                 {"role": "user", "content": f"Question: {question}\n\nItems:\n{numbered}"}], schema, 700)
    return [(p["source"] - 1, p["point"].strip()) for p in json.loads(reply)["points"] if p["point"].strip()]


def unsupported(point, source):
    """Names and numbers in a point that do not occur in the item it cites: the usual
    shape of a made-up detail. Words that only start a sentence are not counted."""
    source = source.lower()
    missing = []
    for m in re.finditer(r"[A-Z][\w'-]+|\d[\d.,]*\d|\d", point):
        starts_sentence = m.start() == 0 or point[:m.start()].rstrip().endswith((".", "!", "?", ":"))
        word = m.group(0)
        if word[0].isalpha() and starts_sentence:
            continue
        if word.lower() not in source and word.lower().rstrip("s") not in source:
            missing.append(word)
    return missing


def table_text(cols, rows, limit=40):
    """A result table as compact text for the answering model."""
    def cell(v):
        return str(v).replace("\n", " ")[:80]
    lines = [" | ".join(cols)] + [" | ".join(cell(v) for v in r) for r in rows[:limit]]
    more = f"\n... {len(rows) - limit} more rows not shown" if len(rows) > limit else ""
    return f"{len(rows)} rows.\n" + "\n".join(lines) + more


def check():
    db().close()  # make sure tables and the all_songs view exist
    cols, rows = run_sql("select count(*) as n, embedding from items")
    assert cols == ["n"] and rows[0][0] >= 0, (cols, rows)
    for bad in ("delete from items", "update items set caption=''", "drop table songs"):
        try:
            run_sql(bad)
        except sqlite3.OperationalError as err:
            assert "readonly" in str(err), err
        else:
            raise AssertionError(f"write was allowed: {bad}")
    assert table_text(["a"], [[1], [2], [3]], limit=2).endswith("1 more rows not shown")
    src = "Keep your resume to one page and tailor it. Use LinkedIn outreach, 3 messages a day."
    assert unsupported("Tailor your resume and use LinkedIn outreach 3 times a day.", src) == []
    assert unsupported("Use ResumeAI to build it in 10 minutes.", src) == ["ResumeAI", "10"]
    fixed = fix_names("select 1 from all_songs where lower(artist) like '%arjit singh%' and type = 'reel'",
                      "which songs in my reels are by Arijit Singh?")
    assert "'%arijit singh%'" in fixed and "type = 'reel'" in fixed, fixed
    same = "select strftime('%Y', post_date) from items where lower(username) like '%nasa%'"
    assert fix_names(same, "how many per year from nasa") == same
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        check()
    else:
        p = plan(" ".join(sys.argv[1:]))
        print(json.dumps(p, indent=1))
        if p["action"] == "sql":
            cols, rows = run_sql(p["sql"])
            print(table_text(cols, rows, 10).encode("ascii", "replace").decode())
