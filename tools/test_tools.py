import unittest

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


if __name__ == "__main__":
    unittest.main()
