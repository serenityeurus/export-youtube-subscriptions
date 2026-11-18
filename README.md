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

On the first run, a browser opens for Google sign-in. The token is cached at `~/.config/export-youtube-subscriptions/token.json` (or `$XDG_CONFIG_HOME/...`) with mode `600`. Use `--token PATH` to change the location.

| Option | Default | Meaning |
|---|---|---|
| `--output-dir DIR` | `$EYS_OUTPUT_DIR`, else `.` | Where `musicians.md`, `youtubers.md` and `overrides.txt` live. Created if missing |
| `--client-secrets PATH` | `$EYS_CLIENT_SECRETS`, else `client_secret.json` | OAuth Desktop client JSON |
| `--token PATH` | `$EYS_TOKEN_PATH`, else `~/.config/export-youtube-subscriptions/token.json` | Cached OAuth token |
| `--env-file PATH` | see [Env file](#env-file) | Settings file to read |
| `--no-browser` | off | Print the auth URL instead of opening a browser |
| `--auth-port PORT` | `0` | Local port that receives Google's sign-in redirect. `0` picks a random free port. Set it on a headless host (see [Headless sign-in](#headless-sign-in)) |
| `--dry-run` | off | Compute and print the summary without writing anything |
| `--allow-empty` | off | Accept an empty subscription list (see Safety). Implies `--allow-drop` |
| `--allow-drop` | off | Let one run newly mark more than max(5, 20%) of the listed channels missing (see the drop guard under Exit codes) |
| `--apply-overrides` | off | Move channels that are already listed in the wrong file to the file `overrides.txt` names for them (see [Overrides](#overrides)). Without it, the run only warns |
| `-v, --verbose` | off | List the channel IDs added, missing, restored, renamed or moved |
| `--from-json PATH` | none | Read subscriptions from a JSON fixture instead of the API (for testing) |
| `--date YYYY-MM-DD` | today | Date to use in new markers (for testing) |

### Headless sign-in

After sign-in, Google redirects the browser to `http://localhost:<port>/`, where the tool is waiting. `--no-browser` only prints the URL instead of opening it; the tool still waits on that port on the machine where it runs. On a headless host the browser is on another machine, so fix the port and forward it over SSH:

```sh
# on your laptop: open an SSH session that forwards the port
ssh -L 8765:localhost:8765 <host>

# in that session, on the host
./export-youtube-subscriptions --no-browser --auth-port 8765
```

Open the printed URL in the laptop's browser and sign in. The redirect to `localhost:8765` goes through the tunnel to the tool. Keep the SSH session open until the run prints its summary. This is needed only when there is no usable cached token. Later runs (including from cron) refresh the token without a browser.

Every run prints which env file was loaded, the resolved output directory and where it came from, then a summary for each file, for example:

```
env file: /Users/jay/.config/export-youtube-subscriptions/export-youtube-subscriptions.env
output dir: /Users/jay/notes/pages (from EYS_OUTPUT_DIR in /Users/jay/.config/export-youtube-subscriptions/export-youtube-subscriptions.env)
fetched 7 subscriptions
musicians.md: 5 channels, +2 added, 0 newly missing, 0 still missing, 0 restored, 0 renamed
youtubers.md: 3 channels, +0 added, 1 newly missing, 0 still missing, 0 restored, 0 renamed
```

Exit codes:

- `0`: success.
- `1`: API, auth or network error (including a token refresh that fails on the network). No files are touched.
- `2`: the run refused or failed. Every case:
  - a bad option, or a `--date` that isn't a real date as `YYYY-MM-DD`;
  - an env file that is malformed, or that was named with `--env-file` or `$EYS_ENV_FILE` and doesn't exist;
  - an output dir that exists but isn't a directory, or can't be created;
  - an `overrides.txt` that is malformed or can't be read;
  - an existing `musicians.md` or `youtubers.md` can't be read (permissions, a directory, not UTF-8). This is checked before the fetch, so it costs no API quota and opens no sign-in;
  - a `--from-json` fixture that is missing, isn't JSON or has the wrong shape;
  - the Google client libraries are not installed (the message says how to install them);
  - missing or invalid OAuth client secrets (no file, or not a Desktop-app client JSON) when a sign-in is needed;
  - a safety guard tripped: the empty-list guard (see [Safety](#safety)) or the drop guard (below);
  - a file can't be written.

  In every case except the last, no files are touched. A write failure stops at the file that failed, but the other file may already have been replaced. Each file is replaced atomically, the pair is not, so the run can be left **half applied**. The error names the file that failed; fix the cause and rerun. With `--apply-overrides`, a moved line can end up in both files or, when moves go both ways, in neither. The error lists the lines to re-add by hand.

**Drop guard.** A partial API result would mark every channel it left out as missing. So a run refuses, before it writes anything, when it would newly mark more than max(5, 20%) of the listed channels missing. It prints the count and the threshold and exits with code 2:

```
error: 7 of 30 listed channel(s) would be newly marked missing, more than the drop threshold of 6 (the larger of 5 and 20%); the API result may be partial; refusing to write (pass --allow-drop if this is real)
```

- The 20% is taken of the channels that are listed in either file and not already marked missing. Channels that already have a marker count on neither side.
- The floor of 5 means a short list never blocks ordinary pruning, and a first run (nothing listed yet) can't trip the guard.
- `--allow-drop` skips the guard for that run. `--allow-empty` implies it.
- `--dry-run` doesn't refuse. It prints the usual summary (add `-v` for the channel IDs) plus a `warning:` line saying a real run would refuse, and exits 0.
- The threshold is the `DROP_THRESHOLD` constant in `export_youtube_subscriptions.py`.

The `--from-json` fixture is a JSON list of `{"id", "title", "topicIds"?, "topicCategories"?}`.

## Env file

Settings can live in `export-youtube-subscriptions.env`, so cron/launchd runs don't need flags. Start from the committed example; the real file is gitignored:

```sh
cp export-youtube-subscriptions.env.example export-youtube-subscriptions.env
# then edit: EYS_OUTPUT_DIR=~/notes/pages
./export-youtube-subscriptions
```

| Key | Default for | Built-in default |
|---|---|---|
| `EYS_OUTPUT_DIR` | `--output-dir` | current directory |
| `EYS_CLIENT_SECRETS` | `--client-secrets` | `client_secret.json` |
| `EYS_TOKEN_PATH` | `--token` | `~/.config/export-youtube-subscriptions/token.json` |

**Which file is read.** The first one found, and only that one:

1. `--env-file PATH`
2. `$EYS_ENV_FILE`
3. `export-youtube-subscriptions.env` in the directory of the executable, with symlinks resolved (a `~/bin` symlink finds the file next to the real script)
4. `~/.config/export-youtube-subscriptions/export-youtube-subscriptions.env` (`$XDG_CONFIG_HOME` is honoured)

If no file is found, that's fine and the built-in defaults apply. A file named with `--env-file` or `$EYS_ENV_FILE` must exist, so a typo fails with exit code 2 instead of silently writing to the wrong place.

**Precedence for each setting:** CLI flag > real environment variable (`EYS_OUTPUT_DIR=... ./export-youtube-subscriptions`) > env file > built-in default. An empty value counts as unset at every level.

**Syntax.** A small, stdlib-parsed subset of dotenv:

- `KEY=VALUE`, one per line. Blank lines and lines starting with `#` are ignored. A leading `export ` is allowed, so the file can also be `source`d.
- In an unquoted value, ` #` (whitespace, then `#`) starts a comment. `a#b` keeps its `#`.
- `"double quotes"` expand `~` and `$HOME`. `'single quotes'` are literal. Unquoted values expand `~` and `$HOME` too.
- Only `~`, `$HOME` and `${HOME}` are expanded. Other `$VARS` are not, so the meaning of the file doesn't depend on whatever environment cron happens to provide.
- Relative paths are resolved against the env file's directory, not the current directory.
- If a key appears twice, the last one wins. Unknown keys produce a warning, which catches typos like `EYS_OUTPUT_DIRS`. A malformed line stops the run with `file:line` and exit code 2.

The output directory is created with `mkdir -p` after the subscriptions are fetched successfully. It is never created in `--dry-run` (the summary says it would be) or when the fetch fails. `-v` also prints the resolved client-secrets and token paths and where each came from.

## File format and re-run semantics

- A **channel line** is `https://www.youtube.com/channel/<ID> # <name>`, optionally followed by ` [missing since YYYY-MM-DD]`. The parser also accepts hand-edited variants and rewrites them in the standard form:
  - `http://`, no `www.`, or `m.`
  - a trailing `/`
  - a `- ` list bullet
  - a name with no `#` before it
- Every other line is kept exactly as written: headers, blank lines, notes. New files start with a `# Musicians` or `# YouTubers` header.
- A channel already in **either** file stays in that file, on the same line. Lines are never reordered and never moved between files, unless you ask for it with `--apply-overrides` (see [Overrides](#overrides)). To reclassify a channel, cut its line and paste it into the other file. The tool keeps it wherever you put it.
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

An override wins over the heuristics when the tool **places a new** channel. It never moves a channel that is already in a file, because positions are fixed. If an override disagrees with where a channel currently is, the run prints a warning telling you to move the line by hand or pass `--apply-overrides`. A malformed line makes the run stop with exit code 2, so typos don't go unnoticed.

#### Moving existing lines: `--apply-overrides`

Pass `--apply-overrides` to have the tool do that move for you. It is opt-in and applies to that run only:

```sh
./export-youtube-subscriptions --apply-overrides --dry-run   # preview
./export-youtube-subscriptions --apply-overrides
```

- A channel whose override names the other file is taken out of the file it is in and appended to the **end** of the other one, after any channels added in the same run. If the target file doesn't exist yet, it is created with its header.
- The line moves as the run would have written it in place. A channel you no longer follow keeps its stored name and its `[missing since …]` marker with the original date. A channel you still follow gets its name refreshed, as on any run.
- Every other line in both files is left as it is. The gap the line leaves is closed; blank lines and notes around it stay.
- **Idempotent:** after the move the override and the file agree, so a second run with the flag changes nothing.
- With the flag, each summary line ends with `N moved in`: the number of channels moved **into** that file. A moved channel is counted in the file it moves to, in `channels` and in every other figure. `--dry-run` reports the same numbers and writes nothing. `-v` lists the IDs.

  ```
  musicians.md: 4 channels, +0 added, 0 newly missing, 0 still missing, 0 restored, 0 renamed, 0 moved in
  youtubers.md: 4 channels, +0 added, 0 newly missing, 1 still missing, 0 restored, 0 renamed, 1 moved in
  ```
- A move is not a drop. The drop guard and the empty-list guard count a moved channel once, exactly as if it had stayed where it was.
- If the same ID is on several lines (hand edits), every line that sits in the wrong file moves. The duplicate warning still prints.
- **One exception to "nothing is deleted":** if a moved line is byte-identical to a line the other file already has (as this run writes it), the moved copy is dropped instead of appended, and the run prints a warning saying so. A line that differs in any byte (another name, another `[missing since …]` date) is never deleted; both stay and the duplicate warning prints.
- **A failed write never loses a moved line.** Both files are fully written to temp files first, and only then put in place, so a disk-full or permission error while writing changes neither file. The two renames come last and are ordered so that every moved line is in at least one file at every point, even after a crash between them. When lines move both ways, this costs one extra rename of the first file. On a write failure the run exits with code 2, names the file that failed and prints every moved line in full, with the file it left and the file it joins, so a channel you no longer follow can be restored by hand if needed. Fix the cause and rerun with `--apply-overrides`: it finishes the move and drops the identical copies, leaving one copy of each line. If the rerun is on a later day, a channel that became unsubscribed in the failed run gets a different `[missing since …]` date, so both copies stay and the duplicate warning tells you to delete one.

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
- `export-youtube-subscriptions.env.example`: documented settings template (copy to `export-youtube-subscriptions.env`).
- `tests/test_export.py`: the tests.

## License

MIT. See [LICENSE](LICENSE).
