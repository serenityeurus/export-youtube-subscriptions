# export-youtube-subscriptions

Export the channels you're subscribed to on YouTube into two Markdown files:

- `musicians.md`: music-artist channels
- `youtubers.md`: every other channel

Each channel gets one line: its URL, then a comment with the channel name.

```
https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx # Channel Name
```

You can run it again at any time. New subscriptions are added to the end of the right file. Channels you no longer follow, or that were deleted, get a marker. Nothing else in the files changes.

## Google Cloud setup (one time)

1. Open <https://console.cloud.google.com/> and create a project, or pick an existing one.
2. Go to **APIs & Services → Library**, find **YouTube Data API v3**, and click **Enable**.
3. Go to **APIs & Services → OAuth consent screen** (called "Google Auth Platform" in newer consoles):
   - User type: **External**. Fill in the app name and your email.
   - Scope: `https://www.googleapis.com/auth/youtube.readonly`.
   - While the app is in **Testing**, add your own Google account under **Test users**.
     Testing-mode refresh tokens expire after about 7 days. When that happens the tool runs the browser sign-in again. Publish the app to stop this.
4. Go to **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Download the JSON and save it as `client_secret.json` in the directory you run the tool from, or pass `--client-secrets PATH`.

Keep `client_secret.json` and the token private. `.gitignore` already excludes `client_secret*.json` and `token.json`.

## Usage

You need Python 3.9 or newer.

```sh
# Option A: uv. The script declares its dependencies inline (PEP 723).
./export-youtube-subscriptions --output-dir ~/notes/youtube

# Option B: plain pip
python3 -m pip install -r requirements.txt
python3 export-youtube-subscriptions --output-dir ~/notes/youtube
```

On the first run, a browser opens for Google sign-in. Use `--no-browser` on a headless host to have the URL printed instead. The token is cached at `~/.config/export-youtube-subscriptions/token.json` (or `$XDG_CONFIG_HOME/...`) with mode `600`. Use `--token PATH` to change the location.

| Option | Default | Meaning |
|---|---|---|
| `--output-dir DIR` | `.` | Where `musicians.md`, `youtubers.md` and `overrides.txt` live |
| `--client-secrets PATH` | `client_secret.json` | OAuth Desktop client JSON |
| `--token PATH` | `~/.config/export-youtube-subscriptions/token.json` | Cached OAuth token |
| `--no-browser` | off | Print the auth URL instead of opening a browser |
| `--dry-run` | off | Compute and print the summary without writing anything |
| `--allow-empty` | off | Accept an empty subscription list (see Safety) |
| `-v, --verbose` | off | List the channel IDs added, missing, restored or renamed |
| `--from-json PATH` | none | Read subscriptions from a JSON fixture instead of the API (for testing) |
| `--date YYYY-MM-DD` | today | Date to use in new markers (for testing) |

Every run prints a summary for each file, for example:

```
fetched 7 subscriptions
musicians.md: 5 channels, +2 added, 0 newly missing, 0 still missing, 0 restored, 0 renamed
youtubers.md: 3 channels, +0 added, 1 newly missing, 0 still missing, 0 restored, 0 renamed
```

Exit codes:

- `0`: success
- `1`: API, auth or network error. No files are touched.
- `2`: usage or config error, a bad `overrides.txt`, or the empty-list safety guard tripped.

The `--from-json` fixture is a JSON list of `{"id", "title", "topicIds"?, "topicCategories"?}`.

## File format and re-run semantics

- A **channel line** is `https://www.youtube.com/channel/<ID> # <name>`, optionally followed by ` [missing since YYYY-MM-DD]`. The parser also accepts hand-edited variants and rewrites them in the standard form:
  - `http://`, no `www.`, or `m.`
  - a trailing `/`
  - a `- ` list bullet
  - a name with no `#` before it
- Every other line is kept exactly as written: headers, blank lines, notes. New files start with a `# Musicians` or `# YouTubers` header.
- A channel already in **either** file stays in that file, on the same line. Lines are never reordered and never moved between files. To reclassify a channel, cut its line and paste it into the other file. The tool keeps it wherever you put it.
- New channels are added to the end of the file their category points to. They come in the order the API returns them, which is alphabetical.
- **Channels you still follow:** the name is updated from the API, so renames show up. If the line had a missing marker, the marker is removed.
- **Channels you no longer follow:** the stored name is kept and ` [missing since <first-seen-missing date>]` is added. Later runs keep the original date and never add a second marker.
- **Idempotent:** running twice with the same data gives byte-identical files. A file whose content didn't change is not rewritten, so its mtime is unchanged.
- Writes are atomic: the tool writes a temp file in the same directory, fsyncs it, then calls `os.replace`. File permissions are kept.
- If the same ID appears on more than one line (hand edits), every line is kept and updated, and a warning is printed.

## Classification

A **new** channel goes into `musicians.md` when any of these is true:

1. Its title ends with ` - Topic`. These are YouTube's auto-generated artist channels.
2. Its title contains `VEVO` (case-insensitive).
3. It has at least one **music** topic, and every other topic it has is a **weak** topic. Topics come from both `topicDetails.topicIds` (Freebase IDs) and `topicCategories` (Wikipedia URLs).
   - Music topics: Music `/m/04rlf` and its genre children (pop `/m/064t9`, rock `/m/06by7`, hip hop `/m/0glt670`, electronic `/m/02lkt`, jazz `/m/03_d0`, classical `/m/0ggq0m`, country `/m/01lyv`, R&B `/m/06j6l`, soul `/m/0gywn`, reggae `/m/06cqb`, independent `/m/05rwpb`, Christian `/m/02mscn`, music of Asia `/m/028sqc`, music of Latin America `/m/0g293`).
   - Weak topics: Entertainment `/m/02jjt` and Performing arts `/m/05qjc`. Musicians are often tagged with these too.
   - Any other topic is strong. Gaming, technology, film, humor, lifestyle and the rest mean the channel is about something else and only features music, so it goes in `youtubers.md`.

Everything else goes into `youtubers.md`, including channels with no topics at all.

Known weak spots: music reaction, music teaching and gear channels that are tagged only with Music would be classed as musicians. Use overrides for those.

### Overrides

Create `overrides.txt` in the output directory:

```
# CHANNEL_ID category  # optional comment
UCxxxxxxxxxxxxxxxxxxxxxx musician   # band without topic tags
UCyyyyyyyyyyyyyyyyyyyyyy youtuber   # music-theory teacher
```

An override wins over the heuristics when the tool **places a new** channel. It never moves a channel that is already in a file, because positions are fixed. If an override disagrees with where a channel currently is, the run prints a warning telling you to move the line by hand. A malformed line makes the run stop with exit code 2, so typos don't go unnoticed.

## Safety

If the API returns **zero** subscriptions while the files list channels, the run aborts with exit code 2 and doesn't mark anything missing. This protects against the wrong account, an expired token or an API glitch. If you really did unsubscribe from everything, pass `--allow-empty`. If the API or auth fails, the run aborts before any file is touched.

## Quota

The tool uses the YouTube Data API v3 with the `youtube.readonly` scope. It calls `subscriptions.list` (1 unit per page of 50) and `channels.list` with `part=snippet,topicDetails` (1 unit per batch of 50 IDs). 1,000 subscriptions cost about 40 units, well under the default free quota of 10,000 units per day.

## Development

```sh
make ci     # lint (py_compile, plus ruff via uvx when available), offline unit tests, CLI smoke test
make test   # python3 -m unittest discover -s tests -v
```

The tests use only the standard library and never touch the network. The Google client libraries are imported only when the tool actually authenticates or fetches. The YouTube API resource is injected, and a fake one is used in the tests. CI (`.forgejo/workflows/ci.yml`, image `node:22-bookworm`, which ships Python 3.11) runs `make ci` and needs no extra installs. Ruff is skipped there.

Layout:

- `export_youtube_subscriptions.py`: all the logic (parse, classify, merge, atomic write, fetch, CLI).
- `export-youtube-subscriptions`: the executable entry point, a uv script with inline dependencies.
- `tests/test_export.py`: the tests.

## License

MIT. See [LICENSE](LICENSE).
