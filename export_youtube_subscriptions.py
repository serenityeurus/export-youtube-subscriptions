"""Export YouTube subscriptions to musicians.md / youtubers.md.

Core logic lives here and is importable with the standard library only; the
Google client libraries are imported lazily inside the fetch/auth functions so
the pure parts (parse, classify, merge, write) can be tested offline without
any third-party packages installed.

The executable entry point is the ``export-youtube-subscriptions`` script next
to this module.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

MUSICIAN = "musician"
YOUTUBER = "youtuber"
CATEGORIES = (MUSICIAN, YOUTUBER)
FILENAMES = {MUSICIAN: "musicians.md", YOUTUBER: "youtubers.md"}
HEADERS = {MUSICIAN: "# Musicians", YOUTUBER: "# YouTubers"}
OVERRIDES_FILENAME = "overrides.txt"

CHANNEL_URL_PREFIX = "https://www.youtube.com/channel/"

# A channel line: URL, optional "# comment". Tolerates a list bullet,
# http/no-www/m., a trailing slash and a missing "#" so hand-edited lines are
# still recognised (and never duplicated); they are re-rendered canonically.
# A leading UTF-8 BOM is tolerated (Windows editors) and scheme/host match
# case-insensitively; the channel id itself is case-sensitive and kept verbatim.
# Not recognised (treated as plain text, so the channel would be re-added):
# Markdown links, @handle / /c/ URLs, and extra path segments like /videos.
_CHANNEL_LINE_RE = re.compile(
    r"^\ufeff?\s*(?:[-*+]\s+)?(?:(?i:https?)://)?(?:(?i:www|m)\.)?(?i:youtube\.com/channel)/"
    r"(?P<id>[A-Za-z0-9_-]+)/?(?:(?:\s*#|\s)\s?(?P<comment>.*?))?\s*$"
)
_MISSING_RE = re.compile(r"\s*\[missing since (?P<date>\d{4}-\d{2}-\d{2})\]\s*$")
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------


@dataclass
class Channel:
    """A channel from the current subscription list."""

    id: str
    title: str
    topic_ids: List[str] = field(default_factory=list)
    topic_categories: List[str] = field(default_factory=list)


@dataclass
class ParsedLine:
    """A recognised channel line from an existing file."""

    id: str
    name: str
    missing_since: Optional[str]


@dataclass
class FileSummary:
    added: List[str] = field(default_factory=list)
    newly_missing: List[str] = field(default_factory=list)
    still_missing: List[str] = field(default_factory=list)
    restored: List[str] = field(default_factory=list)
    renamed: List[str] = field(default_factory=list)
    total: int = 0

    def counts(self) -> Dict[str, int]:
        return {
            "channels": self.total,
            "added": len(self.added),
            "newly_missing": len(self.newly_missing),
            "still_missing": len(self.still_missing),
            "restored": len(self.restored),
            "renamed": len(self.renamed),
        }


@dataclass
class MergeResult:
    contents: Dict[str, List[str]]  # category -> lines (no newline chars)
    summary: Dict[str, FileSummary]
    warnings: List[str]


class EmptySubscriptionsError(RuntimeError):
    """Raised when the API returned nothing but the files list channels."""


# --------------------------------------------------------------------------
# Line format
# --------------------------------------------------------------------------


def clean_name(name: str) -> str:
    """Collapse whitespace/newlines so a name can never break the one-line format."""
    return _WS_RE.sub(" ", name or "").strip()


def parse_line(line: str) -> Optional[ParsedLine]:
    """Parse a channel line; return None for headers, blanks and other text."""
    m = _CHANNEL_LINE_RE.match(line)
    if not m:
        return None
    comment = m.group("comment") or ""
    missing_since = None
    mm = _MISSING_RE.search(comment)
    if mm:
        missing_since = mm.group("date")
        comment = comment[: mm.start()]
    return ParsedLine(id=m.group("id"), name=comment.strip(), missing_since=missing_since)


def format_line(channel_id: str, name: str, missing_since: Optional[str] = None) -> str:
    name = clean_name(name)
    # No name -> bare URL, so we never emit "URL # " / "URL #  [missing ...]".
    line = f"{CHANNEL_URL_PREFIX}{channel_id}" + (f" # {name}" if name else "")
    if missing_since:
        line += f" [missing since {missing_since}]"
    return line


def split_lines(text: str) -> List[str]:
    """Split file text into lines, tolerant of CRLF and a missing final newline."""
    if not text:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def join_lines(lines: Sequence[str]) -> str:
    return "".join(f"{line}\n" for line in lines)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

# Freebase topic ids used by the YouTube Data API (see "topicDetails" docs).
MUSIC_TOPIC_IDS = frozenset(
    {
        "/m/04rlf",  # Music (parent)
        "/m/02mscn",  # Christian music
        "/m/0ggq0m",  # Classical music
        "/m/01lyv",  # Country
        "/m/02lkt",  # Electronic music
        "/m/0glt670",  # Hip hop music
        "/m/05rwpb",  # Independent music
        "/m/03_d0",  # Jazz
        "/m/028sqc",  # Music of Asia
        "/m/0g293",  # Music of Latin America
        "/m/064t9",  # Pop music
        "/m/06cqb",  # Reggae
        "/m/06j6l",  # Rhythm and blues
        "/m/06by7",  # Rock music
        "/m/0gywn",  # Soul music
    }
)
# Non-music topics that musicians routinely also get tagged with; they do not
# disqualify a channel that also carries a music topic.
WEAK_TOPIC_IDS = frozenset({"/m/02jjt", "/m/05qjc"})  # Entertainment, Performing arts

# topicCategories are Wikipedia URLs; map the article slug.
MUSIC_WIKI_SLUGS = frozenset(
    {
        "music",
        "christian_music",
        "classical_music",
        "country_music",
        "electronic_music",
        "hip_hop_music",
        "independent_music",
        "jazz",
        "music_of_asia",
        "music_of_latin_america",
        "pop_music",
        "reggae",
        "rhythm_and_blues",
        "rock_music",
        "soul_music",
    }
)
WEAK_WIKI_SLUGS = frozenset({"entertainment", "performing_arts"})


def _wiki_slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].lower()


def classify(channel: Channel) -> str:
    """Heuristic musician/youtuber classification (overrides are applied separately).

    Musician when any of:
      1. title ends with " - Topic" (YouTube auto-generated artist channel);
      2. title contains "VEVO" (case-insensitive);
      3. the channel has at least one music topic and every other topic is
         "weak" (Entertainment / Performing arts). Any strong non-music topic
         (gaming, technology, film, humor, lifestyle, ...) means the channel
         is about something else and merely features music -> youtuber.
    Otherwise youtuber (including channels with no topics at all).
    """
    title = (channel.title or "").strip()
    if title.endswith(" - Topic"):
        return MUSICIAN
    if "vevo" in title.lower():
        return MUSICIAN

    music = strong_other = False
    for tid in channel.topic_ids or ():
        if tid in MUSIC_TOPIC_IDS:
            music = True
        elif tid not in WEAK_TOPIC_IDS:
            strong_other = True
    for url in channel.topic_categories or ():
        slug = _wiki_slug(url)
        if slug in MUSIC_WIKI_SLUGS:
            music = True
        elif slug not in WEAK_WIKI_SLUGS:
            strong_other = True
    return MUSICIAN if music and not strong_other else YOUTUBER


def parse_overrides(text: str) -> Dict[str, str]:
    """Parse overrides.txt: ``UCxxx musician|youtuber  # optional comment``.

    Blank lines and ``#`` comment lines are ignored. Raises ValueError on a
    malformed line so typos are surfaced instead of silently ignored.
    """
    out: Dict[str, str] = {}
    for n, raw in enumerate(split_lines(text), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or parts[1].lower() not in CATEGORIES:
            raise ValueError(
                f"{OVERRIDES_FILENAME}:{n}: expected 'CHANNEL_ID musician|youtuber', got {raw!r}"
            )
        out[parts[0]] = parts[1].lower()
    return out


def categorize(channel: Channel, overrides: Dict[str, str]) -> str:
    return overrides.get(channel.id) or classify(channel)


_default_categorize = categorize  # merge()'s ``categorize`` parameter shadows the name


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def count_channels(existing: Dict[str, List[str]]) -> int:
    return len({p.id for lines in existing.values() for p in map(parse_line, lines) if p})


def merge(
    existing: Dict[str, List[str]],
    channels: Sequence[Channel],
    today: str,
    overrides: Optional[Dict[str, str]] = None,
    allow_empty: bool = False,
    categorize: Optional[Callable[[Channel], str]] = None,
) -> MergeResult:
    """Merge the current subscription list into the existing file contents.

    ``existing`` maps category -> list of lines (missing file == empty list).
    Pure: no I/O. Rules:
      * Non-channel lines (headers, blanks, notes) are preserved verbatim.
      * A channel already present in either file stays on its line in that file.
      * Present channels get their name refreshed from the API and any missing
        marker removed (restored).
      * Absent channels keep their stored name and get
        ``[missing since <today>]``; an existing marker keeps its original date.
      * New channels are appended to the end of their category's file,
        in subscription-list order.
      * Output for an unchanged input is byte-identical (idempotent).
      * ``categorize(channel) -> MUSICIAN|YOUTUBER`` picks the file for *new*
        channels only (default: overrides, then the heuristic). It is never
        consulted for channels already in a file.
      * A channel id on several lines (hand edits): every line is updated and
        a warning emitted; summary stats count only the first occurrence.
    """
    overrides = overrides or {}
    if categorize is None:
        categorize = lambda c: _default_categorize(c, overrides)  # noqa: E731
    if not channels and not allow_empty:
        n = count_channels(existing)
        if n:
            raise EmptySubscriptionsError(
                f"API returned 0 subscriptions but the files list {n} channel(s); "
                "refusing to mark them all missing (pass --allow-empty if this is real)"
            )

    current: Dict[str, Channel] = {}
    for ch in channels:
        current.setdefault(ch.id, ch)  # de-dupe defensively, first wins

    warnings: List[str] = []
    summary = {cat: FileSummary() for cat in CATEGORIES}
    contents: Dict[str, List[str]] = {}
    seen: Dict[str, str] = {}  # channel id -> category where first seen

    for cat in CATEGORIES:
        out: List[str] = []
        s = summary[cat]
        for line in existing.get(cat, []):
            p = parse_line(line)
            if p is None:
                out.append(line)
                continue
            first = p.id not in seen
            if first:
                seen[p.id] = cat
                s.total += 1
            else:
                warnings.append(
                    f"duplicate channel {p.id} in {FILENAMES[cat]} "
                    f"(also in {FILENAMES[seen[p.id]]}); left in place"
                )
            # Duplicates are still rewritten, but stats come from the first line
            # only so per-file totals and deltas stay consistent.
            stats = s if first else FileSummary()
            ch = current.get(p.id)
            if ch is not None:
                name = clean_name(ch.title) or p.name
                if p.missing_since:
                    stats.restored.append(p.id)
                if p.name and name != p.name:
                    stats.renamed.append(p.id)
                out.append(format_line(p.id, name))
                want = overrides.get(p.id)
                if want and want != cat:
                    warnings.append(
                        f"override says {p.id} is a {want} but it is listed in "
                        f"{FILENAMES[cat]}; existing lines are never moved, move it by hand"
                    )
            else:
                since = p.missing_since or today
                (stats.still_missing if p.missing_since else stats.newly_missing).append(p.id)
                out.append(format_line(p.id, p.name, since))
        contents[cat] = out

    for ch in current.values():
        if ch.id in seen:
            continue
        cat = categorize(ch)
        if cat not in CATEGORIES:
            raise ValueError(f"categorize({ch.id!r}) returned {cat!r}; expected one of {CATEGORIES}")
        lines = contents[cat]
        if not lines:
            lines.extend([HEADERS[cat], ""])  # brand-new file gets a header
        lines.append(format_line(ch.id, ch.title))
        summary[cat].added.append(ch.id)
        summary[cat].total += 1
        seen[ch.id] = cat

    # A duplicated id (hand edit) must only be counted once per file.
    for s in summary.values():
        s.restored = list(dict.fromkeys(s.restored))
        s.renamed = list(dict.fromkeys(s.renamed))
        s.newly_missing = list(dict.fromkeys(s.newly_missing))
        s.still_missing = list(dict.fromkeys(s.still_missing))
    return MergeResult(contents=contents, summary=summary, warnings=warnings)


# --------------------------------------------------------------------------
# File I/O
# --------------------------------------------------------------------------


def read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def atomic_write(path: str, text: str) -> bool:
    """Write ``text`` to ``path`` atomically. Returns False if content was unchanged.

    Temp file in the same directory + fsync + os.replace, so a crash leaves
    either the old or the new file, never a truncated one. Existing file
    permissions are preserved; owner/group and extended attributes are not.
    """
    # Write to the symlink target so a linked file (e.g. into a notes repo or
    # dotfiles) stays a link instead of being replaced by a regular file.
    path = os.path.realpath(path)
    if os.path.exists(path) and read_text(path) == text:
        return False
    directory = os.path.dirname(path)
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        umask = os.umask(0)  # mkstemp is 0600; new files get the usual 0666 & ~umask
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return True


def _fsync_dir(directory: str) -> None:
    """Best effort: persist the rename itself (POSIX); a no-op where unsupported."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def load_existing(output_dir: str) -> Dict[str, List[str]]:
    return {cat: split_lines(read_text(os.path.join(output_dir, FILENAMES[cat]))) for cat in CATEGORIES}


def load_overrides(output_dir: str) -> Dict[str, str]:
    return parse_overrides(read_text(os.path.join(output_dir, OVERRIDES_FILENAME)))


# --------------------------------------------------------------------------
# Subscription sources
# --------------------------------------------------------------------------


def _chunks(items: Sequence[str], n: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(items), n):
        yield items[i : i + n]


def fetch_channels(youtube) -> List[Channel]:
    """Fetch subscribed channels via an injected googleapiclient ``youtube`` resource.

    subscriptions.list (1 quota unit / page of 50) then channels.list in
    batches of 50 for topicDetails (1 unit / batch).
    """
    subs: List[Channel] = []
    seen = set()
    page_token = None
    while True:
        kwargs = dict(part="snippet", mine=True, maxResults=50, order="alphabetical")
        if page_token:
            kwargs["pageToken"] = page_token
        resp = youtube.subscriptions().list(**kwargs).execute()
        for item in resp.get("items", []):
            snip = item.get("snippet", {})
            cid = snip.get("resourceId", {}).get("channelId")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            subs.append(Channel(id=cid, title=clean_name(snip.get("title", ""))))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    by_id = {c.id: c for c in subs}
    for batch in _chunks([c.id for c in subs], 50):
        resp = (
            youtube.channels()
            .list(part="snippet,topicDetails", id=",".join(batch), maxResults=50)
            .execute()
        )
        for item in resp.get("items", []):
            ch = by_id.get(item.get("id"))
            if ch is None:
                continue
            title = clean_name(item.get("snippet", {}).get("title", ""))
            if title:
                ch.title = title
            td = item.get("topicDetails", {}) or {}
            ch.topic_ids = list(td.get("topicIds", []) or [])
            ch.topic_categories = list(td.get("topicCategories", []) or [])
    return subs


def load_json_channels(path: str) -> List[Channel]:
    """Load a fixture: JSON list of {id, title, topicIds?, topicCategories?}."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return [
        Channel(
            id=d["id"],
            title=clean_name(d.get("title", "")),
            topic_ids=list(d.get("topicIds", [])),
            topic_categories=list(d.get("topicCategories", [])),
        )
        for d in data
    ]


def default_token_path(environ=None) -> str:
    return os.path.join(config_dir(environ), "token.json")


def _save_token(path: str, creds) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    os.chmod(path, 0o600)  # in case the file pre-existed with looser perms


def get_credentials(client_secrets: str, token_path: str, open_browser: bool = True):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(token_path, creds)
            return creds
        except RefreshError as e:
            print(f"token refresh failed ({e}); re-authorising", file=sys.stderr)
    if not os.path.exists(client_secrets):
        raise FileNotFoundError(
            f"OAuth client secrets not found at {client_secrets!r}; see README "
            "'Google Cloud setup' or pass --client-secrets"
        )
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser)
    _save_token(token_path, creds)
    return creds


def fetch_from_api(client_secrets: str, token_path: str, open_browser: bool = True) -> List[Channel]:
    from googleapiclient.discovery import build

    creds = get_credentials(client_secrets, token_path, open_browser)
    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    return fetch_channels(youtube)


# --------------------------------------------------------------------------
# Env file (export-youtube-subscriptions.env)
# --------------------------------------------------------------------------

ENV_FILENAME = "export-youtube-subscriptions.env"
ENV_FILE_VAR = "EYS_ENV_FILE"
ENV_OUTPUT_DIR = "EYS_OUTPUT_DIR"
ENV_CLIENT_SECRETS = "EYS_CLIENT_SECRETS"
ENV_TOKEN_PATH = "EYS_TOKEN_PATH"
ENV_KEYS = (ENV_OUTPUT_DIR, ENV_CLIENT_SECRETS, ENV_TOKEN_PATH)
# Keys whose values are paths: relative values in an env file are resolved
# against the env file's own directory (not the cwd), so the file behaves the
# same from cron, launchd or an interactive shell.
ENV_PATH_KEYS = frozenset(ENV_KEYS)

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HOME_VAR_RE = re.compile(r"\$(?:HOME\b|\{HOME\})")


class EnvFileError(ValueError):
    """A malformed env file, or an explicitly requested one that is missing."""


def _home(environ) -> str:
    return (environ or {}).get("HOME") or os.path.expanduser("~")


def expand_home(value: str, environ=None) -> str:
    """Expand a leading ``~`` / ``~/`` and every ``$HOME`` / ``${HOME}``.

    Deliberately only HOME: arbitrary ``$VAR`` interpolation would make the
    file's meaning depend on whatever environment cron/launchd happens to give
    it. ``~user`` is left alone.
    """
    home = _home(environ)
    if value == "~" or value.startswith("~/"):
        value = home + value[1:]
    return _HOME_VAR_RE.sub(lambda _m: home, value)


def parse_env_file(text: str, environ=None, source: str = ENV_FILENAME) -> Dict[str, str]:
    """Parse ``KEY=VALUE`` lines (stdlib only; a small subset of dotenv).

    * blank lines and lines starting with ``#`` are ignored; an optional
      leading ``export `` is accepted so the file can also be ``source``d;
    * unquoted values are stripped and a `` #`` (whitespace + hash) starts an
      inline comment, so ``a#b`` keeps its hash;
    * ``"double"`` quoted values get ``~``/``$HOME`` expansion, ``'single'``
      quoted values are literal; text after the closing quote must be empty or
      a comment;
    * the last occurrence of a key wins.
    Raises EnvFileError (with ``source:line``) on a malformed line.
    """
    out: Dict[str, str] = {}
    for n, raw in enumerate(split_lines(text), 1):
        line = raw.strip()
        if n == 1:
            line = line.lstrip("﻿")  # tolerate a UTF-8 BOM
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _ENV_KEY_RE.match(key):
            raise EnvFileError(f"{source}:{n}: expected KEY=VALUE, got {raw!r}")
        value = value.strip()
        if value[:1] in ("'", '"'):
            q = value[0]
            end = value.find(q, 1)
            rest = value[end + 1 :].strip() if end != -1 else ""
            if end == -1 or (rest and not rest.startswith("#")):
                raise EnvFileError(f"{source}:{n}: unterminated or malformed {q}quoted{q} value: {raw!r}")
            value = value[1:end]
            if q == '"':
                value = expand_home(value, environ)
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
            value = expand_home(value, environ)
        out[key] = value
    return out


def config_dir(environ=None) -> str:
    environ = environ if environ is not None else os.environ
    base = environ.get("XDG_CONFIG_HOME") or os.path.join(_home(environ), ".config")
    return os.path.join(base, "export-youtube-subscriptions")


def default_script_dir() -> str:
    """Directory of the executable, symlinks resolved (the module lives beside it)."""
    return os.path.dirname(os.path.realpath(__file__))


def find_env_file(
    cli_path: Optional[str], environ=None, script_dir: Optional[str] = None
) -> Optional[str]:
    """Locate the env file. Order: ``--env-file`` > ``$EYS_ENV_FILE`` >
    ``<executable dir>/export-youtube-subscriptions.env`` >
    ``~/.config/export-youtube-subscriptions/export-youtube-subscriptions.env``
    (``$XDG_CONFIG_HOME`` honoured, same as the token default).

    An explicitly named file (flag or env var) must exist: EnvFileError
    otherwise, so a typo can't silently fall back to defaults. Implicit
    locations are optional; None when nothing is found. An empty
    ``EYS_ENV_FILE`` counts as unset.
    """
    environ = environ if environ is not None else os.environ
    for explicit, label in ((cli_path, "--env-file"), (environ.get(ENV_FILE_VAR), f"${ENV_FILE_VAR}")):
        if explicit:
            path = os.path.abspath(expand_home(explicit, environ))
            if not os.path.isfile(path):
                raise EnvFileError(f"env file {path!r} (from {label}) not found")
            return path
    script_dir = script_dir if script_dir is not None else default_script_dir()
    for cand in (os.path.join(script_dir, ENV_FILENAME), os.path.join(config_dir(environ), ENV_FILENAME)):
        if os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def load_env_file(path: Optional[str], environ=None) -> Dict[str, str]:
    """Read + parse ``path`` (None -> {}); relative path values are anchored
    to the env file's directory."""
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise EnvFileError(f"cannot read env file {path!r}: {e}") from e
    values = parse_env_file(text, environ, source=path)
    base = os.path.dirname(path)
    for key in ENV_PATH_KEYS:
        v = values.get(key)
        if v and not os.path.isabs(v):
            values[key] = os.path.normpath(os.path.join(base, v))
    return values


def unknown_env_keys(values: Dict[str, str]) -> List[str]:
    """Keys in the file this tool doesn't read (likely typos)."""
    return sorted(k for k in values if k not in ENV_KEYS)


@dataclass
class Settings:
    output_dir: str
    client_secrets: str
    token_path: str
    env_file: Optional[str]
    sources: Dict[str, str]  # setting name -> where its value came from
    unknown_keys: List[str] = field(default_factory=list)  # env-file keys we don't read


def resolve_settings(args, environ=None, script_dir: Optional[str] = None) -> Settings:
    """Precedence per setting: CLI flag > process env var > env file > built-in default.

    Empty values (``EYS_OUTPUT_DIR=``) count as unset at every layer.
    """
    environ = environ if environ is not None else os.environ
    env_file = find_env_file(getattr(args, "env_file", None), environ, script_dir)
    file_values = load_env_file(env_file, environ)

    sources: Dict[str, str] = {}

    def pick(name: str, flag_value: Optional[str], key: str, default: str) -> str:
        # File values were already expanded/anchored by the parser (and a
        # 'single-quoted' value must stay literal), so only the flag and the
        # process env get ~/$HOME expansion here.
        for value, src, expand in (
            (flag_value, f"--{name}", True),
            (environ.get(key), f"${key}", True),
            (file_values.get(key), f"{key} in {env_file}", False),
        ):
            if value:
                sources[name] = src
                return expand_home(value, environ) if expand else value
        sources[name] = "default"
        return default

    return Settings(
        output_dir=pick("output-dir", args.output_dir, ENV_OUTPUT_DIR, "."),
        client_secrets=pick("client-secrets", args.client_secrets, ENV_CLIENT_SECRETS, "client_secret.json"),
        token_path=pick("token", args.token, ENV_TOKEN_PATH, default_token_path(environ)),
        env_file=env_file,
        sources=sources,
        unknown_keys=unknown_env_keys(file_values),
    )



# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def format_summary(result: MergeResult, dry_run: bool) -> str:
    out = []
    for cat in CATEGORIES:
        s = result.summary[cat]
        out.append(
            f"{FILENAMES[cat]}: {s.total} channels, +{len(s.added)} added, "
            f"{len(s.newly_missing)} newly missing, {len(s.still_missing)} still missing, "
            f"{len(s.restored)} restored, {len(s.renamed)} renamed"
        )
    for w in result.warnings:
        out.append(f"warning: {w}")
    if dry_run:
        out.append("dry run: no files written")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="export-youtube-subscriptions",
        description="Export your YouTube subscriptions to musicians.md and youtubers.md.",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help=f"directory for the .md files; created if missing (default: ${ENV_OUTPUT_DIR}, else cwd)",
    )
    p.add_argument(
        "--client-secrets",
        default=None,
        help=f"OAuth Desktop client JSON (default: ${ENV_CLIENT_SECRETS}, else client_secret.json)",
    )
    p.add_argument(
        "--token",
        default=None,
        help=f"cached OAuth token path (default: ${ENV_TOKEN_PATH}, else $XDG_CONFIG_HOME or ~/.config"
        "/export-youtube-subscriptions/token.json)",
    )
    p.add_argument(
        "--env-file",
        metavar="PATH",
        default=None,
        help=f"env file with EYS_* settings (default: ${ENV_FILE_VAR}, else {ENV_FILENAME} next to the "
        "executable, else ~/.config/export-youtube-subscriptions/; must exist when given explicitly)",
    )
    p.add_argument("--no-browser", action="store_true", help="print the auth URL instead of opening a browser")
    p.add_argument("--dry-run", action="store_true", help="print the summary without writing files")
    p.add_argument(
        "--allow-empty",
        action="store_true",
        help="allow an empty subscription list to mark every listed channel missing",
    )
    p.add_argument(
        "--from-json",
        metavar="PATH",
        help="read subscriptions from a JSON fixture instead of the API (testing)",
    )
    p.add_argument("--date", help="override today's date YYYY-MM-DD for markers (testing)")
    p.add_argument("-v", "--verbose", action="store_true", help="list affected channel ids")
    return p


def main(
    argv: Optional[Sequence[str]] = None,
    fetch=None,
    environ=None,
    script_dir: Optional[str] = None,
) -> int:
    """CLI entry. ``environ``/``script_dir`` are injectable so tests never read
    the developer's real env file or environment."""
    args = build_parser().parse_args(argv)
    today = args.date or _dt.date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        print(f"error: --date must be YYYY-MM-DD, got {today!r}", file=sys.stderr)
        return 2
    try:
        settings = resolve_settings(args, environ, script_dir)
    except EnvFileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if settings.unknown_keys:
        keys = ", ".join(settings.unknown_keys)
        print(f"warning: {settings.env_file}: ignoring unknown key(s): {keys}", file=sys.stderr)
    out_dir = settings.output_dir
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        print(f"error: output dir {out_dir!r} exists and is not a directory", file=sys.stderr)
        return 2

    try:
        overrides = load_overrides(out_dir)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        if fetch is not None:
            channels = fetch()
        elif args.from_json:
            channels = load_json_channels(args.from_json)
        else:
            channels = fetch_from_api(settings.client_secrets, settings.token_path, not args.no_browser)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # API/auth/network errors: report, don't touch files
        print(f"error: failed to fetch subscriptions: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    existing = load_existing(out_dir)
    try:
        result = merge(existing, channels, today, overrides, allow_empty=args.allow_empty)
    except EmptySubscriptionsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    created_dir = False
    if not args.dry_run:
        # Created only after a successful fetch + merge, so an auth/API failure
        # never leaves an empty directory behind.
        if not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir, exist_ok=True)
            except OSError as e:
                print(f"error: cannot create output dir {out_dir!r}: {e}", file=sys.stderr)
                return 2
            created_dir = True
        for cat in CATEGORIES:
            lines = result.contents[cat]
            path = os.path.join(out_dir, FILENAMES[cat])
            if not lines and not os.path.exists(path):
                continue  # don't create empty files
            atomic_write(path, join_lines(lines))

    print(f"env file: {settings.env_file or 'none'}")
    note = ""
    if created_dir:
        note = ", created"
    elif args.dry_run and not os.path.isdir(out_dir):
        note = ", does not exist yet; would be created"
    print(f"output dir: {os.path.abspath(out_dir)} (from {settings.sources['output-dir']}{note})")
    print(f"fetched {len(channels)} subscriptions")
    print(format_summary(result, args.dry_run))
    if args.verbose:
        print(f"  client secrets: {settings.client_secrets} (from {settings.sources['client-secrets']})")
        print(f"  token: {settings.token_path} (from {settings.sources['token']})")
        for cat in CATEGORIES:
            s = result.summary[cat]
            for label, ids in (
                ("added", s.added),
                ("newly missing", s.newly_missing),
                ("restored", s.restored),
                ("renamed", s.renamed),
            ):
                for cid in ids:
                    print(f"  {FILENAMES[cat]} {label}: {cid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
