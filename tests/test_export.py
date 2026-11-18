"""Offline tests: no network, no Google libraries required."""

import ast
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import export_youtube_subscriptions as eys  # noqa: E402
from export_youtube_subscriptions import (  # noqa: E402
    MUSICIAN,
    YOUTUBER,
    Channel,
    DropGuardError,
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


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _write_bytes(path, data):
    with open(path, "wb") as f:
        f.write(data)


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

    # write_files (issue #14): stage every file, then rename them in order.

    def test_write_files_keeps_each_mode_and_applies_repeated_paths_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = os.path.join(directory, "a.md"), os.path.join(directory, "b.md")
            for path, mode in ((first, 0o644), (second, 0o640)):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("old\n")
                os.chmod(path, mode)
            steps = [(first, "mid\n"), (second, "old\n"), (first, "new\n"), (first, "new\n")]
            self.assertEqual(eys.write_files(steps), [True, False, True, False])
            self.assertEqual((eys.read_text(first), eys.read_text(second)), ("new\n", "old\n"))
            self.assertEqual(stat.S_IMODE(os.stat(first).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.stat(second).st_mode), 0o640)
            self.assertEqual(sorted(os.listdir(directory)), ["a.md", "b.md"])

    def test_write_files_stages_everything_before_the_first_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = os.path.join(directory, "a.md"), os.path.join(directory, "b.md")
            staged_before_replace = []

            def counting_replace(source, target):
                staged_before_replace.append(sorted(name for name in os.listdir(directory) if name.endswith(".tmp")))
                return os.rename(source, target)

            with mock.patch.object(eys.os, "replace", side_effect=counting_replace):
                eys.write_files([(first, "a\n"), (second, "b\n")])
            self.assertEqual([len(names) for names in staged_before_replace], [2, 1])

    def test_write_files_failure_while_staging_touches_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = os.path.join(directory, "a.md"), os.path.join(directory, "b.md")
            for path in (first, second):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("old\n")
            real_chmod = os.chmod

            def chmod_fails_second(path, mode, calls=[]):  # noqa: B006 - call counter
                calls.append(path)
                if len(calls) == 2:
                    raise PermissionError(1, "Operation not permitted")
                return real_chmod(path, mode)

            with mock.patch.object(eys.os, "chmod", side_effect=chmod_fails_second), \
                    self.assertRaises(eys.WriteError) as caught:
                eys.write_files([(first, "a\n"), (second, "b\n")])
            self.assertEqual(caught.exception.path, second)
            self.assertIn("Operation not permitted", str(caught.exception))
            self.assertEqual((eys.read_text(first), eys.read_text(second)), ("old\n", "old\n"))
            self.assertEqual(sorted(os.listdir(directory)), ["a.md", "b.md"])

    def test_write_files_failed_rename_keeps_earlier_steps_and_names_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = os.path.join(directory, "a.md"), os.path.join(directory, "b.md")
            real_replace = os.replace

            def second_replace_fails(source, target, calls=[]):  # noqa: B006 - call counter
                calls.append(target)
                if len(calls) == 2:
                    raise OSError(28, "No space left on device")
                return real_replace(source, target)

            with mock.patch.object(eys.os, "replace", side_effect=second_replace_fails), \
                    self.assertRaises(eys.WriteError) as caught:
                eys.write_files([(first, "a\n"), (second, "b\n")])
            self.assertEqual(caught.exception.path, second)
            self.assertEqual(eys.read_text(first), "a\n")
            self.assertEqual(sorted(os.listdir(directory)), ["a.md"])


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
    """main() runs with an injected empty environment and script dir so a real
    export-youtube-subscriptions.env on the dev box can never leak in."""

    def setUp(self):
        self._sandbox = tempfile.TemporaryDirectory()
        self.sandbox = self._sandbox.name
        self.env = {"HOME": os.path.join(self.sandbox, "home")}
        self.script_dir = os.path.join(self.sandbox, "bin")
        os.makedirs(self.script_dir)

    def tearDown(self):
        self._sandbox.cleanup()

    def run_cli(self, *argv, fetch=None, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = eys.main(
                list(argv),
                fetch=fetch,
                environ=self.env if environ is None else environ,
                script_dir=self.script_dir,
            )
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

    # ---- env file integration -------------------------------------------

    def write_env(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_output_dir_from_env_file_next_to_executable_and_created(self):
        target = os.path.join(self.sandbox, "notes", "pages")  # does not exist yet
        envf = self.write_env(
            os.path.join(self.script_dir, eys.ENV_FILENAME), f"EYS_OUTPUT_DIR={target}\n"
        )
        code, out, _ = self.run_cli("--date", TODAY, fetch=lambda: [ch("UCm", "A - Topic")])
        self.assertEqual(code, 0)
        self.assertIn(f"env file: {envf}", out)
        self.assertIn(f"output dir: {target} (from EYS_OUTPUT_DIR in {envf}, created)", out)
        self.assertTrue(os.path.isfile(os.path.join(target, "musicians.md")))

    def test_dry_run_does_not_create_output_dir(self):
        target = os.path.join(self.sandbox, "nope")
        self.write_env(os.path.join(self.script_dir, eys.ENV_FILENAME), f"EYS_OUTPUT_DIR={target}\n")
        code, out, _ = self.run_cli("--dry-run", fetch=lambda: [ch("UCa", "A")])
        self.assertEqual(code, 0)
        self.assertIn("would be created", out)
        self.assertFalse(os.path.exists(target))

    def test_fetch_failure_does_not_create_output_dir(self):
        target = os.path.join(self.sandbox, "nope")

        def boom():
            raise RuntimeError("x")

        code, _, _ = self.run_cli("--output-dir", target, fetch=boom)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(target))

    def test_nested_output_dir_mkdir_p(self):
        target = os.path.join(self.sandbox, "a", "b", "c")
        code, _, _ = self.run_cli("--output-dir", target, fetch=lambda: [ch("UCa", "A")])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(target, "youtubers.md")))

    def test_output_dir_is_a_file(self):
        f = os.path.join(self.sandbox, "file")
        open(f, "w").close()
        code, _, err = self.run_cli("--output-dir", f, fetch=lambda: [ch("UCa", "A")])
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_no_env_file_falls_back_to_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.getcwd()
            os.chdir(d)
            try:
                code, out, _ = self.run_cli("--dry-run", fetch=lambda: [ch("UCa", "A")])
            finally:
                os.chdir(old)
            self.assertEqual(code, 0)
            self.assertIn("env file: none", out)
            self.assertIn("(from default", out)

    def test_cli_flag_beats_env_beats_file(self):
        self.write_env(os.path.join(self.script_dir, eys.ENV_FILENAME), "EYS_OUTPUT_DIR=/from/file\n")
        env_dir = os.path.join(self.sandbox, "env")
        flag_dir = os.path.join(self.sandbox, "flag")
        env = dict(self.env, EYS_OUTPUT_DIR=env_dir)
        _, out, _ = self.run_cli("--dry-run", fetch=lambda: [], environ=env)
        self.assertIn(f"output dir: {env_dir} (from $EYS_OUTPUT_DIR", out)
        _, out, _ = self.run_cli("--dry-run", "--output-dir", flag_dir, fetch=lambda: [], environ=env)
        self.assertIn(f"output dir: {flag_dir} (from --output-dir", out)

    def test_explicit_env_file_missing_is_error(self):
        code, _, err = self.run_cli("--env-file", os.path.join(self.sandbox, "missing.env"), fetch=lambda: [])
        self.assertEqual(code, 2)
        self.assertIn("--env-file", err)
        env = dict(self.env, EYS_ENV_FILE=os.path.join(self.sandbox, "missing.env"))
        code, _, err = self.run_cli(fetch=lambda: [], environ=env)
        self.assertEqual(code, 2)
        self.assertIn("$EYS_ENV_FILE", err)

    def test_malformed_env_file_is_error(self):
        self.write_env(os.path.join(self.script_dir, eys.ENV_FILENAME), "this is not valid\n")
        code, _, err = self.run_cli(fetch=lambda: [])
        self.assertEqual(code, 2)
        self.assertIn(":1: expected KEY=VALUE", err)

    def test_unknown_key_warns(self):
        self.write_env(os.path.join(self.script_dir, eys.ENV_FILENAME), "EYS_OUTPUT_DIRS=/typo\n")
        code, _, err = self.run_cli("--dry-run", fetch=lambda: [])
        self.assertEqual(code, 0)
        self.assertIn("unknown key(s): EYS_OUTPUT_DIRS", err)

    def test_verbose_shows_secrets_and_token_sources(self):
        self.write_env(
            os.path.join(self.script_dir, eys.ENV_FILENAME),
            "EYS_CLIENT_SECRETS=/s/cs.json\nEYS_TOKEN_PATH=/s/tok.json\n",
        )
        _, out, _ = self.run_cli("--dry-run", "-v", "--output-dir", self.sandbox, fetch=lambda: [])
        self.assertIn("client secrets: /s/cs.json (from EYS_CLIENT_SECRETS in", out)
        self.assertIn("token: /s/tok.json (from EYS_TOKEN_PATH in", out)

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


class EnvParseTests(unittest.TestCase):
    ENV = {"HOME": "/home/jay"}

    def parse(self, text):
        return eys.parse_env_file(text, self.ENV)

    def test_basic_comments_blanks(self):
        self.assertEqual(
            self.parse("# c\n\n  EYS_OUTPUT_DIR = /x/y  \n   # indented comment\nB=2\n"),
            {"EYS_OUTPUT_DIR": "/x/y", "B": "2"},
        )

    def test_quotes(self):
        v = self.parse('A="/a b/c"\nB=\'/lit $HOME ~\'\nC="~/q"  # trailing\nD=""\n')
        self.assertEqual(v, {"A": "/a b/c", "B": "/lit $HOME ~", "C": "/home/jay/q", "D": ""})

    def test_home_expansion(self):
        v = self.parse("A=~/notes/pages\nB=$HOME/x\nC=${HOME}/y\nD=~\nE=~other/z\nF=$HOMEX\n")
        self.assertEqual(v["A"], "/home/jay/notes/pages")
        self.assertEqual(v["B"], "/home/jay/x")
        self.assertEqual(v["C"], "/home/jay/y")
        self.assertEqual(v["D"], "/home/jay")
        self.assertEqual(v["E"], "~other/z")  # ~user is not expanded
        self.assertEqual(v["F"], "$HOMEX")  # only the HOME variable, word-bounded

    def test_inline_comment_needs_whitespace(self):
        self.assertEqual(self.parse("A=a#b # comment\n"), {"A": "a#b"})

    def test_export_prefix_crlf_bom_last_wins(self):
        v = self.parse("﻿export A=1\r\nA=2\r\nexport\tB=3")
        self.assertEqual(v, {"A": "2", "B": "3"})

    def test_equals_in_value(self):
        self.assertEqual(self.parse("A=x=y\n"), {"A": "x=y"})

    def test_empty_text(self):
        self.assertEqual(self.parse(""), {})

    def test_malformed(self):
        for bad in ("NOEQUALS", "=v", "1A=v", "A B=v", 'A="unterminated', "A='x' junk"):
            with self.assertRaises(eys.EnvFileError, msg=bad) as cm:
                self.parse(f"# ok\n{bad}\n")
            self.assertIn(":2:", str(cm.exception))


class EnvLookupTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = self._d.name
        self.home = os.path.join(self.d, "home")
        self.script_dir = os.path.join(self.d, "bin")
        self.cfg = os.path.join(self.home, ".config", "export-youtube-subscriptions")
        for p in (self.script_dir, self.cfg):
            os.makedirs(p)
        self.env = {"HOME": self.home}

    def tearDown(self):
        self._d.cleanup()

    def touch(self, path, text="EYS_OUTPUT_DIR=/x\n"):
        with open(path, "w") as f:
            f.write(text)
        return path

    def find(self, cli=None, env=None):
        return eys.find_env_file(cli, env if env is not None else self.env, self.script_dir)

    def test_order(self):
        self.assertIsNone(self.find())
        cfg = self.touch(os.path.join(self.cfg, eys.ENV_FILENAME))
        self.assertEqual(self.find(), cfg)
        beside = self.touch(os.path.join(self.script_dir, eys.ENV_FILENAME))
        self.assertEqual(self.find(), beside)
        via_env = self.touch(os.path.join(self.d, "env.env"))
        env = dict(self.env, EYS_ENV_FILE=via_env)
        self.assertEqual(self.find(env=env), via_env)
        via_flag = self.touch(os.path.join(self.d, "flag.env"))
        self.assertEqual(self.find(cli=via_flag, env=env), via_flag)

    def test_xdg_config_home(self):
        xdg = os.path.join(self.d, "xdg", "export-youtube-subscriptions")
        os.makedirs(xdg)
        p = self.touch(os.path.join(xdg, eys.ENV_FILENAME))
        self.assertEqual(self.find(env=dict(self.env, XDG_CONFIG_HOME=os.path.join(self.d, "xdg"))), p)

    def test_empty_env_var_is_unset(self):
        self.assertIsNone(self.find(env=dict(self.env, EYS_ENV_FILE="")))

    def test_explicit_missing_raises(self):
        with self.assertRaises(eys.EnvFileError):
            self.find(cli=os.path.join(self.d, "nope.env"))
        with self.assertRaises(eys.EnvFileError):
            self.find(env=dict(self.env, EYS_ENV_FILE=os.path.join(self.d, "nope.env")))

    def test_explicit_tilde(self):
        p = self.touch(os.path.join(self.home, "my.env"))
        self.assertEqual(self.find(cli="~/my.env"), p)

    def test_directory_named_like_env_file_is_skipped(self):
        os.makedirs(os.path.join(self.script_dir, eys.ENV_FILENAME))
        self.assertIsNone(self.find())

    def test_symlinked_executable_resolves_to_real_dir(self):
        real = os.path.join(self.d, "real")
        os.makedirs(real)
        script = self.touch(os.path.join(real, "export-youtube-subscriptions"), "")
        link = os.path.join(self.script_dir, "eys")
        os.symlink(script, link)
        envf = self.touch(os.path.join(real, eys.ENV_FILENAME))
        resolved = os.path.dirname(os.path.realpath(link))
        self.assertEqual(eys.find_env_file(None, self.env, resolved), os.path.realpath(envf))

    def test_relative_paths_anchored_to_env_file_dir(self):
        p = self.touch(
            os.path.join(self.script_dir, eys.ENV_FILENAME),
            "EYS_OUTPUT_DIR=out\nEYS_CLIENT_SECRETS=./cs.json\nEYS_TOKEN_PATH=/abs/t.json\nOTHER=rel\n",
        )
        v = eys.load_env_file(p, self.env)
        self.assertEqual(v["EYS_OUTPUT_DIR"], os.path.join(self.script_dir, "out"))
        self.assertEqual(v["EYS_CLIENT_SECRETS"], os.path.join(self.script_dir, "cs.json"))
        self.assertEqual(v["EYS_TOKEN_PATH"], "/abs/t.json")
        self.assertEqual(v["OTHER"], "rel")


class SettingsPrecedenceTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = self._d.name
        self.env = {"HOME": os.path.join(self.d, "home")}
        with open(os.path.join(self.d, eys.ENV_FILENAME), "w") as f:
            f.write("EYS_OUTPUT_DIR=/file/out\nEYS_CLIENT_SECRETS=/file/cs.json\nEYS_TOKEN_PATH=/file/t.json\n")

    def tearDown(self):
        self._d.cleanup()

    def resolve(self, argv=(), env=None, script_dir=None):
        args = eys.build_parser().parse_args(list(argv))
        return eys.resolve_settings(args, env if env is not None else self.env,
                                    script_dir if script_dir is not None else self.d)

    def test_file_beats_default(self):
        s = self.resolve()
        self.assertEqual((s.output_dir, s.client_secrets, s.token_path),
                         ("/file/out", "/file/cs.json", "/file/t.json"))
        self.assertEqual(s.env_file, os.path.join(self.d, eys.ENV_FILENAME))

    def test_env_beats_file(self):
        env = dict(self.env, EYS_OUTPUT_DIR="/env/out", EYS_TOKEN_PATH="~/t.json")
        s = self.resolve(env=env)
        self.assertEqual(s.output_dir, "/env/out")
        self.assertEqual(s.token_path, os.path.join(self.env["HOME"], "t.json"))
        self.assertEqual(s.client_secrets, "/file/cs.json")
        self.assertEqual(s.sources["output-dir"], "$EYS_OUTPUT_DIR")

    def test_flag_beats_env(self):
        env = dict(self.env, EYS_OUTPUT_DIR="/env/out")
        s = self.resolve(["--output-dir", "/flag", "--client-secrets", "c.json", "--token", "t"], env=env)
        self.assertEqual((s.output_dir, s.client_secrets, s.token_path), ("/flag", "c.json", "t"))
        self.assertEqual(s.sources["output-dir"], "--output-dir")

    def test_empty_values_are_unset(self):
        env = dict(self.env, EYS_OUTPUT_DIR="")
        self.assertEqual(self.resolve(env=env).output_dir, "/file/out")
        self.assertEqual(self.resolve(["--output-dir", ""], env=env).output_dir, "/file/out")

    def test_builtin_defaults_without_file(self):
        with tempfile.TemporaryDirectory() as empty:
            s = self.resolve(script_dir=empty)
        self.assertEqual(s.env_file, None)
        self.assertEqual(s.output_dir, ".")
        self.assertEqual(s.client_secrets, "client_secret.json")
        self.assertEqual(
            s.token_path, os.path.join(self.env["HOME"], ".config", "export-youtube-subscriptions", "token.json")
        )
        self.assertEqual(set(s.sources.values()), {"default"})

    def test_single_quoted_file_value_stays_literal(self):
        with open(os.path.join(self.d, eys.ENV_FILENAME), "w") as f:
            f.write("EYS_OUTPUT_DIR='/lit/~'\n")
        self.assertEqual(self.resolve().output_dir, "/lit/~")


class Issue2EdgeCaseTests(unittest.TestCase):
    """Gaps found reviewing the core against issue #2's acceptance criteria."""

    # --- parser ---------------------------------------------------------
    def test_bom_on_first_channel_line_is_recognised(self):
        # A UTF-8 BOM (Windows editors) must not hide the first channel, or
        # the next run would append a duplicate of it.
        p = parse_line(f"\ufeff{URL}UCa # A")
        self.assertIsNotNone(p)
        self.assertEqual((p.id, p.name), ("UCa", "A"))
        r = merge({YOUTUBER: [f"\ufeff{URL}UCa # A"]}, [ch("UCa", "A")], TODAY)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # A"])
        self.assertEqual(r.summary[YOUTUBER].added, [])

    def test_scheme_and_host_case_insensitive(self):
        p = parse_line("HTTPS://WWW.YouTube.com/channel/UCaBc # N")
        self.assertIsNotNone(p)
        self.assertEqual(p.id, "UCaBc")  # ids are case-sensitive: kept verbatim

    def test_marker_on_hand_edited_variant(self):
        p = parse_line("- http://m.youtube.com/channel/UCa/ Old [missing since 2026-01-02]")
        self.assertEqual((p.id, p.name, p.missing_since), ("UCa", "Old", "2026-01-02"))

    def test_empty_name_renders_without_dangling_separator(self):
        self.assertEqual(format_line("UCa", ""), f"{URL}UCa")
        self.assertEqual(format_line("UCa", "  ", TODAY), f"{URL}UCa [missing since {TODAY}]")
        p = parse_line(format_line("UCa", "", TODAY))
        self.assertEqual((p.id, p.name, p.missing_since), ("UCa", "", TODAY))

    def test_other_lines_verbatim_including_whitespace(self):
        odd = ["#  Musicians  ", "\t- note with tab", "   ", "https://www.youtube.com/@handle"]
        r = merge({MUSICIAN: odd}, [ch("UCa", "A - Topic")], TODAY)
        self.assertEqual(r.contents[MUSICIAN], odd + [f"{URL}UCa # A - Topic"])

    # --- category callback ----------------------------------------------
    def test_caller_supplied_category_callback(self):
        calls = []

        def cat(c):
            calls.append(c.id)
            return MUSICIAN if c.id.endswith("m") else YOUTUBER

        existing = {YOUTUBER: [f"{URL}UCold # Old"]}
        subs = [ch("UCold", "Old"), ch("UC1m", "x"), ch("UC2", "y"), ch("UC3m", "z")]
        r = merge(existing, subs, TODAY, categorize=cat)
        self.assertEqual(calls, ["UC1m", "UC2", "UC3m"])  # only new channels, input order
        self.assertEqual(r.contents[MUSICIAN], ["# Musicians", "", f"{URL}UC1m # x", f"{URL}UC3m # z"])
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCold # Old", f"{URL}UC2 # y"])

    def test_category_callback_bad_value_raises(self):
        with self.assertRaises(ValueError):
            merge({}, [ch("UCa", "A")], TODAY, categorize=lambda c: "podcast")

    # --- duplicates -------------------------------------------------------
    def test_duplicate_ids_all_lines_updated_and_counted_once(self):
        existing = {
            MUSICIAN: [f"{URL}UCa # Old", f"{URL}UCb # B"],
            YOUTUBER: [f"{URL}UCa # Old", "- http://youtube.com/channel/UCb/ B"],
        }
        r = merge(existing, [ch("UCa", "New")], TODAY)
        self.assertEqual(r.contents[MUSICIAN], [f"{URL}UCa # New", f"{URL}UCb # B [missing since {TODAY}]"])
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # New", f"{URL}UCb # B [missing since {TODAY}]"])
        self.assertEqual(len(r.warnings), 2)
        self.assertTrue(all("duplicate" in w for w in r.warnings))
        # Stats belong to the first occurrence only, so totals and deltas agree.
        m, y = r.summary[MUSICIAN], r.summary[YOUTUBER]
        self.assertEqual((m.total, m.renamed, m.newly_missing), (2, ["UCa"], ["UCb"]))
        self.assertEqual((y.total, y.renamed, y.newly_missing), (0, [], []))

    def test_duplicate_within_one_file_warns(self):
        r = merge({YOUTUBER: [f"{URL}UCa # A", f"{URL}UCa # A"]}, [ch("UCa", "A")], TODAY)
        self.assertEqual(r.contents[YOUTUBER], [f"{URL}UCa # A", f"{URL}UCa # A"])
        self.assertEqual(len(r.warnings), 1)
        self.assertEqual(r.summary[YOUTUBER].total, 1)

    # --- summary ---------------------------------------------------------
    def test_summary_counts_all_kinds(self):
        existing = {
            YOUTUBER: [
                f"{URL}UCkeep # Keep",
                f"{URL}UCren # Before",
                f"{URL}UCgone # Gone",
                f"{URL}UCstill # Still [missing since 2026-01-01]",
                f"{URL}UCback # Back [missing since 2026-01-01]",
            ]
        }
        subs = [ch("UCkeep", "Keep"), ch("UCren", "After"), ch("UCback", "Back"), ch("UCnew", "New")]
        s = merge(existing, subs, TODAY).summary[YOUTUBER]
        self.assertEqual(s.total, 6)
        self.assertEqual(s.added, ["UCnew"])
        self.assertEqual(s.newly_missing, ["UCgone"])
        self.assertEqual(s.still_missing, ["UCstill"])
        self.assertEqual(s.restored, ["UCback"])
        self.assertEqual(s.renamed, ["UCren"])
        self.assertEqual(
            s.counts(),
            {
                "channels": 6,
                "added": 1,
                "newly_missing": 1,
                "still_missing": 1,
                "restored": 1,
                "renamed": 1,
                "moved": 0,
            },
        )

    # --- atomic write / idempotency on disk --------------------------------
    def test_write_through_symlink_keeps_link(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real.md")
            link = os.path.join(d, "youtubers.md")
            with open(real, "w") as f:
                f.write("old\n")
            os.symlink(real, link)
            self.assertTrue(eys.atomic_write(link, "new\n"))
            self.assertTrue(os.path.islink(link))
            self.assertEqual(eys.read_text(real), "new\n")
            self.assertEqual(sorted(os.listdir(d)), ["real.md", "youtubers.md"])

    def test_new_file_respects_umask(self):
        old = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "f.md")
                eys.atomic_write(p, "a\n")
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        finally:
            os.umask(old)

    def test_cli_rerun_byte_identical_and_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            subs = lambda: [ch("UCm", "A - Topic"), ch("UCy", "Vlog")]  # noqa: E731
            with open(os.path.join(d, "youtubers.md"), "w") as f:
                f.write(f"# Mine\n\n{URL}UCgone # Gone\n")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(eys.main(["--output-dir", d, "--date", TODAY], fetch=subs), 0)
            paths = [os.path.join(d, n) for n in ("musicians.md", "youtubers.md")]
            first = [_read_bytes(p) for p in paths]
            for p in paths:
                os.utime(p, (1, 1))
            with redirect_stdout(out):
                self.assertEqual(eys.main(["--output-dir", d, "--date", LATER], fetch=subs), 0)
            self.assertEqual([_read_bytes(p) for p in paths], first)
            self.assertEqual([os.stat(p).st_mtime for p in paths], [1, 1])
            self.assertIn(f"[missing since {TODAY}]".encode(), first[1])


# --------------------------------------------------------------------------
# Issue #3: classification heuristics and overrides.txt
# --------------------------------------------------------------------------

WIKI = "https://en.wikipedia.org/wiki/"
# Freebase id -> Wikipedia slug, exactly the lists in README "Classification".
MUSIC_TOPICS = {
    "/m/04rlf": "Music",
    "/m/064t9": "Pop_music",
    "/m/06by7": "Rock_music",
    "/m/0glt670": "Hip_hop_music",
    "/m/02lkt": "Electronic_music",
    "/m/03_d0": "Jazz",
    "/m/0ggq0m": "Classical_music",
    "/m/01lyv": "Country_music",
    "/m/06j6l": "Rhythm_and_blues",
    "/m/0gywn": "Soul_music",
    "/m/06cqb": "Reggae",
    "/m/05rwpb": "Independent_music",
    "/m/02mscn": "Christian_music",
    "/m/028sqc": "Music_of_Asia",
    "/m/0g293": "Music_of_Latin_America",
}
WEAK_TOPICS = {"/m/02jjt": "Entertainment", "/m/05qjc": "Performing_arts"}
GAMING, TECH = "/m/0bzvm2", "/m/07c1v"  # strong: anything not listed above


class Issue3ClassifyTableTests(unittest.TestCase):
    def check(self, cases):
        for title, topic_ids, cats, want in cases:
            with self.subTest(title=title, topic_ids=topic_ids, cats=cats):
                self.assertEqual(classify(ch("UCx", title, topic_ids, cats)), want)

    def test_readme_topic_lists_are_exact(self):
        self.assertEqual(eys.MUSIC_TOPIC_IDS, frozenset(MUSIC_TOPICS))
        self.assertEqual(eys.WEAK_TOPIC_IDS, frozenset(WEAK_TOPICS))
        self.assertEqual(eys.MUSIC_WIKI_SLUGS, frozenset(s.lower() for s in MUSIC_TOPICS.values()))
        self.assertEqual(eys.WEAK_WIKI_SLUGS, frozenset(s.lower() for s in WEAK_TOPICS.values()))

    def test_rule1_topic_suffix(self):
        self.check(
            [
                ("Daft Punk - Topic", [], [], MUSICIAN),
                ("Daft Punk - Topic", [GAMING], [], MUSICIAN),  # title rule beats strong topics
                ("Daft Punk - Topic  ", [], [], MUSICIAN),  # API whitespace noise
                (" - Topic", [], [], YOUTUBER),  # stripped title is "- Topic": no artist name
                ("Daft Punk - topic", [], [], YOUTUBER),  # suffix is exact, as YouTube writes it
                ("Daft Punk -Topic", [], [], YOUTUBER),
                ("Daft Punk - Topics", [], [], YOUTUBER),
                ("Hot - Topic of the day", [], [], YOUTUBER),
                ("Topic", [], [], YOUTUBER),
            ]
        )

    def test_rule2_vevo_any_case_anywhere(self):
        self.check(
            [
                ("TaylorSwiftVEVO", [], [], MUSICIAN),
                ("taylorswiftvevo", [], [], MUSICIAN),
                ("Vevo", [], [], MUSICIAN),
                ("vEvO dscvr", [GAMING], [], MUSICIAN),  # title rule beats strong topics
                ("VEV0 fan", [], [], YOUTUBER),
                ("V E V O", [], [], YOUTUBER),
            ]
        )

    def test_rule3_every_music_topic_alone(self):
        for topic_id, slug in MUSIC_TOPICS.items():
            with self.subTest(topic=topic_id):
                self.assertEqual(classify(ch("UCx", "Band", [topic_id])), MUSICIAN)
                self.assertEqual(classify(ch("UCx", "Band", cats=[WIKI + slug])), MUSICIAN)

    def test_rule3_weak_and_strong_mixes(self):
        weak = list(WEAK_TOPICS)
        self.check(
            [
                ("Band", ["/m/04rlf"] + weak, [], MUSICIAN),
                ("Band", ["/m/04rlf", "/m/06by7"] + weak, [], MUSICIAN),
                ("Band", weak, [], YOUTUBER),  # weak alone is not music
                ("Band", [weak[0]], [], YOUTUBER),
                ("Band", [GAMING], [], YOUTUBER),
                ("Band", ["/m/04rlf", GAMING], [], YOUTUBER),
                ("Band", ["/m/04rlf", TECH] + weak, [], YOUTUBER),
                ("Band", [GAMING, "/m/04rlf"], [], YOUTUBER),  # order doesn't matter
                ("Band", ["/m/04rlf", "/m/04rlf"], [], MUSICIAN),  # repeats are harmless
                ("Band", [], [], YOUTUBER),  # no topics at all
                ("", [], [], YOUTUBER),
            ]
        )

    def test_rule3_sources_are_combined(self):
        self.check(
            [
                ("Band", ["/m/04rlf"], [WIKI + "Music", WIKI + "Entertainment"], MUSICIAN),
                ("Band", [], [WIKI + "Music", WIKI + "Performing_arts"], MUSICIAN),
                ("Band", ["/m/02jjt"], [WIKI + "Jazz"], MUSICIAN),  # music only in categories
                ("Band", ["/m/03_d0"], [WIKI + "Entertainment"], MUSICIAN),  # music only in ids
                ("Band", ["/m/04rlf"], [WIKI + "Video_game_culture"], YOUTUBER),  # strong in categories
                ("Band", [GAMING], [WIKI + "Music"], YOUTUBER),  # strong in ids
                ("Band", [], [WIKI + "Entertainment"], YOUTUBER),
                ("Band", [], [WIKI + "Musical_theatre"], YOUTUBER),  # not in the list: strong
                ("Band", [], [WIKI + "Music_of_Africa"], YOUTUBER),
            ]
        )

    def test_wikipedia_url_variants(self):
        self.check(
            [
                ("Band", [], ["https://en.wikipedia.org/wiki/Music"], MUSICIAN),
                ("Band", [], ["http://en.m.wikipedia.org/wiki/music/"], MUSICIAN),
                ("Band", [], [WIKI + "Rhythm%20and%20blues"], MUSICIAN),  # percent-encoded
                ("Band", [], [WIKI + "Hip hop music"], MUSICIAN),  # spaces for underscores
                ("Band", [], [WIKI + "Music?oldid=1#History"], MUSICIAN),  # query + fragment
                ("Band", [], [" " + WIKI + "Music\n"], MUSICIAN),
                ("Band", [], [WIKI + "Music#Entertainment"], MUSICIAN),
                ("Band", [], [WIKI + "Humour#Music"], YOUTUBER),  # fragment is not the article
            ]
        )

    def test_blank_and_missing_topics_are_ignored(self):
        self.check(
            [
                ("Band", ["/m/04rlf", "", "  "], ["", None], MUSICIAN),  # blanks don't count as strong
                ("Band", [" /m/04rlf "], [], MUSICIAN),
                ("Band", ["", None], [""], YOUTUBER),  # nothing usable -> no topics
                ("Band", ["/M/04RLF"], [], YOUTUBER),  # Freebase ids are case-sensitive: unknown -> strong
            ]
        )
        self.assertEqual(classify(Channel("UCx", None, None, None)), YOUTUBER)
        self.assertEqual(classify(Channel("UCx", "Band", None, [WIKI + "Jazz"])), MUSICIAN)


class Issue3OverridesParseTests(unittest.TestCase):
    def test_valid_forms(self):
        cases = [
            ("", {}),
            ("\n\n   \n", {}),
            ("# only a comment\n", {}),
            ("UCa musician\n", {"UCa": MUSICIAN}),
            ("UCa youtuber", {"UCa": YOUTUBER}),  # no trailing newline
            ("UCa\tmusician\t# tab separated\n", {"UCa": MUSICIAN}),
            ("  UCa   youtuber   # padded\n", {"UCa": YOUTUBER}),
            ("UCa musician#tight comment\n", {"UCa": MUSICIAN}),
            ("UCa Musician\nUCb YOUTUBER\n", {"UCa": MUSICIAN, "UCb": YOUTUBER}),
            ("UCa musician\r\nUCb youtuber\r\n", {"UCa": MUSICIAN, "UCb": YOUTUBER}),  # CRLF
            ("﻿UCa musician\n", {"UCa": MUSICIAN}),  # BOM from a Windows editor
            ("﻿# header\nUC-a_1 youtuber\n", {"UC-a_1": YOUTUBER}),
            ("UCa musician\nUCa musician # same again\n", {"UCa": MUSICIAN}),  # harmless repeat
            ("UCa musician\nUca youtuber\n", {"UCa": MUSICIAN, "Uca": YOUTUBER}),  # ids are case-sensitive
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_overrides(text), want)

    def test_readme_example(self):
        text = (
            "# CHANNEL_ID category  # optional comment\n"
            "UCxxxxxxxxxxxxxxxxxxxxxx musician   # band without topic tags\n"
            "UCyyyyyyyyyyyyyyyyyyyyyy youtuber   # music-theory teacher\n"
        )
        self.assertEqual(
            parse_overrides(text),
            {"UCxxxxxxxxxxxxxxxxxxxxxx": MUSICIAN, "UCyyyyyyyyyyyyyyyyyyyyyy": YOUTUBER},
        )

    def test_malformed_lines_raise_config_error_with_location(self):
        cases = [
            ("UCa\n", 1),  # no category
            ("UCa singer\n", 1),  # unknown category
            ("UCa musicians\n", 1),  # plural typo
            ("UCa musician extra\n", 1),  # trailing junk
            ("UCa = musician\n", 1),
            ("UCa: musician\n", 1),
            ("musician UCa\n", 1),  # swapped columns
            ("# ok\n\nUCa musician\nUCb\n", 4),  # line number counts blanks/comments
            (URL + "UCa musician\n", 1),  # a URL is not an id
            ("UC$a musician\n", 1),
            ("UCa musician\nUCb youtuber\nUCa youtuber\n", 3),  # contradicts line 1
        ]
        for text, lineno in cases:
            with self.subTest(text=text):
                with self.assertRaises(eys.OverridesError) as ctx:
                    parse_overrides(text)
                self.assertIn(f"overrides.txt:{lineno}:", str(ctx.exception))
        self.assertTrue(issubclass(eys.OverridesError, ValueError))

    def test_error_names_the_source_path(self):
        with self.assertRaises(eys.OverridesError) as ctx:
            parse_overrides("UCa nope\n", source="/notes/overrides.txt")
        self.assertIn("/notes/overrides.txt:1:", str(ctx.exception))

    def test_load_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(eys.load_overrides(d), {})  # no file is fine
            path = os.path.join(d, "overrides.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("UCa musician  # café band\n")
            self.assertEqual(eys.load_overrides(d), {"UCa": MUSICIAN})
            with open(path, "wb") as f:
                f.write(b"UCa musician # \xff\xfe\n")  # not UTF-8
            with self.assertRaises(eys.OverridesError):
                eys.load_overrides(d)
            os.remove(path)
            os.mkdir(path)  # unreadable: a directory
            with self.assertRaises(eys.OverridesError) as ctx:
                eys.load_overrides(d)
            self.assertIn(path, str(ctx.exception))


class Issue3OverridesMergeTests(unittest.TestCase):
    def test_new_channel_placement_table(self):
        cases = [
            # title, topic ids, override, expected file
            ("X - Topic", [], None, MUSICIAN),
            ("X - Topic", [], YOUTUBER, YOUTUBER),
            ("XVEVO", [], YOUTUBER, YOUTUBER),
            ("Theory teacher", ["/m/04rlf"], YOUTUBER, YOUTUBER),  # README weak spot
            ("Untagged band", [], None, YOUTUBER),
            ("Untagged band", [], MUSICIAN, MUSICIAN),
            ("Gamer", [GAMING], MUSICIAN, MUSICIAN),
            ("Band", ["/m/04rlf"], MUSICIAN, MUSICIAN),  # agreeing override
        ]
        for title, topic_ids, override, want in cases:
            with self.subTest(title=title, override=override):
                overrides = {"UCa": override} if override else {}
                r = merge({}, [ch("UCa", title, topic_ids)], TODAY, overrides)
                other = YOUTUBER if want == MUSICIAN else MUSICIAN
                self.assertEqual(r.contents[want][-1], f"{URL}UCa # {title}")
                self.assertEqual(r.contents[other], [])
                self.assertEqual(r.summary[want].added, ["UCa"])
                self.assertEqual(r.warnings, [])

    def test_categorizer_is_the_merge_callback(self):
        categorize = eys.make_categorizer({"UCo": YOUTUBER})
        self.assertEqual(categorize(ch("UCo", "X - Topic")), YOUTUBER)
        self.assertEqual(categorize(ch("UCn", "X - Topic")), MUSICIAN)
        self.assertEqual(categorize(ch("UCn", "Vlog")), YOUTUBER)
        r = merge({}, [ch("UCo", "X - Topic"), ch("UCn", "Y - Topic")], TODAY, categorize=categorize)
        self.assertEqual(r.contents[YOUTUBER], ["# YouTubers", "", f"{URL}UCo # X - Topic"])
        self.assertEqual(r.contents[MUSICIAN], ["# Musicians", "", f"{URL}UCn # Y - Topic"])

    def test_disagreement_warns_and_never_moves(self):
        cases = [
            # file holding the line, override, subscribed?, missing marker already there?
            (YOUTUBER, MUSICIAN, True, False),
            (MUSICIAN, YOUTUBER, True, False),
            (YOUTUBER, MUSICIAN, False, False),  # unsubscribed: still misplaced per the override
            (MUSICIAN, YOUTUBER, False, True),
            (MUSICIAN, YOUTUBER, True, True),  # restored in place
        ]
        for cat, override, subscribed, marked in cases:
            with self.subTest(cat=cat, subscribed=subscribed, marked=marked):
                other = YOUTUBER if cat == MUSICIAN else MUSICIAN
                line = format_line("UCa", "Band", "2026-01-01" if marked else None)
                existing = {cat: ["# Mine", line], other: ["# Other"]}
                subs = [ch("UCa", "Band"), ch("UCkeep", "Keep")] if subscribed else [ch("UCkeep", "Keep")]
                r = merge(existing, subs, TODAY, {"UCa": override})
                self.assertEqual(len(r.contents[cat]), 2 + (cat == YOUTUBER))
                self.assertTrue(r.contents[cat][1].startswith(f"{URL}UCa # Band"))
                self.assertFalse(any("UCa" in out_line for out_line in r.contents[other]))
                hits = [w for w in r.warnings if "UCa" in w]
                self.assertEqual(len(hits), 1, r.warnings)
                for needle in (override, eys.FILENAMES[cat], eys.FILENAMES[other], "by hand"):
                    self.assertIn(needle, hits[0])

    def test_agreeing_or_unrelated_override_is_silent(self):
        existing = {MUSICIAN: [f"{URL}UCa # Band"], YOUTUBER: [f"{URL}UCb # Vlog"]}
        subs = [ch("UCa", "Band"), ch("UCb", "Vlog")]
        r = merge(existing, subs, TODAY, {"UCa": MUSICIAN, "UCb": YOUTUBER, "UCnowhere": MUSICIAN})
        self.assertEqual(r.warnings, [])
        self.assertEqual(r.contents, existing)

    def test_override_set_after_placement_is_stable_across_runs(self):
        subs = [ch("UCa", "Teacher", ["/m/04rlf"])]
        first = merge({}, subs, TODAY)  # heuristic: musician
        second = merge(first.contents, subs, LATER, {"UCa": YOUTUBER})
        self.assertEqual(second.contents, first.contents)
        self.assertEqual(len(second.warnings), 1)
        third = merge(second.contents, subs, LATER, {"UCa": YOUTUBER})
        self.assertEqual(third.contents, first.contents)


class Issue3CliTests(unittest.TestCase):
    # Borrow the sandboxed runner without inheriting (and re-running) CliTests.
    setUp, tearDown, run_cli = CliTests.setUp, CliTests.tearDown, CliTests.run_cli

    def write(self, directory, name, text):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_overrides_place_new_channels_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "youtubers.md", f"# YouTubers\n\n{URL}UCold # Old - Topic\n")
            fixture = os.path.join(d, "subs.json")
            with open(fixture, "w", encoding="utf-8") as f:
                json.dump(
                    [
                        {"id": "UCband", "title": "Band"},
                        {"id": "UCteach", "title": "Teacher", "topicIds": ["/m/04rlf"], "topicCategories": None},
                        {"id": "UCold", "title": "Old - Topic", "topicIds": None},
                        {"id": "UCjazz", "title": "Trio", "topicCategories": [WIKI + "Jazz", WIKI + "Entertainment"]},
                    ],
                    f,
                )
            self.write(d, "overrides.txt", "# mine\nUCband musician  # untagged\nUCteach youtuber\nUCold musician\n")
            code, out, err = self.run_cli("--output-dir", d, "--from-json", fixture, "--date", TODAY)
            self.assertEqual((code, err), (0, ""))
            musicians = _read_bytes(os.path.join(d, "musicians.md")).decode()
            youtubers = _read_bytes(os.path.join(d, "youtubers.md")).decode()
            self.assertEqual(musicians, f"# Musicians\n\n{URL}UCband # Band\n{URL}UCjazz # Trio\n")
            self.assertEqual(
                youtubers, f"# YouTubers\n\n{URL}UCold # Old - Topic\n{URL}UCteach # Teacher\n"
            )
            self.assertIn("warning: override says UCold", out)
            self.assertIn("by hand", out)

    def test_bad_overrides_exit_2_before_fetch_and_touch_nothing(self):
        for text in ("UCa band\n", "UCa\n", "UCa musician\nUCa youtuber\n"):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as d:
                self.write(d, "overrides.txt", text)
                self.write(d, "youtubers.md", f"{URL}UCgone # Gone\n")
                fetched = []
                code, out, err = self.run_cli("--output-dir", d, fetch=lambda: fetched.append(1) or [ch("UCn", "N")])  # noqa: B023
                self.assertEqual(code, 2)
                self.assertEqual(fetched, [])  # no API quota spent on a config typo
                self.assertIn(os.path.join(d, "overrides.txt") + ":", err)
                self.assertEqual(out, "")
                self.assertEqual(sorted(os.listdir(d)), ["overrides.txt", "youtubers.md"])
                self.assertEqual(_read_bytes(os.path.join(d, "youtubers.md")), f"{URL}UCgone # Gone\n".encode())

    def test_unreadable_overrides_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "overrides.txt"))
            code, _, err = self.run_cli("--output-dir", d, fetch=lambda: [ch("UCn", "N")])
            self.assertEqual(code, 2)
            self.assertIn("overrides.txt", err)
            self.assertEqual(os.listdir(d), ["overrides.txt"])


# --------------------------------------------------------------------------
# Issue #4: API fetch, OAuth and the full CLI
# --------------------------------------------------------------------------

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOOGLE_TOP_LEVEL = ("google", "googleapiclient", "google_auth_oauthlib")


class ScriptedYouTube:
    """Fake resource that replays canned responses and records every call.

    A response that is an Exception is raised from ``execute()``, the way
    googleapiclient raises HttpError.
    """

    def __init__(self, sub_pages, chan_responses=None):
        self.sub_pages = list(sub_pages)
        self.chan_responses = chan_responses
        self.sub_calls = []
        self.chan_calls = []

    def subscriptions(self):
        return _Scripted(self.sub_calls, self.sub_pages)

    def channels(self):
        return _Scripted(self.chan_calls, self.chan_responses)


class _Scripted:
    def __init__(self, calls, responses):
        self.calls, self.responses = calls, responses

    def list(self, **kw):
        self.calls.append(kw)
        if self.responses is None:  # channels.list: no details for anyone
            return _Req({"items": []})
        return self

    def execute(self):
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def sub_page(ids, next_token=None):
    page = {"items": [{"snippet": {"title": f"Sub {i}", "resourceId": {"channelId": i}}} for i in ids]}
    if next_token:
        page["nextPageToken"] = next_token
    return page


class Issue4FetchTests(unittest.TestCase):
    def test_request_parameters(self):
        yt = FakeYouTube(60)
        eys.fetch_channels(yt)
        first, second = yt.sub_calls
        self.assertEqual(first, {"part": "snippet", "mine": True, "maxResults": 50, "order": "alphabetical"})
        self.assertEqual(second["pageToken"], "50")  # the token is passed back verbatim
        yt = ScriptedYouTube([sub_page(["UCa", "UCb"])])
        eys.fetch_channels(yt)
        self.assertEqual(yt.chan_calls, [{"part": "snippet,topicDetails", "id": "UCa,UCb", "maxResults": 50}])

    def test_page_and_batch_boundaries(self):
        for count, pages, batches in ((1, 1, [1]), (50, 1, [50]), (51, 2, [50, 1]), (100, 2, [50, 50])):
            with self.subTest(count=count):
                yt = FakeYouTube(count)
                chans = eys.fetch_channels(yt)
                self.assertEqual([c.id for c in chans], yt.ids)  # API order kept across pages
                self.assertEqual(len(yt.sub_calls), pages)
                self.assertEqual([len(b) for b in yt.chan_calls], batches)

    def test_duplicate_and_malformed_items_are_skipped(self):
        junk = [{"snippet": {"title": "no id"}}, {"snippet": None}, {}]
        pages = [sub_page(["UCa", "UCb"], "t1"), {"items": sub_page(["UCb", "UCc"])["items"] + junk}]
        yt = ScriptedYouTube(pages)
        self.assertEqual([c.id for c in eys.fetch_channels(yt)], ["UCa", "UCb", "UCc"])
        self.assertEqual(yt.chan_calls[0]["id"], "UCa,UCb,UCc")

    def test_null_fields_are_tolerated(self):
        chan = {"id": "UCa", "snippet": None, "topicDetails": {"topicIds": None, "topicCategories": None}}
        yt = ScriptedYouTube([sub_page(["UCa"])], [{"items": [chan, {"id": "UCstranger"}]}])
        self.assertEqual(eys.fetch_channels(yt), [Channel("UCa", "Sub UCa", [], [])])
        self.assertEqual(eys.fetch_channels(ScriptedYouTube([{"items": None}], [{"items": None}])), [])

    def test_topics_come_from_channels_list(self):
        details = {"topicIds": ["/m/04rlf"], "topicCategories": [WIKI + "Music"]}
        chan = {"id": "UCa", "snippet": {"title": "New\nName"}, "topicDetails": details}
        yt = ScriptedYouTube([sub_page(["UCa"])], [{"items": [chan]}])
        self.assertEqual(eys.fetch_channels(yt), [Channel("UCa", "New Name", ["/m/04rlf"], [WIKI + "Music"])])

    def test_repeated_page_token_aborts_instead_of_looping(self):
        yt = ScriptedYouTube([sub_page(["UCa"], "same"), sub_page(["UCb"], "same"), sub_page(["UCc"], "same")])
        with self.assertRaisesRegex(RuntimeError, "repeated page token"):
            eys.fetch_channels(yt)
        self.assertEqual(len(yt.sub_calls), 2)

    def test_errors_propagate(self):
        with self.assertRaisesRegex(OSError, "page 2"):
            eys.fetch_channels(ScriptedYouTube([sub_page(["UCa"], "t1"), OSError("page 2")]))
        with self.assertRaisesRegex(OSError, "batch"):
            eys.fetch_channels(ScriptedYouTube([sub_page(["UCa"])], [OSError("batch")]))

    def test_injected_resource_skips_auth(self):
        # No client secrets, no token, no Google libraries: none are needed.
        chans = eys.fetch_from_api("/nonexistent/cs.json", "/nonexistent/token.json", youtube=FakeYouTube(3))
        self.assertEqual(len(chans), 3)


class Issue4CliTests(unittest.TestCase):
    setUp, tearDown = CliTests.setUp, CliTests.tearDown

    def run_cli(self, *argv, youtube=None, environ=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = eys.main(
                list(argv),
                environ=self.env if environ is None else environ,
                script_dir=self.script_dir,
                youtube=youtube,
            )
        return code, out.getvalue(), err.getvalue()

    def write(self, directory, name, text):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def fixture(self, data):
        return self.write(self.sandbox, "subs.json", data if isinstance(data, str) else json.dumps(data))

    def snapshot(self, directory):
        return {name: _read_bytes(os.path.join(directory, name)) for name in sorted(os.listdir(directory))}

    # ---- fake API through the whole CLI ----------------------------------

    def test_fake_api_end_to_end(self):
        out_dir = os.path.join(self.sandbox, "out")
        yt = FakeYouTube(123)
        code, out, err = self.run_cli("--output-dir", out_dir, "--date", TODAY, youtube=yt)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((len(yt.sub_calls), [len(b) for b in yt.chan_calls]), (3, [50, 50, 23]))
        self.assertIn("fetched 123 subscriptions\n", out)
        musicians = eys.read_text(os.path.join(out_dir, "musicians.md")).splitlines()
        youtubers = eys.read_text(os.path.join(out_dir, "youtubers.md")).splitlines()
        self.assertEqual((musicians[0], youtubers[0]), ("# Musicians", "# YouTubers"))
        self.assertEqual(len(musicians) + len(youtubers) - 4, 123)
        self.assertIn(f"{URL}{yt.ids[1]} # Chan {yt.ids[1]}", musicians)  # tagged Music
        self.assertIn(f"{URL}{yt.ids[7]} # Sub {yt.ids[7]}", youtubers)  # no channels.list item
        before = self.snapshot(out_dir)
        code, out, _ = self.run_cli("--output-dir", out_dir, "--date", LATER, youtube=FakeYouTube(123))
        self.assertEqual(code, 0)
        self.assertIn("+0 added, 0 newly missing", out)
        self.assertEqual(self.snapshot(out_dir), before)

    def test_api_errors_exit_1_and_touch_nothing(self):
        failures = {
            "second page": ScriptedYouTube([sub_page(["UCa"], "t1"), OSError("network down")]),
            "channels.list": ScriptedYouTube([sub_page(["UCa"])], [RuntimeError("quotaExceeded")]),
            "token loop": ScriptedYouTube([sub_page(["UCa"], "t"), sub_page(["UCb"], "t")]),
        }
        for name, yt in failures.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                self.write(d, "youtubers.md", f"{URL}UCold # Old\n")
                before = self.snapshot(d)
                code, out, err = self.run_cli("--output-dir", d, "--date", TODAY, youtube=yt)
                self.assertEqual((code, out), (1, ""))
                self.assertIn("error: failed to fetch subscriptions:", err)
                self.assertEqual(self.snapshot(d), before)
        missing_dir = os.path.join(self.sandbox, "never")
        code, _, _ = self.run_cli("--output-dir", missing_dir, youtube=ScriptedYouTube([OSError("x")]))
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(missing_dir))

    def test_empty_list_guard(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "musicians.md", f"# Musicians\n\n{URL}UCa # A\n")
            before = self.snapshot(d)
            for extra in ((), ("--dry-run",)):
                code, out, err = self.run_cli("--output-dir", d, *extra, youtube=FakeYouTube(0))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("0 subscriptions", err)
                self.assertEqual(self.snapshot(d), before)
            code, out, _ = self.run_cli("--output-dir", d, "--allow-empty", "--date", TODAY, youtube=FakeYouTube(0))
            self.assertEqual(code, 0)
            self.assertIn("musicians.md: 1 channels, +0 added, 1 newly missing", out)
            self.assertEqual(
                eys.read_text(os.path.join(d, "musicians.md")),
                f"# Musicians\n\n{URL}UCa # A [missing since {TODAY}]\n",
            )

    def test_empty_list_with_no_channels_on_file_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "youtubers.md", "# YouTubers\n\nnotes only\n")
            before = self.snapshot(d)
            code, out, _ = self.run_cli("--output-dir", d, youtube=FakeYouTube(0))
            self.assertEqual(code, 0)
            self.assertIn("fetched 0 subscriptions", out)
            self.assertEqual(self.snapshot(d), before)  # and no empty musicians.md appears

    # ---- output format ----------------------------------------------------

    def test_summary_matches_readme_format(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "musicians.md", f"# Musicians\n\n{URL}UCm1 # M1\n{URL}UCm2 # M2\n{URL}UCm3 # M3\n")
            self.write(d, "youtubers.md", f"# YouTubers\n\n{URL}UCy1 # Y1\n{URL}UCy2 # Y2\n{URL}UCgone # Gone\n")
            names = {"UCm1": "M1", "UCm2": "M2", "UCm3": "M3", "UCy1": "Y1", "UCy2": "Y2"}
            subs = [{"id": i, "title": t} for i, t in names.items()]
            subs += [{"id": "UCn1", "title": "N1 - Topic"}, {"id": "UCn2", "title": "N2VEVO"}]
            argv = ("--output-dir", d, "--from-json", self.fixture(subs), "--date", TODAY)
            code, out, err = self.run_cli(*argv, "--dry-run")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(
                out.splitlines(),
                [
                    "env file: none",
                    f"output dir: {os.path.abspath(d)} (from --output-dir)",
                    "fetched 7 subscriptions",
                    "musicians.md: 5 channels, +2 added, 0 newly missing, 0 still missing, 0 restored, 0 renamed",
                    "youtubers.md: 3 channels, +0 added, 1 newly missing, 0 still missing, 0 restored, 0 renamed",
                    "dry run: no files written",
                ],
            )
            code, verbose_out, _ = self.run_cli(*argv, "--verbose")
            self.assertEqual(code, 0)
            expected_lines = (
                "  musicians.md added: UCn1",
                "  musicians.md added: UCn2",
                "  youtubers.md newly missing: UCgone",
            )
            for line in expected_lines:
                self.assertIn(line, verbose_out.splitlines())
            self.assertNotIn("UCn1", out)  # ids only with -v

    def test_verbose_lists_restored_and_renamed(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "youtubers.md", f"{URL}UCback # Back [missing since 2026-01-01]\n{URL}UCren # Old\n")
            subs = [{"id": "UCback", "title": "Back"}, {"id": "UCren", "title": "New"}]
            code, out, _ = self.run_cli("--output-dir", d, "--from-json", self.fixture(subs), "-v")
            self.assertEqual(code, 0)
            self.assertIn(
                "youtubers.md: 2 channels, +0 added, 0 newly missing, 0 still missing, 1 restored, 1 renamed", out
            )
            self.assertIn("  youtubers.md restored: UCback", out)
            self.assertIn("  youtubers.md renamed: UCren", out)

    def test_date_defaults_to_today(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "youtubers.md", f"{URL}UCgone # Gone\n")
            code, _, _ = self.run_cli("--output-dir", d, "--from-json", self.fixture([{"id": "UCa", "title": "A"}]))
            self.assertEqual(code, 0)
            today = eys._dt.date.today().isoformat()
            self.assertIn(f"{URL}UCgone # Gone [missing since {today}]", eys.read_text(os.path.join(d, "youtubers.md")))

    # ---- exit code 2: usage and config ------------------------------------

    def test_bad_date_is_usage_error(self):
        for bad in ("2026-13-01", "2026-02-30", "20261115", "2026-1-5", "tomorrow", "2026-11-15T00:00"):
            with self.subTest(bad), tempfile.TemporaryDirectory() as d:
                code, out, err = self.run_cli("--output-dir", d, "--date", bad, youtube=FakeYouTube(1))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--date", err)
                self.assertEqual(os.listdir(d), [])

    def test_argparse_usage_errors_exit_2_and_help_exits_0(self):
        for argv, expected in ((["--bogus"], 2), (["--date"], 2), (["stray"], 2), (["--help"], 0), (["-h"], 0)):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as caught:
                self.run_cli(*argv)
            self.assertEqual(caught.exception.code, expected)

    def test_help_documents_every_readme_option(self):
        text = eys.build_parser().format_help()
        for option in ("--output-dir", "--client-secrets", "--token", "--env-file", "--no-browser", "--dry-run",
                       "--allow-empty", "--allow-drop", "-v, --verbose", "--from-json", "--date"):
            self.assertIn(option, text)

    def test_bad_fixture_is_usage_error(self):
        bad_fixtures = {
            "not json": "{nope",
            "empty file": "",
            "not a list": {"id": "UCa", "title": "A"},
            "item not an object": ["UCa"],
            "missing id": [{"title": "A"}],
            "null id": [{"id": None, "title": "A"}],
            "id would break the line format": [{"id": "UC a # x", "title": "A"}],
            "numeric title": [{"id": "UCa", "title": 7}],
            "topics not a list": [{"id": "UCa", "title": "A", "topicIds": "/m/04rlf"}],
        }
        for name, data in bad_fixtures.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as d:
                self.write(d, "youtubers.md", f"{URL}UCold # Old\n")
                before = self.snapshot(d)
                code, out, err = self.run_cli("--output-dir", d, "--from-json", self.fixture(data))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("--from-json fixture", err)
                self.assertEqual(self.snapshot(d), before)
        code, _, err = self.run_cli("--from-json", os.path.join(self.sandbox, "missing.json"))
        self.assertEqual(code, 2)
        self.assertIn("missing.json", err)

    def test_fixture_optional_fields(self):
        subs = [{"id": "UCa"}, {"id": "UCb", "title": None, "topicIds": None, "topicCategories": None}]
        chans = eys.load_json_channels(self.fixture(subs))
        self.assertEqual(chans, [Channel("UCa", "", [], []), Channel("UCb", "", [], [])])

    def test_unreadable_existing_file_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "musicians.md"), "wb") as f:
                f.write(b"\xff\xfe not utf-8\n")
            before = self.snapshot(d)
            code, out, err = self.run_cli("--output-dir", d, youtube=FakeYouTube(2))
            self.assertEqual((code, out), (2, ""))
            self.assertIn("cannot read the existing files", err)
            self.assertEqual(self.snapshot(d), before)

    def test_write_failure_is_exit_2_not_a_traceback(self):
        def refuse(steps):
            path = steps[0][0]
            raise eys.WriteError(path, PermissionError(13, "Permission denied", path))

        original, eys.write_files = eys.write_files, refuse
        try:
            with tempfile.TemporaryDirectory() as d:
                code, _, err = self.run_cli("--output-dir", d, youtube=FakeYouTube(2))
                self.assertEqual(code, 2)
                self.assertIn("cannot write", err)
                self.assertEqual(os.listdir(d), [])
        finally:
            eys.write_files = original

    def test_missing_google_libraries_is_exit_2_with_install_hint(self):
        blocked = dict.fromkeys(GOOGLE_TOP_LEVEL)  # None in sys.modules makes the import fail
        with mock.patch.dict(sys.modules, blocked), tempfile.TemporaryDirectory() as d:
            code, out, err = self.run_cli("--output-dir", d, "--token", os.path.join(d, "token.json"))
            self.assertEqual((code, out), (2, ""))
            self.assertIn("requirements.txt", err)
            self.assertEqual(os.listdir(d), [])


# ---- OAuth, exercised against stand-ins for the Google libraries ----------


class FakeCreds:
    def __init__(self, valid=True, expired=False, refresh_token="refresh-token", refresh_error=None):
        self.valid, self.expired, self.refresh_token = valid, expired, refresh_token
        self.refresh_error = refresh_error
        self.refreshed = 0

    def refresh(self, request):
        self.refreshed += 1
        if self.refresh_error:
            raise self.refresh_error
        self.valid, self.expired = True, False

    def to_json(self):
        return json.dumps({"refresh_token": self.refresh_token})


class FakeGoogle:
    """Just enough of google-auth / google-auth-oauthlib / googleapiclient to
    run get_credentials() and build_youtube() offline, recording what they ask for."""

    class RefreshError(Exception):
        pass

    def __init__(self, cached=None, fresh=None, resource=None):
        self.cached = cached  # what the token file "contains"; an Exception is raised
        self.fresh = fresh or FakeCreds()  # what the interactive flow returns
        self.resource = resource
        self.calls = []

    def _load_token(self, path, scopes):
        self.calls.append(("load_token", path, tuple(scopes)))
        if isinstance(self.cached, Exception):
            raise self.cached
        return self.cached

    def _flow(self, path, scopes):
        self.calls.append(("flow", path, tuple(scopes)))
        with open(path, encoding="utf-8") as f:
            json.load(f)["installed"]  # the real library rejects non-Desktop files too
        return self

    def run_local_server(self, **kwargs):
        self.calls.append(("run_local_server", kwargs))
        return self.fresh

    def _build(self, service, version, **kwargs):
        self.calls.append(("build", service, version, kwargs))
        return self.resource

    def modules(self):
        def module(name, **attrs):
            mod = types.ModuleType(name)
            mod.__dict__.update(attrs)
            return mod

        mods = [
            module("google"),
            module("google.auth"),
            module("google.auth.exceptions", RefreshError=self.RefreshError),
            module("google.auth.transport"),
            module("google.auth.transport.requests", Request=object),
            module("google.oauth2"),
            module("google.oauth2.credentials", Credentials=module("C", from_authorized_user_file=self._load_token)),
            module("google_auth_oauthlib"),
            module("google_auth_oauthlib.flow", InstalledAppFlow=module("F", from_client_secrets_file=self._flow)),
            module("googleapiclient"),
            module("googleapiclient.discovery", build=self._build),
        ]
        return {mod.__name__: mod for mod in mods}

    def names(self):
        return [call[0] for call in self.calls]


class Issue4AuthTests(unittest.TestCase):
    READONLY = ("https://www.googleapis.com/auth/youtube.readonly",)

    def setUp(self):
        self._sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self._sandbox.cleanup)
        self.sandbox = self._sandbox.name
        self.secrets = os.path.join(self.sandbox, "client_secret.json")
        with open(self.secrets, "w", encoding="utf-8") as f:
            json.dump({"installed": {"client_id": "fake"}}, f)
        self.token = os.path.join(self.sandbox, "config", "token.json")

    def install(self, google):
        patcher = mock.patch.dict(sys.modules, google.modules())
        patcher.start()
        self.addCleanup(patcher.stop)
        return google

    def cache_token(self, mode=0o600):
        os.makedirs(os.path.dirname(self.token), exist_ok=True)
        with open(self.token, "w", encoding="utf-8") as f:
            f.write("{}")
        os.chmod(self.token, mode)

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def credentials(self, **kwargs):
        with redirect_stderr(io.StringIO()) as err:
            creds = eys.get_credentials(self.secrets, self.token, **kwargs)
        return creds, err.getvalue()

    def test_scope_is_read_only(self):
        self.assertEqual(tuple(eys.SCOPES), self.READONLY)

    def test_first_run_runs_installed_app_flow_and_caches_token_0600(self):
        google = self.install(FakeGoogle())
        creds, _ = self.credentials()
        self.assertIs(creds, google.fresh)
        self.assertEqual(google.calls[0], ("flow", self.secrets, self.READONLY))
        self.assertEqual(google.calls[1], ("run_local_server", {"port": 0, "open_browser": True}))
        self.assertEqual(json.loads(eys.read_text(self.token)), {"refresh_token": "refresh-token"})
        self.assertEqual(self.mode(self.token), 0o600)
        self.assertEqual(self.mode(os.path.dirname(self.token)), 0o700)

    def test_no_browser_prints_url_instead(self):
        google = self.install(FakeGoogle())
        self.credentials(open_browser=False)
        self.assertEqual(google.calls[1], ("run_local_server", {"port": 0, "open_browser": False}))

    def test_valid_cached_token_needs_no_flow_and_no_client_secrets(self):
        self.cache_token()
        cached = FakeCreds(valid=True)
        google = self.install(FakeGoogle(cached=cached))
        os.unlink(self.secrets)
        creds, _ = self.credentials()
        self.assertIs(creds, cached)
        self.assertEqual(google.calls, [("load_token", self.token, self.READONLY)])
        self.assertEqual(eys.read_text(self.token), "{}")  # not rewritten

    def test_expired_token_is_refreshed_and_saved_0600_even_if_it_was_looser(self):
        self.cache_token(mode=0o644)
        cached = FakeCreds(valid=False, expired=True)
        google = self.install(FakeGoogle(cached=cached))
        creds, _ = self.credentials()
        self.assertIs(creds, cached)
        self.assertEqual((cached.refreshed, google.names()), (1, ["load_token"]))
        self.assertEqual(self.mode(self.token), 0o600)
        self.assertIn("refresh-token", eys.read_text(self.token))

    def test_dead_refresh_token_falls_back_to_the_flow(self):
        # README: testing-mode refresh tokens expire after about 7 days.
        self.cache_token()
        cached = FakeCreds(valid=False, expired=True, refresh_error=FakeGoogle.RefreshError("invalid_grant"))
        google = self.install(FakeGoogle(cached=cached))
        creds, err = self.credentials()
        self.assertIs(creds, google.fresh)
        self.assertEqual(google.names(), ["load_token", "flow", "run_local_server"])
        self.assertIn("invalid_grant", err)

    def test_token_without_refresh_token_falls_back_to_the_flow(self):
        self.cache_token()
        cached = FakeCreds(valid=False, expired=True, refresh_token=None)
        google = self.install(FakeGoogle(cached=cached))
        creds, _ = self.credentials()
        self.assertIs(creds, google.fresh)
        self.assertEqual(cached.refreshed, 0)

    def test_corrupt_token_file_falls_back_to_the_flow(self):
        self.cache_token()
        google = self.install(FakeGoogle(cached=ValueError("missing fields refresh_token")))
        creds, err = self.credentials()
        self.assertIs(creds, google.fresh)
        self.assertIn("ignoring unusable token", err)
        self.assertEqual(self.mode(self.token), 0o600)

    def test_network_error_while_refreshing_propagates(self):
        self.cache_token()
        cached = FakeCreds(valid=False, expired=True, refresh_error=OSError("no route to host"))
        google = self.install(FakeGoogle(cached=cached))
        with self.assertRaisesRegex(OSError, "no route"):
            self.credentials()
        self.assertEqual(google.names(), ["load_token"])  # no surprise browser flow
        self.assertEqual(eys.read_text(self.token), "{}")

    def test_missing_or_invalid_client_secrets_is_usage_error(self):
        self.install(FakeGoogle())
        with open(self.secrets, "w", encoding="utf-8") as f:
            f.write('{"web": {}}')
        with self.assertRaisesRegex(eys.UsageError, "cannot use OAuth client secrets"):
            self.credentials()
        os.unlink(self.secrets)
        with self.assertRaisesRegex(eys.UsageError, "client secrets not found"):
            self.credentials()
        self.assertFalse(os.path.exists(self.token))

    def test_save_token_bare_filename(self):
        old = os.getcwd()
        os.chdir(self.sandbox)
        try:
            eys._save_token("token.json", FakeCreds())
        finally:
            os.chdir(old)
        self.assertEqual(self.mode(os.path.join(self.sandbox, "token.json")), 0o600)

    # ---- through main(): flags, default token path, exit codes -------------

    def run_cli(self, *argv):
        home = os.path.join(self.sandbox, "home")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = eys.main(
                ["--output-dir", os.path.join(self.sandbox, "out"), "--date", TODAY, *argv],
                environ={"HOME": home, "XDG_CONFIG_HOME": os.path.join(self.sandbox, "xdg")},
                script_dir=os.path.join(self.sandbox, "bin"),
            )
        return code, out.getvalue(), err.getvalue()

    def test_cli_authenticates_builds_v3_client_and_fetches(self):
        google = self.install(FakeGoogle(resource=FakeYouTube(3)))
        code, out, err = self.run_cli("--client-secrets", self.secrets, "--no-browser")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("fetched 3 subscriptions", out)
        self.assertEqual(google.calls[1], ("run_local_server", {"port": 0, "open_browser": False}))
        service, version, kwargs = google.calls[2][1:]
        self.assertEqual((service, version, kwargs["credentials"]), ("youtube", "v3", google.fresh))
        default_token = os.path.join(self.sandbox, "xdg", "export-youtube-subscriptions", "token.json")
        self.assertEqual(self.mode(default_token), 0o600)  # $XDG_CONFIG_HOME default
        self.assertTrue(os.path.isfile(os.path.join(self.sandbox, "out", "youtubers.md")))

    def test_cli_token_flag_and_browser_default(self):
        google = self.install(FakeGoogle(resource=FakeYouTube(1)))
        code, _, _ = self.run_cli("--client-secrets", self.secrets, "--token", self.token)
        self.assertEqual(code, 0)
        self.assertEqual(google.calls[1], ("run_local_server", {"port": 0, "open_browser": True}))
        self.assertEqual(self.mode(self.token), 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.sandbox, "xdg")))

    def test_cli_exit_codes_for_auth_problems(self):
        out_dir = os.path.join(self.sandbox, "out")
        self.install(FakeGoogle())
        code, _, err = self.run_cli("--client-secrets", os.path.join(self.sandbox, "nope.json"))
        self.assertEqual(code, 2)
        self.assertIn("client secrets not found", err)
        self.assertFalse(os.path.exists(out_dir))

        self.cache_token()
        dead = FakeCreds(valid=False, expired=True, refresh_error=OSError("no route to host"))
        self.install(FakeGoogle(cached=dead))
        code, _, err = self.run_cli("--client-secrets", self.secrets, "--token", self.token)
        self.assertEqual(code, 1)
        self.assertIn("no route to host", err)
        self.assertFalse(os.path.exists(out_dir))


class Issue4PackagingTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(REPO_DIR, name), encoding="utf-8") as f:
            return f.read()

    def test_google_libraries_are_only_imported_inside_functions(self):
        tree = ast.parse(self.read("export_youtube_subscriptions.py"))
        top_level = []
        for node in tree.body:  # module scope only: imports inside functions are the lazy ones
            if isinstance(node, ast.Import):
                top_level += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                top_level.append(node.module or "")
        self.assertTrue(top_level)
        self.assertEqual([name for name in top_level if name.split(".")[0] in GOOGLE_TOP_LEVEL], [])
        lazy = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in GOOGLE_TOP_LEVEL
        ]
        self.assertIn("googleapiclient.discovery", lazy)
        self.assertIn("google_auth_oauthlib.flow", lazy)

    def test_offline_run_never_imports_google_libraries(self):
        script = (
            "import sys, export_youtube_subscriptions as eys\n"
            "code = eys.main(['--dry-run', '--from-json', sys.argv[1], '--output-dir', sys.argv[2]],"
            " environ={'HOME': sys.argv[2]}, script_dir=sys.argv[2])\n"
            f"loaded = sorted(m for m in sys.modules if m.split('.')[0] in {GOOGLE_TOP_LEVEL!r})\n"
            "sys.exit(code or (3 if loaded else 0))\n"
        )
        with tempfile.TemporaryDirectory() as d:
            fixture = os.path.join(d, "subs.json")
            with open(fixture, "w", encoding="utf-8") as f:
                json.dump([{"id": "UCa", "title": "A"}], f)
            done = subprocess.run(
                [sys.executable, "-c", script, fixture, d], cwd=REPO_DIR, capture_output=True, text=True
            )
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_entry_point_pep723_block_matches_requirements_txt(self):
        entry = self.read("export-youtube-subscriptions").splitlines()
        self.assertTrue(entry[0].startswith("#!") and "uv run --script" in entry[0], entry[0])
        start, end = entry.index("# /// script"), entry.index("# ///")
        block = "\n".join(line[2:] for line in entry[start + 1 : end])
        self.assertRegex(block, r'(?m)^requires-python = ">=3\.9"$')
        inline = re.search(r"(?s)^dependencies = \[(.*?)\]", block, re.M).group(1)
        inline_deps = re.findall(r'"([^"]+)"', inline)
        requirements = [
            line.strip()
            for line in self.read("requirements.txt").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(sorted(inline_deps), sorted(requirements))
        for package in ("google-api-python-client", "google-auth-oauthlib"):
            self.assertTrue(any(dep.startswith(package) for dep in inline_deps), package)


def _listed(count, prefix="UCk", missing_since=None):
    """``count`` channel lines with distinct ids, optionally already marked missing."""
    return [format_line(f"{prefix}{i:03d}", f"N{i}", missing_since) for i in range(count)]


def _kept(lines, dropped):
    """The subscription list that drops the first ``dropped`` channels of ``lines``."""
    return [ch(parse_line(line).id, parse_line(line).name) for line in lines[dropped:]]


class Issue5DropThresholdTests(unittest.TestCase):
    def test_default_is_the_larger_of_5_and_20_percent(self):
        self.assertEqual(tuple(eys.DROP_THRESHOLD), (5, 20))

    def test_limit_table(self):
        # (active channels, most that may be newly marked missing)
        for active, limit in ((0, 5), (1, 5), (5, 5), (25, 5), (29, 5), (30, 6), (34, 6), (35, 7), (100, 20),
                              (1000, 200)):
            with self.subTest(active=active):
                self.assertEqual(eys.DROP_THRESHOLD.limit(active), limit)

    def test_limit_matches_the_real_valued_rule(self):
        # "more than max(5, 20%)" over whole channels, with no float rounding slip.
        for active in range(0, 400):
            limit = eys.DROP_THRESHOLD.limit(active)
            for newly in (limit, limit + 1):
                self.assertEqual(newly > limit, newly > 5 and newly * 100 > active * 20, (active, newly))


class Issue5DropGuardMergeTests(unittest.TestCase):
    def drop(self, listed, dropped, **kwargs):
        lines = _listed(listed)
        # One brand-new channel keeps the subscription list non-empty, so the
        # empty-list guard is never the one that fires here.
        subs = _kept(lines, dropped) + [ch("UCnew", "New")]
        return merge({YOUTUBER: lines}, subs, TODAY, **kwargs)

    def test_exactly_at_the_threshold_is_allowed(self):
        for listed, dropped in ((30, 6), (100, 20), (35, 7)):
            with self.subTest(listed=listed):
                r = self.drop(listed, dropped)
                self.assertEqual(len(r.summary[YOUTUBER].newly_missing), dropped)

    def test_one_above_the_threshold_is_refused(self):
        for listed, dropped, limit in ((30, 7, 6), (100, 21, 20), (35, 8, 7)):
            with self.subTest(listed=listed), self.assertRaises(DropGuardError) as caught:
                self.drop(listed, dropped)
            e = caught.exception
            self.assertEqual((e.newly_missing, e.active, e.limit), (dropped, listed, limit))
            self.assertIn(f"{dropped} of {listed} listed channel(s)", str(e))
            self.assertIn(f"drop threshold of {limit} ", str(e))

    def test_percent_boundary_is_not_rounded_up(self):
        # 20% of 26 is 5.2: six is already "more than", five is the floor.
        self.assertEqual(len(self.drop(26, 5).summary[YOUTUBER].newly_missing), 5)
        with self.assertRaises(DropGuardError):
            self.drop(26, 6)

    def test_small_list_floor(self):
        # Up to 5 may always go, even when that is the whole list.
        for listed in (1, 3, 5):
            with self.subTest(listed=listed):
                r = self.drop(listed, listed)
                self.assertEqual(len(r.summary[YOUTUBER].newly_missing), listed)
        self.assertEqual(len(self.drop(8, 5).summary[YOUTUBER].newly_missing), 5)
        for listed in (6, 8, 25):
            with self.subTest(listed=listed), self.assertRaises(DropGuardError) as caught:
                self.drop(listed, 6)
            self.assertEqual(caught.exception.limit, 5)

    def test_allow_drop_bypasses(self):
        r = self.drop(30, 30, allow_drop=True)
        self.assertEqual(len(r.summary[YOUTUBER].newly_missing), 30)
        self.assertTrue(all(line.endswith(f"[missing since {TODAY}]") for line in r.contents[YOUTUBER][:30]))

    def test_allow_empty_implies_allow_drop(self):
        self.assertEqual(len(self.drop(30, 30, allow_empty=True).summary[YOUTUBER].newly_missing), 30)
        # ... and for the case it exists for: nothing came back at all.
        r = merge({YOUTUBER: _listed(30)}, [], TODAY, allow_empty=True)
        self.assertEqual(len(r.summary[YOUTUBER].newly_missing), 30)

    def test_empty_list_still_hits_the_empty_guard_first(self):
        with self.assertRaises(EmptySubscriptionsError):
            merge({YOUTUBER: _listed(30)}, [], TODAY)
        with self.assertRaises(EmptySubscriptionsError):  # --allow-drop is not --allow-empty
            merge({YOUTUBER: _listed(30)}, [], TODAY, allow_drop=True)

    def test_counts_across_both_files(self):
        musicians, youtubers = _listed(15, "UCm"), _listed(15, "UCy")
        existing = {MUSICIAN: musicians, YOUTUBER: youtubers}
        # 3 + 3 = 6 of 30: at the threshold, though each file alone lost 20%.
        r = merge(existing, _kept(musicians, 3) + _kept(youtubers, 3), TODAY)
        self.assertEqual([len(s.newly_missing) for s in r.summary.values()], [3, 3])
        with self.assertRaises(DropGuardError) as caught:  # 4 + 3 = 7 of 30
            merge(existing, _kept(musicians, 4) + _kept(youtubers, 3), TODAY)
        self.assertEqual((caught.exception.newly_missing, caught.exception.active), (7, 30))

    def test_already_missing_channels_do_not_count(self):
        active = _listed(30)
        old = _listed(200, "UCold", "2026-01-01")
        existing = {MUSICIAN: old, YOUTUBER: active}
        # Not as drops: 200 still-missing lines plus 6 new drops stays allowed.
        r = merge(existing, _kept(active, 6), TODAY)
        self.assertEqual(len(r.summary[MUSICIAN].still_missing), 200)
        self.assertEqual(len(r.summary[YOUTUBER].newly_missing), 6)
        # Nor in the base: 7 is refused against the 30 active channels, where
        # 20% of all 230 listed would have let 46 through.
        with self.assertRaises(DropGuardError) as caught:
            merge(existing, _kept(active, 7), TODAY)
        self.assertEqual((caught.exception.active, caught.exception.limit), (30, 6))

    def test_only_already_missing_channels_never_trips(self):
        old = _listed(50, "UCold", "2026-01-01")
        r = merge({YOUTUBER: old}, [ch("UCnew", "New")], TODAY)
        self.assertEqual(len(r.summary[YOUTUBER].still_missing), 50)
        self.assertEqual(r.contents[YOUTUBER][:50], old)

    def test_first_run_never_trips(self):
        subs = [ch(f"UCn{i:03d}", f"N{i}") for i in range(300)]
        for existing in ({}, {MUSICIAN: [], YOUTUBER: []}, {YOUTUBER: ["# YouTubers", "", "a note"]}):
            r = merge(existing, subs, TODAY)
            self.assertEqual(sum(len(s.added) for s in r.summary.values()), 300)
        self.assertIsNone(eys.check_drop(merge({}, [], TODAY).summary))

    def test_restored_and_added_channels_do_not_offset_drops(self):
        lines = _listed(30)
        subs = _kept(lines, 7) + [ch(f"UCn{i}", f"New {i}") for i in range(50)]
        with self.assertRaises(DropGuardError):
            merge({YOUTUBER: lines}, subs, TODAY)

    def test_restored_channels_are_not_in_the_base(self):
        # 29 active + 1 that comes back: the base is 29 (limit 5), not 30 (limit 6).
        active = _listed(29)
        old = _listed(1, "UCold", "2026-01-01")
        subs = _kept(active, 6) + [ch("UCold000", "N0")]
        with self.assertRaises(DropGuardError) as caught:
            merge({YOUTUBER: active + old}, subs, TODAY)
        self.assertEqual((caught.exception.active, caught.exception.limit), (29, 5))

    def test_duplicate_lines_count_once(self):
        lines = _listed(30)
        existing = {YOUTUBER: lines + lines[:6]}  # the 6 dropped ids each appear twice
        r = merge(existing, _kept(lines, 6) + [ch("UCnew", "New")], TODAY)
        self.assertEqual(len(r.summary[YOUTUBER].newly_missing), 6)

    def test_refusal_leaves_the_input_untouched(self):
        lines = _listed(30)
        existing = {YOUTUBER: list(lines)}
        with self.assertRaises(DropGuardError):
            merge(existing, _kept(lines, 7), TODAY)
        self.assertEqual(existing, {YOUTUBER: lines})

    def test_check_drop_reports_without_raising(self):
        r = self.drop(30, 7, allow_drop=True)
        tripped = eys.check_drop(r.summary)
        self.assertIsInstance(tripped, DropGuardError)
        self.assertEqual((tripped.newly_missing, tripped.active, tripped.limit), (7, 30, 6))
        self.assertIsNone(eys.check_drop(self.drop(30, 6).summary))


class Issue5DropGuardCliTests(unittest.TestCase):
    setUp, tearDown = CliTests.setUp, CliTests.tearDown
    run_cli = CliTests.run_cli
    snapshot = Issue4CliTests.snapshot

    def seed(self, directory, musicians=10, youtubers=20):
        """Write both files plus overrides.txt; return the channel lines."""
        lines = {MUSICIAN: _listed(musicians, "UCm"), YOUTUBER: _listed(youtubers, "UCy")}
        for cat, header in ((MUSICIAN, "# Musicians"), (YOUTUBER, "# YouTubers")):
            with open(os.path.join(directory, eys.FILENAMES[cat]), "w", encoding="utf-8") as f:
                f.write("\n".join([header, "", *lines[cat]]) + "\n")
        with open(os.path.join(directory, "overrides.txt"), "w", encoding="utf-8") as f:
            f.write("# none\n")
        return lines

    def subs(self, lines, dropped):
        """Drop ``dropped`` youtubers; keep every musician."""
        return lambda: _kept(lines[MUSICIAN], 0) + _kept(lines[YOUTUBER], dropped)

    def stat_snapshot(self, directory):
        out = {}
        for name in sorted(os.listdir(directory)):
            st = os.stat(os.path.join(directory, name))
            out[name] = (st.st_mtime_ns, st.st_ino, st.st_size)
        return out

    def test_refusal_exits_2_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            content, stats = self.snapshot(d), self.stat_snapshot(d)
            code, out, err = self.run_cli("--output-dir", d, "--date", TODAY, fetch=self.subs(lines, 7))
            self.assertEqual((code, out), (2, ""))
            self.assertIn("7 of 30 listed channel(s) would be newly marked missing", err)
            self.assertIn("drop threshold of 6 (the larger of 5 and 20%)", err)
            self.assertIn("--allow-drop", err)
            self.assertEqual(err.count("\n"), 1)
            # Same bytes, same inodes, same mtimes, and no temp file left behind.
            self.assertEqual(self.snapshot(d), content)
            self.assertEqual(self.stat_snapshot(d), stats)

    def test_refusal_never_calls_the_writer(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            with mock.patch.object(eys, "write_files") as writer, mock.patch.object(eys.os, "makedirs") as mkdirs:
                code, _, _ = self.run_cli("--output-dir", d, fetch=self.subs(lines, 30))
            self.assertEqual(code, 2)
            writer.assert_not_called()
            mkdirs.assert_not_called()

    def test_at_the_threshold_writes(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            code, out, err = self.run_cli("--output-dir", d, "--date", TODAY, fetch=self.subs(lines, 6))
            self.assertEqual((code, err), (0, ""))
            self.assertIn("youtubers.md: 20 channels, +0 added, 6 newly missing", out)
            self.assertNotIn("warning", out)
            self.assertEqual(eys.read_text(os.path.join(d, "youtubers.md")).count(f"[missing since {TODAY}]"), 6)

    def test_allow_drop_writes(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            code, out, err = self.run_cli(
                "--output-dir", d, "--allow-drop", "--date", TODAY, fetch=self.subs(lines, 7)
            )
            self.assertEqual((code, err), (0, ""))
            self.assertIn("youtubers.md: 20 channels, +0 added, 7 newly missing", out)
            self.assertNotIn("warning", out)
            self.assertEqual(eys.read_text(os.path.join(d, "youtubers.md")).count(f"[missing since {TODAY}]"), 7)

    def test_allow_empty_implies_allow_drop(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            code, out, err = self.run_cli(
                "--output-dir", d, "--allow-empty", "--date", TODAY, fetch=self.subs(lines, 7)
            )
            self.assertEqual((code, err), (0, ""))
            self.assertIn("7 newly missing", out)
            self.assertNotIn("warning", out)

    def test_allow_drop_does_not_imply_allow_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.seed(d)
            before = self.snapshot(d)
            code, _, err = self.run_cli("--output-dir", d, "--allow-drop", fetch=lambda: [])
            self.assertEqual(code, 2)
            self.assertIn("--allow-empty", err)
            self.assertEqual(self.snapshot(d), before)

    def test_dry_run_reports_and_warns(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            content, stats = self.snapshot(d), self.stat_snapshot(d)
            code, out, err = self.run_cli(
                "--output-dir", d, "--dry-run", "-v", "--date", TODAY, fetch=self.subs(lines, 7)
            )
            self.assertEqual((code, err), (0, ""))
            self.assertIn("youtubers.md: 20 channels, +0 added, 7 newly missing", out)
            self.assertIn(
                "warning: 7 of 30 listed channel(s) would be newly marked missing, more than the drop "
                "threshold of 6 (the larger of 5 and 20%); the API result may be partial; "
                "a real run would refuse without --allow-drop\n",
                out,
            )
            self.assertIn("dry run: no files written", out)
            for line in lines[YOUTUBER][:7]:  # -v names what would be marked
                self.assertIn(f"  youtubers.md newly missing: {parse_line(line).id}\n", out)
            self.assertEqual(self.snapshot(d), content)
            self.assertEqual(self.stat_snapshot(d), stats)

    def test_dry_run_warns_only_when_a_real_run_would_refuse(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            for argv, dropped in ((("--dry-run",), 6), (("--dry-run", "--allow-drop"), 7),
                                  (("--dry-run", "--allow-empty"), 7)):
                with self.subTest(argv=argv):
                    code, out, _ = self.run_cli("--output-dir", d, *argv, fetch=self.subs(lines, dropped))
                    self.assertEqual(code, 0)
                    self.assertIn(f"{dropped} newly missing", out)
                    self.assertNotIn("warning", out)

    def test_dry_run_empty_list_still_refuses(self):
        # The empty-list guard is unchanged: it exits 2 under --dry-run too.
        with tempfile.TemporaryDirectory() as d:
            self.seed(d)
            code, out, err = self.run_cli("--output-dir", d, "--dry-run", fetch=lambda: [])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("0 subscriptions", err)

    def test_first_run_with_many_channels(self):
        out_dir = os.path.join(self.sandbox, "fresh")
        subs = [ch(f"UCn{i:03d}", f"N{i}") for i in range(120)]
        code, out, err = self.run_cli("--output-dir", out_dir, fetch=lambda: subs)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("youtubers.md: 120 channels, +120 added, 0 newly missing", out)

    def test_already_missing_do_not_count_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            lines = self.seed(d)
            self.run_cli("--output-dir", d, "--date", TODAY, fetch=self.subs(lines, 6))
            # 6 are now marked: 24 active, floor 5. The same list again is a no-op...
            before = self.snapshot(d)
            code, out, _ = self.run_cli("--output-dir", d, "--date", LATER, fetch=self.subs(lines, 6))
            self.assertEqual(code, 0)
            self.assertIn("0 newly missing, 6 still missing", out)
            self.assertEqual(self.snapshot(d), before)
            # ...5 more go through, a 6th would not.
            code, _, err = self.run_cli("--output-dir", d, "--date", LATER, fetch=self.subs(lines, 12))
            self.assertEqual(code, 2)
            self.assertIn("6 of 24 listed channel(s)", err)
            self.assertEqual(self.snapshot(d), before)
            code, out, _ = self.run_cli("--output-dir", d, "--date", LATER, fetch=self.subs(lines, 11))
            self.assertEqual(code, 0)
            self.assertIn("5 newly missing, 6 still missing", out)

    def test_help_names_the_threshold(self):
        text = " ".join(eys.build_parser().format_help().split())
        self.assertIn("--allow-drop", text)
        self.assertIn("max(5, 20%)", text)
        self.assertIn("implies --allow-drop", text)

    def test_readme_documents_allow_drop(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("| `--allow-drop` |", readme)
        floor, percent = eys.DROP_THRESHOLD
        self.assertIn(f"max({floor}, {percent}%)", readme)


# --------------------------------------------------------------------------
# Issue #6: --apply-overrides moves existing lines between files
# --------------------------------------------------------------------------


class Issue6ApplyOverridesMergeTests(unittest.TestCase):
    def existing(self):
        return {
            MUSICIAN: ["# Musicians", "", f"{URL}UCband # Band", "a note", f"{URL}UCteach # Teacher"],
            YOUTUBER: ["# YouTubers", "", f"{URL}UCvlog # Vlog", "- http://youtube.com/channel/UCodd Odd"],
        }

    SUBS = [ch("UCband", "Band"), ch("UCteach", "Teacher"), ch("UCvlog", "Vlog"), ch("UCodd", "Odd")]

    def test_without_the_flag_nothing_moves_and_the_warning_prints(self):
        plain = merge(self.existing(), self.SUBS, TODAY)
        result = merge(self.existing(), self.SUBS, TODAY, {"UCteach": YOUTUBER})
        self.assertEqual(result.contents, plain.contents)
        self.assertEqual([file_summary.moved for file_summary in result.summary.values()], [[], []])
        self.assertEqual(len(result.warnings), 1)
        for needle in ("UCteach", "musicians.md", "youtubers.md", "by hand", "--apply-overrides"):
            self.assertIn(needle, result.warnings[0])

    def test_move_appends_to_the_end_and_keeps_every_other_line(self):
        for cid, name, src, dst in (("UCteach", "Teacher", MUSICIAN, YOUTUBER), ("UCvlog", "Vlog", YOUTUBER, MUSICIAN)):
            with self.subTest(cid=cid):
                plain = merge(self.existing(), self.SUBS, TODAY)
                result = merge(self.existing(), self.SUBS, TODAY, {cid: dst}, apply_overrides=True)
                line = f"{URL}{cid} # {name}"
                self.assertEqual(result.contents[src], [kept for kept in plain.contents[src] if kept != line])
                self.assertEqual(result.contents[dst], plain.contents[dst] + [line])
                self.assertEqual(result.warnings, [])
                self.assertEqual(result.summary[dst].moved, [cid])
                self.assertEqual(result.summary[src].moved, [])
                self.assertEqual((result.summary[src].total, result.summary[dst].total), (1, 3))

    def test_moved_line_lands_after_new_channels(self):
        subs = self.SUBS + [ch("UCnew", "New")]
        result = merge(self.existing(), subs, TODAY, {"UCteach": YOUTUBER}, apply_overrides=True)
        self.assertEqual(result.contents[YOUTUBER][-2:], [f"{URL}UCnew # New", f"{URL}UCteach # Teacher"])

    def test_moves_in_both_directions_keep_file_order(self):
        overrides = {"UCband": YOUTUBER, "UCteach": YOUTUBER, "UCodd": MUSICIAN}
        result = merge(self.existing(), self.SUBS, TODAY, overrides, apply_overrides=True)
        self.assertEqual(result.contents[MUSICIAN], ["# Musicians", "", "a note", f"{URL}UCodd # Odd"])
        self.assertEqual(
            result.contents[YOUTUBER],
            ["# YouTubers", "", f"{URL}UCvlog # Vlog", f"{URL}UCband # Band", f"{URL}UCteach # Teacher"],
        )
        self.assertEqual(result.summary[MUSICIAN].counts()["moved"], 1)
        self.assertEqual(result.summary[YOUTUBER].counts()["moved"], 2)

    def test_missing_marker_and_stored_name_are_kept(self):
        existing = self.existing()
        existing[MUSICIAN][4] = f"{URL}UCteach # Old Teacher Name [missing since 2026-01-02]"
        subs = [channel for channel in self.SUBS if channel.id != "UCteach"]
        result = merge(existing, subs, TODAY, {"UCteach": YOUTUBER}, apply_overrides=True)
        self.assertEqual(result.contents[YOUTUBER][-1], f"{URL}UCteach # Old Teacher Name [missing since 2026-01-02]")
        self.assertEqual(result.contents[MUSICIAN], existing[MUSICIAN][:4])
        self.assertEqual(result.summary[YOUTUBER].still_missing, ["UCteach"])
        self.assertEqual(result.summary[YOUTUBER].newly_missing, [])
        self.assertEqual(result.summary[MUSICIAN].still_missing, [])

    def test_moved_line_is_what_merge_would_have_written_in_place(self):
        cases = [
            # stored line, subscribed title (None: unsubscribed), expected moved line, stat list
            (f"{URL}UCa # Old", "New", f"{URL}UCa # New", "renamed"),
            (f"{URL}UCa # Band [missing since 2026-01-02]", "Band", f"{URL}UCa # Band", "restored"),
            (f"{URL}UCa # Band", None, f"{URL}UCa # Band [missing since {TODAY}]", "newly_missing"),
            (f"{URL}UCa", None, f"{URL}UCa [missing since {TODAY}]", "newly_missing"),
        ]
        for stored, title, moved, stat_name in cases:
            with self.subTest(stored=stored, title=title):
                existing = {MUSICIAN: [stored], YOUTUBER: [f"{URL}UCkeep # Keep"]}
                subs = [ch("UCkeep", "Keep")] + ([ch("UCa", title)] if title else [])
                in_place = merge(existing, subs, TODAY)
                result = merge(existing, subs, TODAY, {"UCa": YOUTUBER}, apply_overrides=True)
                self.assertEqual(in_place.contents[MUSICIAN], [moved])
                self.assertEqual(result.contents, {MUSICIAN: [], YOUTUBER: [f"{URL}UCkeep # Keep", moved]})
                self.assertEqual(getattr(result.summary[YOUTUBER], stat_name), ["UCa"])
                self.assertEqual(getattr(result.summary[MUSICIAN], stat_name), [])

    def test_second_run_changes_nothing(self):
        existing = self.existing()
        existing[YOUTUBER].append(f"{URL}UCgone # Gone [missing since 2026-01-02]")
        overrides = {"UCband": YOUTUBER, "UCodd": MUSICIAN, "UCgone": MUSICIAN, "UCvlog": YOUTUBER}
        first = merge(existing, self.SUBS, TODAY, overrides, apply_overrides=True)
        self.assertEqual(sum(len(file_summary.moved) for file_summary in first.summary.values()), 3)
        second = merge(first.contents, self.SUBS, LATER, overrides, apply_overrides=True)
        self.assertEqual(second.contents, first.contents)
        self.assertEqual([file_summary.moved for file_summary in second.summary.values()], [[], []])
        self.assertEqual(second.warnings, [])
        # ...and dropping the flag afterwards is just as quiet.
        third = merge(first.contents, self.SUBS, LATER, overrides)
        self.assertEqual((third.contents, third.warnings), (first.contents, []))

    def test_move_into_a_file_that_does_not_exist_yet_adds_the_header(self):
        existing = {MUSICIAN: [], YOUTUBER: ["# YouTubers", "", f"{URL}UCa # Band"]}
        result = merge(existing, [ch("UCa", "Band")], TODAY, {"UCa": MUSICIAN}, apply_overrides=True)
        self.assertEqual(
            result.contents, {MUSICIAN: ["# Musicians", "", f"{URL}UCa # Band"], YOUTUBER: ["# YouTubers", ""]}
        )

    def test_duplicated_id_moves_every_misplaced_line_and_is_stable(self):
        for src, dst in ((MUSICIAN, YOUTUBER), (YOUTUBER, MUSICIAN)):
            with self.subTest(src=src):
                existing = {src: [f"{URL}UCa # One", "note", f"{URL}UCa # Two"], dst: [f"{URL}UCa # Three"]}
                first = merge(existing, [], TODAY, {"UCa": dst}, allow_empty=True, apply_overrides=True)
                self.assertEqual(first.contents[src], ["note"])
                self.assertEqual([parse_line(line).name for line in first.contents[dst]], ["Three", "One", "Two"])
                self.assertEqual(first.summary[dst].total + first.summary[src].total, 1)
                self.assertEqual(first.summary[dst].moved, ["UCa"])
                # The "(also in ...)" file is where the first line ends up, not
                # where it was read (PR #13 review, test gap 1).
                src_name, dst_name = eys.FILENAMES[src], eys.FILENAMES[dst]
                if src == MUSICIAN:  # musicians.md is read first: "One" is seen first, in youtubers.md
                    expected = [
                        f"duplicate channel UCa in {src_name} (also in {dst_name}); moved to {dst_name}",
                        f"duplicate channel UCa in {dst_name} (also in {dst_name}); left in place",
                    ]
                else:  # "Three" is seen first, in place in musicians.md
                    expected = [f"duplicate channel UCa in {src_name} (also in {dst_name}); moved to {dst_name}"] * 2
                self.assertEqual(first.warnings, expected)
                second = merge(first.contents, [], TODAY, {"UCa": dst}, allow_empty=True, apply_overrides=True)
                self.assertEqual(second.contents, first.contents)
                self.assertEqual(second.summary[dst].moved, [])

    # Issue #14, finding 2: a moved line identical to one already in the
    # destination is dropped; a line that differs in any byte is kept.

    def test_identical_line_already_in_the_destination_is_dropped(self):
        for src, dst in ((MUSICIAN, YOUTUBER), (YOUTUBER, MUSICIAN)):
            with self.subTest(src=src):
                # What a one-way move whose second write failed leaves behind.
                line = f"{URL}UCa # Band [missing since 2026-01-02]"
                existing = {src: ["note", line], dst: [line, f"{URL}UCb # Other"]}
                result = merge(existing, [ch("UCb", "Other")], TODAY, {"UCa": dst}, apply_overrides=True)
                self.assertEqual(result.contents, {src: ["note"], dst: [line, f"{URL}UCb # Other"]})
                self.assertEqual(result.summary[dst].moved, ["UCa"])
                self.assertEqual((result.summary[dst].total, result.summary[src].total), (2, 0))
                dropped = (
                    f"UCa is already in {eys.FILENAMES[dst]} as an identical line; "
                    "dropped the copy that was moved there"
                )
                self.assertEqual(result.warnings, [dropped])
                self.assertEqual(result.moves, [(src, dst, line)])
                second = merge(result.contents, [ch("UCb", "Other")], TODAY, {"UCa": dst}, apply_overrides=True)
                self.assertEqual((second.contents, second.warnings), (result.contents, []))

    def test_a_line_that_differs_in_any_byte_is_never_dropped(self):
        stored = f"{URL}UCa # Band [missing since 2026-01-02]"
        for variant in (
            f"{URL}UCa # Band [missing since 2026-01-03]",  # other date
            f"{URL}UCa # Band. [missing since 2026-01-02]",  # other name
            f"{URL}UCa # band [missing since 2026-01-02]",  # case
            f"{URL}UCa # Band",  # newly missing: rendered with today's date
        ):
            with self.subTest(variant=variant):
                existing = {MUSICIAN: [variant], YOUTUBER: [stored]}
                result = merge(existing, [], TODAY, {"UCa": YOUTUBER}, allow_empty=True, apply_overrides=True)
                self.assertEqual(result.contents[MUSICIAN], [])
                self.assertEqual(len(result.contents[YOUTUBER]), 2)
                self.assertEqual(result.contents[YOUTUBER][0], stored)
                self.assertTrue(any("duplicate channel UCa" in warning for warning in result.warnings))
                self.assertFalse(any("dropped" in warning for warning in result.warnings))

    def test_only_a_copy_already_in_place_is_dropped_not_other_moved_copies(self):
        # Two identical misplaced lines both move: nothing in the destination
        # held the line before, so neither is dropped (no-delete rule).
        line = f"{URL}UCa # Band"
        existing = {MUSICIAN: [line, line], YOUTUBER: ["# YouTubers"]}
        result = merge(existing, [ch("UCa", "Band")], TODAY, {"UCa": YOUTUBER}, apply_overrides=True)
        self.assertEqual(result.contents[YOUTUBER], ["# YouTubers", line, line])

    def test_moves_list_every_moved_line_with_both_files(self):
        overrides = {"UCband": YOUTUBER, "UCodd": MUSICIAN}
        result = merge(self.existing(), self.SUBS, TODAY, overrides, apply_overrides=True)
        self.assertEqual(
            result.moves, [(MUSICIAN, YOUTUBER, f"{URL}UCband # Band"), (YOUTUBER, MUSICIAN, f"{URL}UCodd # Odd")]
        )
        self.assertEqual(merge(self.existing(), self.SUBS, TODAY, overrides).moves, [])

    # Issue #14, finding 1: the order of the writes.

    def test_plan_writes_without_moves_is_one_step_per_file(self):
        result = merge(self.existing(), self.SUBS, TODAY)
        self.assertEqual(eys.plan_writes(result), [(cat, result.contents[cat]) for cat in eys.CATEGORIES])

    def test_plan_writes_one_way_writes_the_gaining_file_first(self):
        for src, dst, cid in ((MUSICIAN, YOUTUBER, "UCteach"), (YOUTUBER, MUSICIAN, "UCvlog")):
            with self.subTest(src=src):
                result = merge(self.existing(), self.SUBS, TODAY, {cid: dst}, apply_overrides=True)
                self.assertEqual(eys.plan_writes(result), [(dst, result.contents[dst]), (src, result.contents[src])])

    def test_plan_writes_two_way_keeps_leaving_lines_until_the_other_file_has_them(self):
        overrides = {"UCband": YOUTUBER, "UCteach": YOUTUBER, "UCodd": MUSICIAN}
        result = merge(self.existing(), self.SUBS, TODAY, overrides, apply_overrides=True)
        leaving = [f"{URL}UCband # Band", f"{URL}UCteach # Teacher"]
        self.assertEqual(
            eys.plan_writes(result),
            [
                (MUSICIAN, result.contents[MUSICIAN] + leaving),
                (YOUTUBER, result.contents[YOUTUBER]),
                (MUSICIAN, result.contents[MUSICIAN]),
            ],
        )

    def test_moved_channels_are_not_drops(self):
        # 12 active musicians all move: none is "newly missing", so the guard
        # (limit max(5, 20%)) stays quiet even with allow_drop off.
        lines = _listed(12, "UCm")
        overrides = {parse_line(line).id: YOUTUBER for line in lines}
        existing = {MUSICIAN: ["# Musicians", ""] + lines, YOUTUBER: []}
        result = merge(existing, _kept(lines, 0), TODAY, overrides, apply_overrides=True)
        self.assertEqual(len(result.summary[YOUTUBER].moved), 12)
        self.assertEqual(result.summary[YOUTUBER].newly_missing + result.summary[MUSICIAN].newly_missing, [])
        self.assertEqual((result.summary[MUSICIAN].total, result.summary[YOUTUBER].total), (0, 12))
        self.assertIsNone(eys.check_drop(result.summary))

    def test_drop_guard_counts_the_same_with_and_without_moves(self):
        # 30 active channels, limit 6. Moving channels must change neither side
        # of the comparison: 6 real drops pass, 7 are refused, moved or not.
        musicians, youtubers = _listed(10, "UCm"), _listed(20, "UCy")
        existing = {MUSICIAN: musicians, YOUTUBER: youtubers}
        overrides = {parse_line(line).id: MUSICIAN for line in youtubers[:10]}  # 5 of them get dropped too
        for dropped, refused in ((6, False), (7, True)):
            with self.subTest(dropped=dropped):
                subs = _kept(musicians, 0) + _kept(youtubers, dropped)
                plain = eys.check_drop(merge(existing, subs, TODAY, overrides, allow_drop=True).summary)
                moved = merge(existing, subs, TODAY, overrides, allow_drop=True, apply_overrides=True)
                tripped = eys.check_drop(moved.summary)
                self.assertEqual(tripped is not None, refused)
                self.assertEqual(plain is not None, refused)
                if refused:
                    self.assertEqual((tripped.newly_missing, tripped.active), (plain.newly_missing, plain.active))
                    self.assertEqual((tripped.newly_missing, tripped.active, tripped.limit), (7, 30, 6))
                    with self.assertRaises(DropGuardError):
                        merge(existing, subs, TODAY, overrides, apply_overrides=True)

    def test_already_missing_moved_channel_stays_out_of_the_active_base(self):
        # 10 active + 20 old markers: limit is max(5, 2) = 5. Moving the 20
        # marked lines must not turn them into "active" and loosen the guard.
        active, marked = _listed(10, "UCm"), _listed(20, "UCy", "2026-01-02")
        overrides = {parse_line(line).id: MUSICIAN for line in marked}
        existing = {MUSICIAN: active, YOUTUBER: marked}
        result = merge(existing, _kept(active, 6), TODAY, overrides, allow_drop=True, apply_overrides=True)
        tripped = eys.check_drop(result.summary)
        self.assertEqual((tripped.newly_missing, tripped.active, tripped.limit), (6, 10, 5))

    def test_empty_list_guard_still_fires_and_counts_each_channel_once(self):
        existing = {MUSICIAN: [f"{URL}UCa # A", f"{URL}UCb # B"], YOUTUBER: [f"{URL}UCc # C"]}
        with self.assertRaises(EmptySubscriptionsError) as caught:
            merge(existing, [], TODAY, {"UCa": YOUTUBER}, apply_overrides=True)
        self.assertIn("list 3 channel(s)", str(caught.exception))


class Issue6ApplyOverridesCliTests(unittest.TestCase):
    setUp, tearDown, run_cli = CliTests.setUp, CliTests.tearDown, CliTests.run_cli
    snapshot = Issue4CliTests.snapshot

    MUSICIANS = f"# Musicians\n\n{URL}UCband # Band\nnote\n{URL}UCteach # Teacher [missing since 2026-01-02]\n"
    YOUTUBERS = f"# YouTubers\n\n{URL}UCvlog # Vlog\n"

    def seed(self, directory):
        for name, text in (
            ("musicians.md", self.MUSICIANS),
            ("youtubers.md", self.YOUTUBERS),
            ("overrides.txt", "UCteach youtuber  # teaches, doesn't play\n"),
        ):
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        fixture = os.path.join(self.sandbox, "subs.json")
        with open(fixture, "w", encoding="utf-8") as handle:
            json.dump([{"id": "UCband", "title": "Band"}, {"id": "UCvlog", "title": "Vlog"}], handle)
        return ("--output-dir", directory, "--from-json", fixture, "--date", TODAY)

    def test_flag_is_in_help(self):
        self.assertIn("--apply-overrides", eys.build_parser().format_help())

    def test_without_the_flag_output_and_files_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = self.seed(directory)
            before = self.snapshot(directory)
            code, out, err = self.run_cli(*argv)
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(self.snapshot(directory), before)
            self.assertIn("warning: override says UCteach is a youtuber but it is listed in musicians.md", out)
            self.assertNotIn("moved in", out)
            self.assertIn(
                "musicians.md: 2 channels, +0 added, 0 newly missing, 1 still missing, 0 restored, 0 renamed\n", out
            )

    def test_move_then_second_run_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = self.seed(directory)
            code, out, err = self.run_cli(*argv, "--apply-overrides", "-v")
            self.assertEqual((code, err), (0, ""))
            after = self.snapshot(directory)
            self.assertEqual(after["musicians.md"], f"# Musicians\n\n{URL}UCband # Band\nnote\n".encode())
            self.assertEqual(
                after["youtubers.md"],
                (self.YOUTUBERS + f"{URL}UCteach # Teacher [missing since 2026-01-02]\n").encode(),
            )
            self.assertIn("musicians.md: 1 channels, +0 added, 0 newly missing, 0 still missing, "
                          "0 restored, 0 renamed, 0 moved in\n", out)
            self.assertIn("youtubers.md: 2 channels, +0 added, 0 newly missing, 1 still missing, "
                          "0 restored, 0 renamed, 1 moved in\n", out)
            self.assertIn("  youtubers.md moved in: UCteach\n", out)
            self.assertNotIn("warning", out)

            mtimes = {name: os.stat(os.path.join(directory, name)).st_mtime_ns for name in after}
            code, out, err = self.run_cli(*argv, "--apply-overrides")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(self.snapshot(directory), after)
            self.assertEqual({name: os.stat(os.path.join(directory, name)).st_mtime_ns for name in after}, mtimes)
            self.assertEqual(out.count("0 moved in"), 2)

    def test_dry_run_reports_the_move_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = self.seed(directory)
            before = self.snapshot(directory)
            with mock.patch.object(eys, "write_files") as writer:
                code, out, err = self.run_cli(*argv, "--apply-overrides", "--dry-run")
            self.assertEqual((code, err), (0, ""))
            writer.assert_not_called()
            self.assertEqual(self.snapshot(directory), before)
            self.assertIn("renamed, 0 moved in\nyoutubers.md: 2 channels", out)
            self.assertIn("0 renamed, 1 moved in\n", out)
            self.assertIn("dry run: no files written", out)

    def test_moves_do_not_trip_the_drop_guard_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            lines = _listed(12, "UCm")
            with open(os.path.join(directory, "musicians.md"), "w", encoding="utf-8") as handle:
                handle.write("\n".join(["# Musicians", "", *lines]) + "\n")
            with open(os.path.join(directory, "overrides.txt"), "w", encoding="utf-8") as handle:
                handle.write("".join(f"{parse_line(line).id} youtuber\n" for line in lines))
            code, out, err = self.run_cli(
                "--output-dir", directory, "--date", TODAY, "--apply-overrides", fetch=lambda: _kept(lines, 0)
            )
            self.assertEqual((code, err), (0, ""))
            self.assertIn("youtubers.md: 12 channels, +0 added, 0 newly missing", out)
            self.assertIn("12 moved in", out)
            self.assertEqual(self.snapshot(directory)["musicians.md"], b"# Musicians\n\n")
            expected = ("\n".join(["# YouTubers", "", *lines]) + "\n").encode()
            self.assertEqual(self.snapshot(directory)["youtubers.md"], expected)

    def test_empty_list_guard_wins_over_the_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = self.seed(directory)
            before = self.snapshot(directory)
            code, out, err = self.run_cli("--output-dir", directory, "--apply-overrides", fetch=lambda: [])
            self.assertEqual((code, out), (2, ""))
            self.assertIn("files list 3 channel(s)", err)
            self.assertEqual(self.snapshot(directory), before)
            del argv

    # Issue #14: a failed write during a move never loses a line.

    TEACH = f"{URL}UCteach # Teacher [missing since 2026-01-02]"  # unsubscribed: only the file has it
    VLOG = f"{URL}UCvlog # Vlog"
    SCENARIOS = {
        "musicians to youtubers": ("UCteach youtuber\n", [TEACH]),
        "youtubers to musicians": ("UCvlog musician\n", [VLOG]),
        "both ways": ("UCteach youtuber\nUCvlog musician\n", [TEACH, VLOG]),
    }

    def seed_scenario(self, directory, overrides):
        argv = self.seed(directory)
        with open(os.path.join(directory, "overrides.txt"), "w", encoding="utf-8") as handle:
            handle.write(overrides)
        return argv

    def run_failing(self, argv, target, op_name, fail_at):
        """Run with ``target.op_name`` raising ENOSPC on its ``fail_at``-th call."""
        real_op, calls = getattr(target, op_name), []

        def failing(*args, **kwargs):
            calls.append(args)
            if len(calls) == fail_at:
                raise OSError(28, "No space left on device")
            return real_op(*args, **kwargs)

        with mock.patch.object(target, op_name, side_effect=failing):
            code, out, err = self.run_cli(*argv, "--apply-overrides")
        failed = calls[fail_at - 1] if len(calls) >= fail_at else None
        return code, out, err, failed

    def test_injected_failure_at_any_step_never_leaves_a_moved_line_in_neither_file(self):
        steps = ((eys.tempfile, "mkstemp"), (eys.os, "fsync"), (eys.os, "chmod"), (eys.os, "replace"))
        for scenario, (overrides, moved) in self.SCENARIOS.items():
            with tempfile.TemporaryDirectory() as directory:
                argv = self.seed_scenario(directory, overrides)
                self.assertEqual(self.run_cli(*argv, "--apply-overrides")[0], 0)
                clean = self.snapshot(directory)
            failed_replaces = set()
            for (target, op_name), fail_at in [(step, count) for step in steps for count in range(1, 5)]:
                with self.subTest(scenario=scenario, step=op_name, fail_at=fail_at), \
                        tempfile.TemporaryDirectory() as directory:
                    argv = self.seed_scenario(directory, overrides)
                    before = self.snapshot(directory)
                    code, _, err, failed = self.run_failing(argv, target, op_name, fail_at)
                    after = self.snapshot(directory)
                    self.assertEqual([name for name in os.listdir(directory) if name.endswith(".tmp")], [])
                    files = after["musicians.md"].decode() + after["youtubers.md"].decode()
                    for line in moved:
                        self.assertIn(line + "\n", files)  # never in neither file
                    if code == 0:  # no such call, or a swallowed directory fsync
                        self.assertEqual(after, clean)
                        continue
                    self.assertEqual(code, 2)
                    self.assertIn("error: cannot write", err)
                    for line in moved:  # the full line, so it can be restored by hand
                        self.assertIn(line, err)
                    if op_name != "replace":  # failed while staging: nothing replaced
                        self.assertEqual(after, before)
                    else:
                        failed_replaces.add(os.path.basename(failed[1]))
                    # A rerun with the flag finishes the move: one copy of each line.
                    code, out, err = self.run_cli(*argv, "--apply-overrides")
                    self.assertEqual((code, err), (0, ""))
                    self.assertEqual(self.snapshot(directory), clean)
            # The replace of the first and of the second file both failed once.
            self.assertEqual(failed_replaces, {"musicians.md", "youtubers.md"}, scenario)

    def test_one_way_second_write_fails_then_rerun_leaves_exactly_one_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = self.seed(directory)
            code, _, err, failed = self.run_failing(argv, eys.os, "replace", 2)
            self.assertEqual((code, os.path.basename(failed[1])), (2, "musicians.md"))
            self.assertIn(f"error: cannot write {os.path.join(directory, 'musicians.md')!r}", err)
            self.assertIn(f"error:   musicians.md -> youtubers.md: {self.TEACH}\n", err)
            after = self.snapshot(directory)
            self.assertEqual(after["musicians.md"], self.MUSICIANS.encode())  # untouched: still has the line
            self.assertEqual(after["youtubers.md"].decode().count(self.TEACH), 1)

            code, out, err = self.run_cli(*argv, "--apply-overrides")
            self.assertEqual((code, err), (0, ""))
            files = self.snapshot(directory)
            self.assertEqual((files["musicians.md"] + files["youtubers.md"]).decode().count("UCteach"), 1)
            self.assertIn(self.TEACH, files["youtubers.md"].decode())
            self.assertIn("warning: UCteach is already in youtubers.md as an identical line", out)
            self.assertNotIn("duplicate channel", out)

    def test_write_failure_without_moves_lists_no_lines(self):
        # PR #13 review, test gap 2: the move listing is only for runs that move.
        with tempfile.TemporaryDirectory() as directory:
            argv = self.seed_scenario(directory, "")
            with open(os.path.join(directory, "youtubers.md"), "w", encoding="utf-8") as handle:
                handle.write(f"{URL}UCvlog # Old Vlog Name\n")  # a rename, so the run writes
            code, _, err, _ = self.run_failing(argv, eys.os, "replace", 1)
            self.assertEqual(code, 2)
            self.assertIn("error: cannot write", err)
            self.assertNotIn("--apply-overrides did not finish", err)
            self.assertNotIn("->", err)

    def test_readme_documents_the_flag(self):
        with open(os.path.join(REPO_DIR, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("| `--apply-overrides` |", readme)
        self.assertIn("moved in", readme)


# ---- Issue #11: review follow-ups from #4 ----------------------------------


class Issue11AuthPortTests(unittest.TestCase):
    """--auth-port reaches flow.run_local_server, using the FakeGoogle stand-ins."""

    READONLY = Issue4AuthTests.READONLY
    setUp = Issue4AuthTests.setUp
    install = Issue4AuthTests.install
    credentials = Issue4AuthTests.credentials
    run_cli = Issue4AuthTests.run_cli

    def test_get_credentials_passes_the_port(self):
        google = self.install(FakeGoogle())
        self.credentials(open_browser=False, auth_port=8765)
        self.assertEqual(google.calls[1], ("run_local_server", {"port": 8765, "open_browser": False}))

    def test_cli_auth_port_reaches_run_local_server(self):
        google = self.install(FakeGoogle(resource=FakeYouTube(1)))
        code, _, err = self.run_cli("--client-secrets", self.secrets, "--no-browser", "--auth-port", "8765")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(google.calls[1], ("run_local_server", {"port": 8765, "open_browser": False}))

    def test_cli_default_auth_port_is_0_random(self):
        self.assertEqual(eys.build_parser().parse_args([]).auth_port, 0)
        google = self.install(FakeGoogle(resource=FakeYouTube(1)))
        code, _, _ = self.run_cli("--client-secrets", self.secrets)
        self.assertEqual(code, 0)
        self.assertEqual(google.calls[1], ("run_local_server", {"port": 0, "open_browser": True}))

    def test_out_of_range_or_non_numeric_port_is_usage_error(self):
        for bad in ("-1", "65536", "http", "", "80.5"):
            with self.subTest(bad), redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    eys.build_parser().parse_args(["--auth-port", bad])
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("--auth-port", err.getvalue())
        for good in ("0", "1", "8765", "65535"):
            self.assertEqual(eys.build_parser().parse_args(["--auth-port", good]).auth_port, int(good))

    def test_unreadable_existing_file_fails_before_auth(self):
        # Real API path (no fetch/youtube injection): the Google stand-ins must
        # never be asked for a token, a flow or a client.
        google = self.install(FakeGoogle(resource=FakeYouTube(1)))
        out_dir = os.path.join(self.sandbox, "out")
        os.makedirs(out_dir)
        with open(os.path.join(out_dir, "youtubers.md"), "wb") as f:
            f.write(b"\xff not utf-8\n")
        code, out, err = self.run_cli("--client-secrets", self.secrets, "--no-browser")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("cannot read the existing files", err)
        self.assertEqual(google.calls, [])


class Issue11CliTests(unittest.TestCase):
    setUp, tearDown, run_cli = CliTests.setUp, CliTests.tearDown, CliTests.run_cli

    def test_unreadable_existing_file_fails_before_fetch(self):
        unreadable = {
            "not utf-8": lambda path: _write_bytes(path, b"\xff\xfe\n"),
            "a directory": os.mkdir,
        }
        for name, make in unreadable.items():
            for filename in ("musicians.md", "youtubers.md"):
                with self.subTest(name, file=filename), tempfile.TemporaryDirectory() as d:
                    make(os.path.join(d, filename))
                    fetch = mock.Mock(return_value=[ch("UCa", "A")])
                    code, out, err = self.run_cli("--output-dir", d, fetch=fetch)
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("cannot read the existing files", err)
                    fetch.assert_not_called()

    def test_from_json_is_not_read_when_existing_file_is_unreadable(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "musicians.md"))
            with mock.patch.object(eys, "load_json_channels") as load_json:
                code, _, _ = self.run_cli("--output-dir", d, "--from-json", os.path.join(d, "subs.json"))
            self.assertEqual(code, 2)
            load_json.assert_not_called()

    def test_help_has_an_example_epilog(self):
        parser = eys.build_parser()
        self.assertTrue(parser.epilog)
        text = parser.format_help()
        self.assertIn("example", text.lower())
        self.assertRegex(text, r"export-youtube-subscriptions --output-dir \S+")
        self.assertIn("--no-browser --auth-port 8765", text)
        # The example lines keep their layout instead of being re-wrapped.
        self.assertRegex(text, r"\n  export-youtube-subscriptions --output-dir")

    def test_help_documents_auth_port(self):
        self.assertIn("--auth-port PORT", eys.build_parser().format_help())

    def test_dead_skipped_client_secrets_test_is_gone(self):
        self.assertFalse(hasattr(CliTests, "test_missing_client_secrets"))


class Issue11ReadmeTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO_DIR, "README.md"), encoding="utf-8") as f:
            self.readme = f.read()

    def exit_codes_section(self):
        start = self.readme.index("Exit codes:")
        return " ".join(self.readme[start:self.readme.index("**Drop guard.**", start)].split())

    def test_exit_2_lists_every_case(self):
        section = self.exit_codes_section()
        for case in (
            "Google client libraries are not installed",
            "`--from-json`",
            "`--date`",
            "missing or invalid OAuth client secrets",
            "existing `musicians.md` or `youtubers.md` can't be read",
            "a file can't be written",
            "half applied",
        ):
            self.assertIn(case, section)

    def test_headless_sign_in_is_documented(self):
        self.assertIn("| `--auth-port PORT` | `0`", self.readme)
        self.assertIn("--no-browser --auth-port 8765", self.readme)
        self.assertIn("ssh -L 8765:localhost:8765", self.readme)


if __name__ == "__main__":
    unittest.main()
