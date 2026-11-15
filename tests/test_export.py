"""Offline tests: no network, no Google libraries required."""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_youtube_subscriptions as eys  # noqa: E402
from export_youtube_subscriptions import (  # noqa: E402
    MUSICIAN,
    YOUTUBER,
    Channel,
    EmptySubscriptionsError,
    classify,
    format_line,
    merge,
    parse_line,
    parse_overrides,
)

URL = "https://www.youtube.com/channel/"
TODAY = "2026-11-15"
LATER = "2026-12-01"


def ch(cid, title, topic_ids=(), cats=()):
    return Channel(cid, title, list(topic_ids), list(cats))


class ParseLineTests(unittest.TestCase):
    def test_canonical(self):
        p = parse_line(f"{URL}UCabc_-1 # Some Name")
        self.assertEqual((p.id, p.name, p.missing_since), ("UCabc_-1", "Some Name", None))

    def test_marker(self):
        p = parse_line(f"{URL}UCa # Old Name [missing since 2026-01-02]")
        self.assertEqual((p.name, p.missing_since), ("Old Name", "2026-01-02"))

    def test_name_with_hash(self):
        self.assertEqual(parse_line(f"{URL}UCa # C# Tutorials #1").name, "C# Tutorials #1")

    def test_tolerant_variants(self):
        for line in (
            "http://youtube.com/channel/UCa # N",
            "- https://www.youtube.com/channel/UCa # N",
            f"{URL}UCa/ # N",
            f"{URL}UCa N",
            f"  {URL}UCa#N  ",
        ):
            p = parse_line(line)
            self.assertIsNotNone(p, line)
            self.assertEqual((p.id, p.name), ("UCa", "N"), line)

    def test_no_comment(self):
        self.assertEqual(parse_line(f"{URL}UCa").name, "")

    def test_non_channel(self):
        for line in ("", "# Musicians", "notes", "https://www.youtube.com/@handle # x",
                     "https://www.youtube.com/watch?v=abc"):
            self.assertIsNone(parse_line(line), line)

    def test_format_roundtrip_and_newline_safety(self):
        line = format_line("UCa", "Multi\nline\tname  ", "2026-01-01")
        self.assertEqual(line, f"{URL}UCa # Multi line name [missing since 2026-01-01]")
        p = parse_line(line)
        self.assertEqual((p.name, p.missing_since), ("Multi line name", "2026-01-01"))

    def test_split_lines(self):
        self.assertEqual(eys.split_lines(""), [])
        self.assertEqual(eys.split_lines("a\r\nb"), ["a", "b"])
        self.assertEqual(eys.split_lines("a\n\n"), ["a", ""])


class ClassifyTests(unittest.TestCase):
    def test_topic_suffix(self):
        self.assertEqual(classify(ch("UC1", "Daft Punk - Topic")), MUSICIAN)

    def test_topic_word_not_suffix(self):
        self.assertEqual(classify(ch("UC1", "Topic Talks")), YOUTUBER)

    def test_vevo(self):
        self.assertEqual(classify(ch("UC1", "TaylorSwiftVEVO")), MUSICIAN)
        self.assertEqual(classify(ch("UC1", "Vevo Pop")), MUSICIAN)

    def test_music_topic_ids(self):
        self.assertEqual(classify(ch("UC1", "Band", ["/m/04rlf", "/m/06by7"])), MUSICIAN)
        self.assertEqual(classify(ch("UC1", "Band", ["/m/064t9", "/m/02jjt"])), MUSICIAN)

    def test_music_topic_categories(self):
        cats = ["https://en.wikipedia.org/wiki/Music", "https://en.wikipedia.org/wiki/Hip_hop_music"]
        self.assertEqual(classify(ch("UC1", "Rapper", cats=cats)), MUSICIAN)

    def test_strong_other_topic_wins(self):
        # Music + video games -> gaming channel that features music.
        self.assertEqual(classify(ch("UC1", "Gamer", ["/m/04rlf", "/m/0bzvm2"])), YOUTUBER)
        cats = ["https://en.wikipedia.org/wiki/Music", "https://en.wikipedia.org/wiki/Technology"]
        self.assertEqual(classify(ch("UC1", "Gear reviews", cats=cats)), YOUTUBER)

    def test_entertainment_only(self):
        self.assertEqual(classify(ch("UC1", "Vlogger", ["/m/02jjt"])), YOUTUBER)

    def test_no_topics(self):
        self.assertEqual(classify(ch("UC1", "Anything")), YOUTUBER)
        self.assertEqual(classify(ch("UC1", "")), YOUTUBER)


class OverridesTests(unittest.TestCase):
    def test_parse(self):
        text = "# header\n\nUCa musician  # actually a band\nUCb YouTuber\n"
        self.assertEqual(parse_overrides(text), {"UCa": MUSICIAN, "UCb": YOUTUBER})

    def test_malformed(self):
        with self.assertRaises(ValueError):
            parse_overrides("UCa singer\n")
        with self.assertRaises(ValueError):
            parse_overrides("UCa\n")

    def test_override_wins_for_new_channel(self):
        r = merge({}, [ch("UCa", "X - Topic")], TODAY, {"UCa": YOUTUBER})
        self.assertIn(f"{URL}UCa # X - Topic", r.contents[YOUTUBER])
        self.assertEqual(r.contents[MUSICIAN], [])

    def test_override_never_moves_existing(self):
        existing = {YOUTUBER: [f"{URL}UCa # Band"]}
        r = merge(existing, [ch("UCa", "Band")], TODAY, {"UCa": MUSICIAN})
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # Band"])
        self.assertEqual(r.contents[MUSICIAN], [])
        self.assertTrue(any("move it by hand" in w for w in r.warnings))


class MergeTests(unittest.TestCase):
    def test_first_run_creates_headers_and_order(self):
        subs = [ch("UCm", "Artist - Topic"), ch("UCy1", "Vlog"), ch("UCy2", "Tech")]
        r = merge({}, subs, TODAY)
        self.assertEqual(r.contents[MUSICIAN], ["# Musicians", "", f"{URL}UCm # Artist - Topic"])
        self.assertEqual(
            r.contents[YOUTUBER], ["# YouTubers", "", f"{URL}UCy1 # Vlog", f"{URL}UCy2 # Tech"]
        )
        self.assertEqual(r.summary[YOUTUBER].added, ["UCy1", "UCy2"])

    def test_append_new_preserves_positions_and_other_lines(self):
        existing = {YOUTUBER: ["My list", f"{URL}UCb # B", "", "<!-- note -->", f"{URL}UCa # A"]}
        r = merge(existing, [ch("UCa", "A"), ch("UCb", "B"), ch("UCc", "C")], TODAY)
        self.assertEqual(
            r.contents[YOUTUBER],
            ["My list", f"{URL}UCb # B", "", "<!-- note -->", f"{URL}UCa # A", f"{URL}UCc # C"],
        )

    def test_existing_file_without_header_gets_no_header(self):
        r = merge({YOUTUBER: [f"{URL}UCa # A"]}, [ch("UCa", "A"), ch("UCb", "B")], TODAY)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # A", f"{URL}UCb # B"])

    def test_missing_marker_keeps_stored_name(self):
        existing = {YOUTUBER: [f"{URL}UCa # Old", f"{URL}UCb # B"]}
        r = merge(existing, [ch("UCb", "B")], TODAY)
        self.assertEqual(r.contents[YOUTUBER][0], f"{URL}UCa # Old [missing since {TODAY}]")
        self.assertEqual(r.summary[YOUTUBER].newly_missing, ["UCa"])

    def test_marker_not_stacked_and_original_date_kept(self):
        existing = {YOUTUBER: [f"{URL}UCa # Old [missing since 2026-01-01]", f"{URL}UCb # B"]}
        r = merge(existing, [ch("UCb", "B")], LATER)
        self.assertEqual(r.contents[YOUTUBER][0], f"{URL}UCa # Old [missing since 2026-01-01]")
        self.assertEqual(r.summary[YOUTUBER].still_missing, ["UCa"])
        self.assertEqual(r.summary[YOUTUBER].newly_missing, [])

    def test_restore_removes_marker_and_refreshes_name(self):
        existing = {MUSICIAN: [f"{URL}UCa # Old [missing since 2026-01-01]"]}
        r = merge(existing, [ch("UCa", "New Name")], LATER)
        self.assertEqual(r.contents[MUSICIAN], [f"{URL}UCa # New Name"])
        self.assertEqual(r.summary[MUSICIAN].restored, ["UCa"])
        self.assertEqual(r.summary[MUSICIAN].renamed, ["UCa"])

    def test_present_name_refreshed_but_empty_api_title_keeps_stored(self):
        existing = {YOUTUBER: [f"{URL}UCa # Old", f"{URL}UCb # Keep"]}
        r = merge(existing, [ch("UCa", "Renamed"), ch("UCb", "")], TODAY)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # Renamed", f"{URL}UCb # Keep"])

    def test_no_moving_between_files(self):
        # Heuristic now says musician, but the channel lives in youtubers.md.
        existing = {YOUTUBER: [f"{URL}UCa # Band"]}
        r = merge(existing, [ch("UCa", "Band - Topic")], TODAY)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # Band - Topic"])
        self.assertEqual(r.contents[MUSICIAN], [])

    def test_no_duplicates_and_dedupe_input(self):
        existing = {MUSICIAN: [f"{URL}UCa # A"]}
        r = merge(existing, [ch("UCa", "A"), ch("UCa", "A"), ch("UCb", "B"), ch("UCb", "B")], TODAY)
        all_lines = r.contents[MUSICIAN] + r.contents[YOUTUBER]
        ids = [p.id for p in map(parse_line, all_lines) if p]
        self.assertEqual(sorted(ids), ["UCa", "UCb"])

    def test_preexisting_duplicates_left_and_warned(self):
        existing = {MUSICIAN: [f"{URL}UCa # A"], YOUTUBER: [f"{URL}UCa # A"]}
        r = merge(existing, [ch("UCa", "A")], TODAY)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # A"])
        self.assertEqual(len(r.warnings), 1)

    def test_canonicalises_hand_edited_lines(self):
        existing = {YOUTUBER: ["- http://youtube.com/channel/UCa/ A"]}
        r = merge(existing, [ch("UCa", "A")], TODAY)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # A"])

    def test_idempotent(self):
        existing = {
            YOUTUBER: ["# YouTubers", "", f"{URL}UCa # A", f"{URL}UCgone # Gone", "trailing note"],
            MUSICIAN: [f"{URL}UCm # M [missing since 2026-01-01]"],
        }
        subs = [ch("UCa", "A2"), ch("UCnew", "New - Topic"), ch("UCm", "M")]
        r1 = merge(existing, subs, TODAY)
        r2 = merge(r1.contents, subs, LATER)
        self.assertEqual(r1.contents, r2.contents)
        for s in r2.summary.values():
            self.assertEqual((s.added, s.newly_missing, s.restored, s.renamed), ([], [], [], []))

    def test_empty_guard(self):
        existing = {YOUTUBER: [f"{URL}UCa # A"]}
        with self.assertRaises(EmptySubscriptionsError):
            merge(existing, [], TODAY)
        r = merge(existing, [], TODAY, allow_empty=True)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # A [missing since {TODAY}]"])

    def test_empty_everything(self):
        r = merge({}, [], TODAY)
        self.assertEqual(r.contents, {MUSICIAN: [], YOUTUBER: []})


class AtomicWriteTests(unittest.TestCase):
    def test_write_preserve_mode_and_no_temp_left(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.md")
            self.assertTrue(eys.atomic_write(p, "a\n"))
            os.chmod(p, 0o600)
            self.assertTrue(eys.atomic_write(p, "b\n"))
            self.assertEqual(eys.read_text(p), "b\n")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(os.listdir(d), ["f.md"])

    def test_unchanged_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.md")
            eys.atomic_write(p, "a\n")
            os.utime(p, (1, 1))
            self.assertFalse(eys.atomic_write(p, "a\n"))
            self.assertEqual(os.stat(p).st_mtime, 1)

    def test_failure_cleans_temp(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.md")
            orig = os.replace
            os.replace = lambda *a: (_ for _ in ()).throw(OSError("boom"))
            try:
                with self.assertRaises(OSError):
                    eys.atomic_write(p, "x")
            finally:
                os.replace = orig
            self.assertEqual(os.listdir(d), [])


class _Req:
    def __init__(self, resp):
        self.resp = resp

    def execute(self):
        return self.resp


class FakeYouTube:
    """Minimal stand-in for the googleapiclient resource."""

    def __init__(self, n_subs, page=50):
        self.ids = [f"UC{i:022d}" for i in range(n_subs)]
        self.page = page
        self.sub_calls = []
        self.chan_calls = []

    def subscriptions(self):
        return self

    def channels(self):
        return _Channels(self)

    def list(self, **kw):  # subscriptions().list
        self.sub_calls.append(kw)
        assert kw["mine"] is True and kw["part"] == "snippet" and kw["maxResults"] == 50
        start = int(kw.get("pageToken") or 0)
        chunk = self.ids[start : start + self.page]
        resp = {
            "items": [
                {"snippet": {"title": f"Sub {cid}", "resourceId": {"channelId": cid}}}
                for cid in chunk
            ]
        }
        if start + self.page < len(self.ids):
            resp["nextPageToken"] = str(start + self.page)
        return _Req(resp)


class _Channels:
    def __init__(self, yt):
        self.yt = yt

    def list(self, **kw):
        ids = kw["id"].split(",")
        self.yt.chan_calls.append(ids)
        assert len(ids) <= 50 and "topicDetails" in kw["part"]
        items = []
        for i, cid in enumerate(ids):
            if cid.endswith("7"):
                continue  # simulate a terminated channel: no channels.list item
            items.append(
                {
                    "id": cid,
                    "snippet": {"title": f"Chan {cid}"},
                    "topicDetails": {"topicIds": ["/m/04rlf"]} if i % 2 else {},
                }
            )
        return _Req({"items": items})


class FetchTests(unittest.TestCase):
    def test_pagination_and_batching(self):
        yt = FakeYouTube(123)
        chans = eys.fetch_channels(yt)
        self.assertEqual(len(chans), 123)
        self.assertEqual(len(yt.sub_calls), 3)
        self.assertEqual([len(b) for b in yt.chan_calls], [50, 50, 23])
        by_id = {c.id: c for c in chans}
        # channels.list title wins; missing channel item keeps subscription title.
        self.assertEqual(by_id[yt.ids[0]].title, f"Chan {yt.ids[0]}")
        self.assertEqual(by_id[yt.ids[7]].title, f"Sub {yt.ids[7]}")
        self.assertEqual(by_id[yt.ids[1]].topic_ids, ["/m/04rlf"])

    def test_empty(self):
        yt = FakeYouTube(0)
        self.assertEqual(eys.fetch_channels(yt), [])
        self.assertEqual(yt.chan_calls, [])


class CliTests(unittest.TestCase):
    def run_cli(self, *argv, fetch=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = eys.main(list(argv), fetch=fetch)
        return code, out.getvalue(), err.getvalue()

    def test_end_to_end_from_json(self):
        with tempfile.TemporaryDirectory() as d:
            fx = os.path.join(d, "subs.json")
            with open(fx, "w") as f:
                json.dump([{"id": "UCm", "title": "A - Topic"}, {"id": "UCy", "title": "Vlog"}], f)
            code, out, _ = self.run_cli("--output-dir", d, "--from-json", fx, "--date", TODAY)
            self.assertEqual(code, 0)
            self.assertIn("musicians.md: 1 channels, +1 added", out)
            with open(os.path.join(d, "musicians.md")) as f:
                self.assertEqual(f.read(), f"# Musicians\n\n{URL}UCm # A - Topic\n")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, _ = self.run_cli("--output-dir", d, "--dry-run", fetch=lambda: [ch("UCa", "A")])
            self.assertEqual(code, 0)
            self.assertIn("dry run", out)
            self.assertEqual(os.listdir(d), [])

    def test_empty_guard_exit_code_and_files_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "youtubers.md")
            with open(p, "w") as f:
                f.write(f"{URL}UCa # A\n")
            code, _, err = self.run_cli("--output-dir", d, fetch=lambda: [])
            self.assertEqual(code, 2)
            self.assertIn("--allow-empty", err)
            self.assertEqual(eys.read_text(p), f"{URL}UCa # A\n")

    def test_fetch_error_leaves_files(self):
        def boom():
            raise RuntimeError("quotaExceeded")

        with tempfile.TemporaryDirectory() as d:
            code, _, err = self.run_cli("--output-dir", d, fetch=boom)
            self.assertEqual(code, 1)
            self.assertIn("quotaExceeded", err)
            self.assertEqual(os.listdir(d), [])

    def test_bad_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "overrides.txt"), "w") as f:
                f.write("UCa band\n")
            code, _, err = self.run_cli("--output-dir", d, fetch=lambda: [])
            self.assertEqual(code, 2)
            self.assertIn("overrides.txt:1", err)

    def test_missing_client_secrets(self):
        try:
            import google_auth_oauthlib  # noqa: F401
        except ImportError:
            self.skipTest("google libs not installed")
        with tempfile.TemporaryDirectory() as d:
            code, _, err = self.run_cli(
                "--output-dir", d, "--client-secrets", os.path.join(d, "nope.json"),
                "--token", os.path.join(d, "token.json"),
            )
            self.assertEqual(code, 2)
            self.assertIn("client secrets not found", err)

    def test_default_token_path_xdg(self):
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg"
        try:
            self.assertEqual(
                eys.default_token_path(), "/tmp/xdg/export-youtube-subscriptions/token.json"
            )
        finally:
            if old is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = old


if __name__ == "__main__":
    unittest.main()
