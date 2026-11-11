"""Give every saved item one or two topics from a fixed list, into table item_topics.

    python tag.py        # every item not tagged yet (safe to stop and rerun)
    python tag.py 10     # only the next 10
    python tag.py redo   # forget all tags and start again (after editing TOPICS)

The list is fixed so that counts add up: a free-form tagger writes "ai", "AI tools"
and "artificial intelligence" for the same thing. It was drawn up from a sample of
real saves; edit TOPICS in brain.py to suit your own, then run `python tag.py redo`.
"""
import json
import sys
import time

import brain
from sync import db

TOPICS = brain.TOPICS
SCHEMA = {
    "type": "object",
    "properties": {"topics": {"type": "array", "items": {"enum": TOPICS}, "minItems": 1, "maxItems": 2}},
    "required": ["topics"],
}
PROMPT = (
    "Choose the ONE topic that best describes what this saved Instagram item is about. "
    "Add a second topic only if the item is just as clearly about that too; most items have one. "
    "Topics: " + ", ".join(TOPICS) + ". "
    "'ai' is for AI tools and models; 'coding' for programming; 'tech' for other technology; "
    "'motivation' only for self-improvement or inspirational content. "
    "'other' means nothing fits and is never combined with another topic. Reply with JSON."
)


def topics_for(r):
    info = (f"Caption: {r['caption'][:300]}\nShown: {(r['visual'] or '')[:200]}\n"
            f"Said: {(r['transcript'] or '')[:300]}")
    picked = json.loads(brain.ask([{"role": "system", "content": PROMPT},
                                   {"role": "user", "content": info}], SCHEMA, 40))["topics"]
    picked = list(dict.fromkeys(t for t in picked if t in TOPICS))
    return [t for t in picked if t != "other"] or ["other"]  # "other" only when alone


def main(limit=None):
    con = db()
    if limit == "redo":
        con.execute("delete from item_topics")
        con.execute("update items set topics=null")
        con.commit()
        limit = None
    rows = con.execute(
        "select * from items where id not in (select item_id from item_topics) order by pos"
        + (f" limit {int(limit)}" if limit else "")
    ).fetchall()
    for n, r in enumerate(rows, 1):
        started = time.time()
        picked = topics_for(r)  # an Ollama error should stop the run, not be skipped
        con.executemany("insert into item_topics values(?, ?)", [(r["id"], t) for t in picked])
        con.execute("update items set topics=? where id=?", (", ".join(picked), r["id"]))
        con.commit()
        print(f"[{n}/{len(rows)}] {r['id']} {time.time() - started:.0f}s: {', '.join(picked)}"
              f" | {r['caption'][:50]!a}", flush=True)
    print(f"tagged {len(rows)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
