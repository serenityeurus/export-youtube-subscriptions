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
from typing import Dict, Iterable, List, Optional, Sequence

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
_CHANNEL_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:https?://)?(?:www\.|m\.)?youtube\.com/channel/"
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
    line = f"{CHANNEL_URL_PREFIX}{channel_id} # {clean_name(name)}"
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
    """
    overrides = overrides or {}
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
            if p.id in seen:
                warnings.append(
                    f"duplicate channel {p.id} in {FILENAMES[cat]} "
                    f"(also in {FILENAMES[seen[p.id]]}); left in place"
                )
            else:
                seen[p.id] = cat
                s.total += 1
            ch = current.get(p.id)
            if ch is not None:
                name = clean_name(ch.title) or p.name
                if p.missing_since:
                    s.restored.append(p.id)
                if p.name and name != p.name:
                    s.renamed.append(p.id)
                out.append(format_line(p.id, name))
                want = overrides.get(p.id)
                if want and want != cat:
                    warnings.append(
                        f"override says {p.id} is a {want} but it is listed in "
                        f"{FILENAMES[cat]}; existing lines are never moved, move it by hand"
                    )
            else:
                since = p.missing_since or today
                (s.still_missing if p.missing_since else s.newly_missing).append(p.id)
                out.append(format_line(p.id, p.name, since))
        contents[cat] = out

    for ch in current.values():
        if ch.id in seen:
            continue
        cat = categorize(ch, overrides)
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
    permissions are preserved.
    """
    if os.path.exists(path) and read_text(path) == text:
        return False
    directory = os.path.dirname(os.path.abspath(path))
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return True


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


def default_token_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "export-youtube-subscriptions", "token.json")


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
    p.add_argument("--output-dir", default=".", help="directory for the .md files (default: cwd)")
    p.add_argument(
        "--client-secrets",
        default="client_secret.json",
        help="OAuth Desktop client JSON (default: client_secret.json)",
    )
    p.add_argument(
        "--token",
        default=None,
        help="cached OAuth token path (default: $XDG_CONFIG_HOME or ~/.config"
        "/export-youtube-subscriptions/token.json)",
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


def main(argv: Optional[Sequence[str]] = None, fetch=None) -> int:
    args = build_parser().parse_args(argv)
    today = args.date or _dt.date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        print(f"error: --date must be YYYY-MM-DD, got {today!r}", file=sys.stderr)
        return 2
    out_dir = args.output_dir
    if not os.path.isdir(out_dir):
        print(f"error: output dir {out_dir!r} does not exist", file=sys.stderr)
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
            channels = fetch_from_api(
                args.client_secrets, args.token or default_token_path(), not args.no_browser
            )
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

    if not args.dry_run:
        for cat in CATEGORIES:
            lines = result.contents[cat]
            path = os.path.join(out_dir, FILENAMES[cat])
            if not lines and not os.path.exists(path):
                continue  # don't create empty files
            atomic_write(path, join_lines(lines))

    print(f"fetched {len(channels)} subscriptions")
    print(format_summary(result, args.dry_run))
    if args.verbose:
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
