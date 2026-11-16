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


def _read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


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
            {"channels": 6, "added": 1, "newly_missing": 1, "still_missing": 1, "restored": 1, "renamed": 1},
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


if __name__ == "__main__":
    unittest.main()
