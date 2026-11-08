import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import board
import check_ownership
import team as roster

TEAM = {
    "repo": "o/r",
    "board_issue": 1,
    "members": [
        {"role": "lead", "github": "Boss", "paths": ["agents/lead/"]},
        {"role": "data", "github": "Dana", "paths": ["agents/data/"]},
        {"role": "eval", "github": None, "paths": ["agents/eval/"]},
    ],
}
ALLOWED = {"boss", "dana", board.BOT}


def comment(author, body):
    return {"author": author, "body": body}


class Mentions(unittest.TestCase):
    def test_finds_tags_case_insensitively(self):
        self.assertEqual(board.mentions("hi @Dana and @boss-2."), {"dana", "boss-2"})

    def test_ignores_emails_paths_code_and_quotes(self):
        body = "a@dana.com x/@dana `@dana`\n```\n@dana\n```\n> @dana said"
        self.assertEqual(board.mentions(body), set())


class Delivery(unittest.TestCase):
    def test_delivers_a_teammates_tag(self):
        self.assertTrue(board.is_for_me(comment("Boss", "@dana go"), "Dana", ALLOWED))

    def test_ignores_untagged(self):
        self.assertFalse(board.is_for_me(comment("Boss", "@boss go"), "Dana", ALLOWED))

    def test_ignores_outsiders(self):
        self.assertFalse(board.is_for_me(comment("mallory", "@dana go"), "Dana", ALLOWED))

    def test_ignores_self_unless_asked(self):
        mine = comment("Dana", "@dana go")
        self.assertFalse(board.is_for_me(mine, "Dana", ALLOWED))
        self.assertTrue(board.is_for_me(mine, "Dana", ALLOWED, include_self=True))

    def test_delivers_ci(self):
        self.assertTrue(board.is_for_me(comment(board.BOT, "@dana fix"), "Dana", ALLOWED))


class Targets(unittest.TestCase):
    def test_resolves_roles_and_logins(self):
        self.assertEqual(board.resolve_targets(TEAM, "data,boss", "x"), ["Dana", "Boss"])

    def test_all_skips_me_and_unassigned(self):
        self.assertEqual(board.resolve_targets(TEAM, "all", "Boss"), ["Dana"])

    def test_rejects_unknown_and_unassigned(self):
        for target in ("nobody", "eval"):
            with self.assertRaises(SystemExit):
                board.resolve_targets(TEAM, target, "Boss")


class Ownership(unittest.TestCase):
    def test_folder_owner(self):
        self.assertEqual(roster.owner_of(TEAM, "agents/data/x.py")["role"], "data")

    def test_lead_owns_the_rest(self):
        self.assertEqual(roster.owner_of(TEAM, "RULES.md")["role"], "lead")

    def test_prefix_needs_the_whole_folder_name(self):
        self.assertEqual(roster.owner_of(TEAM, "agents/database/x.py")["role"], "lead")

    def test_violations(self):
        paths = ["agents/data/a.py", "RULES.md", "agents/eval/b.py"]
        problems = check_ownership.violations(TEAM, "dana", paths)
        self.assertEqual(len(problems), 2)
        self.assertIn("RULES.md", problems[0])
        self.assertIn("nobody yet", problems[1])


class OpenPaths(unittest.TestCase):
    team = {
        "open_paths": ["web/"],
        "members": [
            {"role": "lead", "github": "Boss", "paths": ["agents/lead/", "web/apps/held/"]},
            {"role": "data", "github": "Dana", "paths": ["agents/data/"]},
        ],
    }

    def problems(self, login, path):
        return check_ownership.violations(self.team, login, [path])

    def test_anyone_on_the_roster_may_change_an_open_folder(self):
        self.assertEqual(self.problems("dana", "web/apps/bank/x.astro"), [])
        self.assertEqual(self.problems("Boss", "web/package.json"), [])

    def test_a_member_keeps_a_folder_held_inside_it(self):
        self.assertEqual(len(self.problems("Dana", "web/apps/held/x.astro")), 1)
        self.assertEqual(self.problems("Boss", "web/apps/held/x.astro"), [])

    def test_outsiders_are_still_refused(self):
        self.assertEqual(len(self.problems("mallory", "web/apps/bank/x.astro")), 1)

    def test_prefix_needs_the_whole_folder_name(self):
        self.assertEqual(len(self.problems("Dana", "website/x.html")), 1)

    def test_other_folders_stay_closed(self):
        self.assertEqual(len(self.problems("Dana", "agents/lead/x.py")), 1)
        self.assertEqual(len(self.problems("Dana", "tools/team.py")), 1)

    def test_team_without_open_paths(self):
        self.assertFalse(roster.is_open(TEAM, "web/x.astro"))

    def test_only_held_folders_are_denied_to_claude(self):
        rules = roster.edit_denies(self.team, "data")
        self.assertIn("Edit(/web/apps/held/**)", rules)
        self.assertNotIn("Edit(/web/**)", rules)

    def test_setup_clears_a_deny_left_from_the_old_owner(self):
        settings = {"permissions": {"deny": ["Edit(/web/**)", "Edit(/mine/**)"]}}
        result = roster.with_rules(settings, [], ["Edit(/web/apps/held/**)"], ["web/"])
        self.assertEqual(result["permissions"]["deny"],
                         ["Edit(/mine/**)", "Edit(/web/apps/held/**)"])


class QuietMentions(unittest.TestCase):
    def test_wraps_handles_so_they_tag_nobody(self):
        quiet = board.quiet_mentions("thanks @Dana, cc @boss-2")
        self.assertEqual(quiet, "thanks `@Dana`, cc `@boss-2`")
        self.assertEqual(board.mentions(quiet), set())

    def test_quotes_decorators_whole(self):
        self.assertEqual(board.quiet_mentions("added @lru_cache"), "added `@lru_cache`")

    def test_leaves_code_and_emails_alone(self):
        text = "use `@dana` or\n```\n@property\n```\nmail a@dana.com"
        self.assertEqual(board.quiet_mentions(text), text)


class Since(unittest.TestCase):
    def test_asks_for_two_extra_minutes(self):
        self.assertEqual(board.since_param("2026-11-07T10:00:30Z"), "2026-11-07T09:58:30Z")

    def test_no_cursor_means_everything(self):
        self.assertIsNone(board.since_param(None))


class Newer(unittest.TestCase):
    def post(self, cid, author, body, ts="2026-11-07T10:00:00Z"):
        return {"id": cid, "author": author, "body": body, "created_at": ts, "url": "u"}

    def test_skips_seen_posts_and_moves_the_cursor(self):
        posts = [self.post(5, "Boss", "@dana old"), self.post(6, "Boss", "@dana new"),
                 self.post(7, "Dana", "@boss mine"), self.post(8, "mallory", "@dana hi")]
        with mock.patch.object(board, "fetch_comments", return_value=posts):
            mine, cursor = board.newer(TEAM, "Dana", {"id": 5, "ts": "x"}, False)
        self.assertEqual([c["id"] for c in mine], [6])
        self.assertEqual(cursor["id"], 8)

    def test_one_line_names_the_type(self):
        line = board.one_line(self.post(9, "Boss", "**[NEED]** from `lead` to @Dana\n\nx"))
        self.assertIn("NEED from Boss (#9)", line)
        self.assertNotIn("\n", line)


class CommitChecks(unittest.TestCase):
    def test_only_lead_is_lead(self):
        self.assertTrue(check_ownership.is_lead(TEAM, "boss"))
        self.assertFalse(check_ownership.is_lead(TEAM, "Dana"))

    def test_oversized(self):
        sizes = {"agents/data/small.csv": 1024, "agents/data/big.parquet": 11 * 2**20}
        problems = check_ownership.oversized(sizes)
        self.assertEqual(len(problems), 1)
        self.assertIn("big.parquet", problems[0])

    def test_finds_keys_in_added_lines_only(self):
        key = "AIza" + "B" * 35
        diff = "\n".join([
            "diff --git a/agents/data/client.py b/agents/data/client.py",
            "--- a/agents/data/client.py",
            "+++ b/agents/data/client.py",
            "@@ -1 +1,3 @@",
            f"-OLD = '{key}'",
            f"+KEY = '{key}'",
            "+++ not a header, just a line starting with ++",
            'diff --git "a/agents/data/my notes.md" "b/agents/data/my notes.md"',
            '+++ "b/agents/data/my notes.md"',
            "@@ -0,0 +1 @@",
            "+nvapi-" + "x1" * 15,
        ])
        problems = check_ownership.secrets_in(diff)
        self.assertEqual(len(problems), 2)
        self.assertIn("agents/data/client.py has what looks like a Google API key", problems[0])
        self.assertIn("my notes.md has what looks like an NVIDIA API key", problems[1])
        self.assertNotIn(key, "".join(problems))

    def test_ordinary_hyphenated_words_are_not_keys(self):
        diff = ("diff --git a/x b/x\n+++ b/x\n@@ -0,0 +1 @@\n"
                "+risk-adjusted-sharpe-ratio-computation and sk-learn-pipeline-for-the-model")
        self.assertEqual(check_ownership.secrets_in(diff), [])

    def test_openai_style_key(self):
        diff = "diff --git a/x b/x\n+++ b/x\n@@ -0,0 +1 @@\n+k = 'sk-proj-" + "a1" * 12 + "'"
        self.assertIn("OpenAI-style", check_ownership.secrets_in(diff)[0])


class LocalSettings(unittest.TestCase):
    def test_denies_edits_outside_my_folder(self):
        rules = roster.edit_denies(TEAM, "data")
        self.assertIn("Edit(/agents/lead/**)", rules)
        self.assertIn("Edit(/agents/eval/**)", rules)
        self.assertIn("Edit(/RULES.md)", rules)
        self.assertNotIn("Edit(/agents/data/**)", rules)

    def test_lead_may_edit_its_files(self):
        rules = roster.edit_denies(TEAM, "lead")
        self.assertEqual(rules, ["Edit(/agents/data/**)", "Edit(/agents/eval/**)"])

    def test_keeps_my_own_rules_and_replaces_old_generated_ones(self):
        settings = {"permissions": {"allow": ["Bash(make *)", roster.ALLOW[0]],
                                    "deny": ["Edit(/agents/old/**)", "Bash(rm *)"]},
                    "model": "opus"}
        result = roster.with_rules(settings, roster.ALLOW, ["Edit(/agents/new/**)"])
        self.assertEqual(result["permissions"]["allow"], ["Bash(make *)"] + roster.ALLOW)
        self.assertEqual(result["permissions"]["deny"], ["Bash(rm *)", "Edit(/agents/new/**)"])
        self.assertEqual(result["model"], "opus")

    def test_writes_and_rewrites_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.local.json"
            path.write_text(json.dumps({"permissions": {"allow": ["Bash(make *)"]}}))
            roster.write_local_settings(TEAM, "data", path)
            roster.write_local_settings(TEAM, "data", path)
            saved = json.loads(path.read_text())["permissions"]
            self.assertEqual(saved["allow"].count("Bash(make *)"), 1)
            self.assertEqual(saved["allow"].count(roster.ALLOW[0]), 1)
            self.assertIn("Read(/.env)", saved["deny"])
            path.write_text("{not json")
            with self.assertRaises(SystemExit):
                roster.write_local_settings(TEAM, "data", path)


if __name__ == "__main__":
    unittest.main()
