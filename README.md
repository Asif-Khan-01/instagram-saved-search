# Instagram saved search

Search your own saved Instagram reels, posts and songs by what they are about,
what is said in them and what is shown in them. Everything runs on your own
computer and is free: no cloud AI, no API keys.

Ask things like:

- "reels about AI tools from 2026"
- "list all my saved songs"
- "how many reels did I save per account?"
- "summarise the career advice I saved"

## How it works

| Script | What it does |
|---|---|
| `sync.py` | Reads your saved posts and saved-audio list through your browser login (via gallery-dl) into `saved.db` |
| `transcribe.py` | Transcribes speech in each reel with Whisper, translated to English |
| `visuals.py` | Describes four frames of each reel with a local vision model in Ollama |
| `listen.py` | Identifies background songs with Shazam (unofficial `shazamio` library) |
| `tag.py` | Sorts each item into one or two of a fixed list of topics (edit `TOPICS` in `brain.py`) |
| `brain.py` | Local model that turns a question into a topic search or a read-only database query, then writes the answer |
| `app.py` | The chat page (Streamlit) |
| `update.py` | Runs all the steps above for new saves |

## Setup

Built and tested on Windows 11 with an NVIDIA RTX 4050 (6 GB) and Python 3.13.

1. Install [ffmpeg](https://ffmpeg.org/) and [Ollama](https://ollama.com/), then:

   ```
   pip install -r requirements.txt
   ollama pull qwen3-vl:4b-instruct
   ```

2. Log in to Instagram in Firefox. (To use another browser, change `BROWSER` in `sync.py`.)

3. Fetch and process your saves, then open the page:

   ```
   python sync.py
   python update.py
   python -m streamlit run app.py
   ```

   The first run takes hours for a few hundred reels. Later, `python update.py`
   (or `Update saves.bat`) only handles new saves.

## Automatic updates (Windows)

To run the update every day at 9 PM with no window, register a scheduled task once
from PowerShell, with the path changed to where you put this folder:

```
Register-ScheduledTask -TaskName "Instagram saved search update" -Action (New-ScheduledTaskAction -Execute "wscript.exe" -Argument '"C:\path\to\update-hidden.vbs"') -Trigger (New-ScheduledTaskTrigger -Daily -At 21:00) -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

Output goes to `update.log`. A second update will not start while one is running.
Remove it with `Unregister-ScheduledTask -TaskName "Instagram saved search update"`.

## Removing saves you have unsaved

Saves you remove on Instagram stay in the tool until you run, by hand:

```
python sync.py prune          # lists what you have unsaved; changes nothing
python sync.py prune apply    # moves those items into table items_removed
```

This reads your whole saved list, the heaviest request the tool makes, so it is not part
of the nightly update. It refuses to change anything if the fetch had errors or returned
less than 90% of the items it knows, since a cut-short fetch looks like mass unsaving.
Archived items keep their transcripts and descriptions. Avoid running it while an update runs.

## Things to know

- This reads your own account with your own login, but automated access is against
  Instagram's terms and could get an account restricted. Use it at your own risk.
- Your data stays local: `saved.db`, `thumbs/`, `frames/` and `clips/` are git-ignored.
- Video links from a sync expire after about a day, so run `transcribe.py`,
  `visuals.py` and `listen.py` soon after syncing.
- The search model is English-only. Speech is translated to English; captions are not.
- Song identification sends audio fingerprints to Shazam.
- Each script has a `--check` self-test where it has logic worth testing
  (`sync.py --check`, `brain.py --check`).
