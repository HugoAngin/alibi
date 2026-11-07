#!/usr/bin/env python3
"""Message Board client. Agents talk to each other through one pinned issue."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

import team as roster

BOT = "github-actions[bot]"
CURSOR_DIR = roster.ROOT / ".board"
MENTION = re.compile(r"(?<![\w/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)
# Wider than MENTION so that a decorator such as @lru_cache is quoted whole.
HANDLE = re.compile(r"(?<![\w/.-])@[\w-]+")
KIND = re.compile(r"\*\*\[([A-Z]+)\]\*\*")
# Ask GitHub for a little more than we need, so two posts in the same second
# are never skipped. Comment ids remove the overlap.
SINCE_MARGIN = timedelta(minutes=2)


def die(message: str) -> None:
    print(f"board: {message}", file=sys.stderr)
    raise SystemExit(1)


def mentions(body: str) -> set[str]:
    """Return lowercased logins tagged in body, ignoring code and quotes."""
    text = CODE.sub("", body)
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return {m.lower() for m in MENTION.findall(text)}


def quiet_mentions(text: str) -> str:
    """Put @handles outside code in backticks, so only --to tags anyone."""
    def quiet(part: str) -> str:
        return HANDLE.sub(lambda m: f"`{m.group(0)}`", part)

    parts, last = [], 0
    for block in CODE.finditer(text):
        parts += [quiet(text[last:block.start()]), block.group(0)]
        last = block.end()
    return "".join(parts) + quiet(text[last:])


def since_param(ts: str | None) -> str | None:
    """Turn a cursor timestamp into the 'since' value to send to GitHub."""
    if not ts:
        return None
    moment = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") - SINCE_MARGIN
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_targets(team: dict, targets: str, me: str) -> list[str]:
    """Turn a comma-separated list of roles, logins or 'all' into logins."""
    members = team["members"]
    logins: list[str] = []
    for target in (t.strip() for t in targets.split(",") if t.strip()):
        if target.lower() == "all":
            logins += [m["github"] for m in members
                       if m["github"] and not roster.same_login(m["github"], me)]
            continue
        match = next((m for m in members
                      if m["role"] == target.lower() or roster.same_login(m["github"], target)),
                     None)
        if match is None:
            die(f"unknown recipient '{target}'")
        if not match["github"]:
            die(f"role '{match['role']}' has no GitHub username in team.json")
        logins.append(match["github"])
    return list(dict.fromkeys(logins))


def is_for_me(comment: dict, me: str, allowed: set[str], include_self: bool = False) -> bool:
    author = comment["author"].lower()
    if author not in allowed:
        return False
    if author == me.lower() and not include_self:
        return False
    return me.lower() in mentions(comment["body"])


def board_path(team: dict) -> str:
    if not team.get("board_issue"):
        die("team.json has no board_issue")
    return f"repos/{team['repo']}/issues/{team['board_issue']}/comments"


def fetch_comments(team: dict, since: str | None = None) -> list[dict]:
    url = f"{board_path(team)}?per_page=100"
    since = since_param(since)
    if since:
        url += f"&since={since}"
    jq = ".[] | {id, author: .user.login, created_at, body, url: .html_url}"
    done = subprocess.run(["gh", "api", "--paginate", "--jq", jq, url],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        raise RuntimeError(done.stderr.strip() or "gh api failed")
    decoder = json.JSONDecoder()
    comments, position, text = [], 0, done.stdout
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            return sorted(comments, key=lambda c: c["id"])
        comment, position = decoder.raw_decode(text, position)
        comments.append(comment)


def read_cursor(me: str) -> dict:
    try:
        return json.loads((CURSOR_DIR / f"{me.lower()}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"id": 0, "ts": None}


def write_cursor(me: str, cursor: dict) -> None:
    CURSOR_DIR.mkdir(exist_ok=True)
    (CURSOR_DIR / f"{me.lower()}.json").write_text(json.dumps(cursor))


def allowed_authors(team: dict) -> set[str]:
    return {m["github"].lower() for m in team["members"] if m["github"]} | {BOT}


def newer(team: dict, me: str, cursor: dict, include_self: bool) -> tuple[list[dict], dict]:
    """Return tags for me posted after cursor, and the cursor that follows them."""
    new = [c for c in fetch_comments(team, cursor["ts"]) if c["id"] > cursor["id"]]
    if new:
        cursor = {"id": new[-1]["id"], "ts": new[-1]["created_at"]}
    allowed = allowed_authors(team)
    return [c for c in new if is_for_me(c, me, allowed, include_self)], cursor


def unread(team: dict, me: str, include_self: bool) -> tuple[list[dict], dict]:
    """Return unread tags for me and the cursor to save once they are shown."""
    return newer(team, me, read_cursor(me), include_self)


def show(comment: dict) -> None:
    print(f"--- #{comment['id']} from {comment['author']} at {comment['created_at']}")
    print(comment["body"])
    print(comment["url"])
    print()


def one_line(comment: dict) -> str:
    """A single line for Claude Code's Monitor tool, which wakes on each line."""
    kind = KIND.search(comment["body"])
    label = kind.group(1) if kind else "POST"
    return (f"Board: {label} from {comment['author']} (#{comment['id']}). "
            f"Run board.py inbox to read it.")


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=roster.ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        die(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def require_published(member: dict) -> None:
    """A teammate cannot use work that has not reached the remote."""
    for prefix in member["paths"]:
        if git("status", "--porcelain", "--", prefix):
            die(f"uncommitted changes in {prefix}. Commit and push before posting done.")
    git("fetch", "-q", "origin")
    if git("rev-list", "--count", "origin/main..HEAD") != "0":
        die("you have unpushed commits. Push before posting done.")


def cmd_whoami(args: argparse.Namespace, team: dict, me: str) -> None:
    member = roster.member_for_login(team, me)
    if member is None:
        die(f"{me} is not in team.json. Ask lead to add you.")
    print(f"login:  {me}\nrole:   {member['role']}\nfolder: {member['paths'][0]}")
    print(f"brief:  {member['paths'][0]}BRIEF.md")
    print(f"board:  https://github.com/{team['repo']}/issues/{team['board_issue']}")


def cmd_post(args: argparse.Namespace, team: dict, me: str) -> None:
    member = roster.member_for_login(team, me)
    if member is None:
        die(f"{me} is not in team.json")
    kind = args.type
    if kind == "fyi" and args.to:
        die("fyi posts tag nobody. Use done, need or blocked to tag someone.")
    if kind != "fyi" and not args.to:
        die(f"{kind} posts need --to")
    if kind == "done":
        require_published(member)
    tags = resolve_targets(team, args.to or "", me)
    if kind != "fyi" and not tags:
        die("--to resolved to nobody")
    header = f"**[{kind.upper()}]** from `{member['role']}`"
    if tags:
        header += " to " + " ".join(f"@{login}" for login in tags)
    message = quiet_mentions(args.message)
    body = f"{header}\n\n{message}\n\n`{git('rev-parse', '--short', 'HEAD')}`"
    done = subprocess.run(["gh", "api", "--method", "POST", board_path(team),
                           "-f", f"body={body}", "--jq", ".html_url"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode:
        die(done.stderr.strip())
    print(done.stdout.strip())


def cmd_inbox(args: argparse.Namespace, team: dict, me: str) -> None:
    messages, cursor = unread(team, me, args.include_self)
    for message in messages:
        show(message)
    if not messages:
        print("No new messages.")
    write_cursor(me, cursor)


def cmd_wait(args: argparse.Namespace, team: dict, me: str) -> None:
    started = time.monotonic()
    while True:
        try:
            messages, cursor = unread(team, me, args.include_self)
            if messages:
                for message in messages:
                    show(message)
                sys.stdout.flush()
                write_cursor(me, cursor)
                return
            write_cursor(me, cursor)
        except (RuntimeError, ValueError) as error:
            print(f"board: retrying after error: {error}", file=sys.stderr)
        if args.timeout and time.monotonic() - started >= args.timeout:
            print("Timed out with no new messages.")
            raise SystemExit(2)
        time.sleep(args.interval)


def cmd_watch(args: argparse.Namespace, team: dict, me: str) -> None:
    """Print one line per new tag and keep going. Leaves the inbox unread."""
    started, cursor, failing = time.monotonic(), read_cursor(me), False
    while True:
        try:
            messages, cursor = newer(team, me, cursor, args.include_self)
            for message in messages:
                print(one_line(message), flush=True)
            failing = False
        except (RuntimeError, ValueError) as error:
            if not failing:
                print(f"board: cannot reach GitHub, still trying: {error}", file=sys.stderr)
            failing = True
        if args.timeout and time.monotonic() - started >= args.timeout:
            raise SystemExit(2)
        time.sleep(args.interval)


def cmd_read(args: argparse.Namespace, team: dict, me: str) -> None:
    comments = fetch_comments(team)
    if args.mine:
        allowed = allowed_authors(team)
        comments = [c for c in comments if is_for_me(c, me, allowed)]
    for comment in comments[-args.n:]:
        show(comment)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("whoami", help="show your role and folder").set_defaults(run=cmd_whoami)

    post = commands.add_parser("post", help="post to the board")
    post.add_argument("type", choices=["done", "need", "blocked", "fyi"])
    post.add_argument("message")
    post.add_argument("--to", help="roles, usernames or 'all', comma-separated")
    post.set_defaults(run=cmd_post)

    inbox = commands.add_parser("inbox", help="show unread tags and return")
    inbox.add_argument("--include-self", action="store_true", help="for testing")
    inbox.set_defaults(run=cmd_inbox)

    wait = commands.add_parser("wait", help="block until you are tagged")
    wait.add_argument("--interval", type=int, default=20, help="seconds between checks")
    wait.add_argument("--timeout", type=int, default=0, help="give up after N seconds and exit 2")
    wait.add_argument("--include-self", action="store_true", help="for testing")
    wait.set_defaults(run=cmd_wait)

    watch = commands.add_parser("watch", help="one line per new tag, for the Monitor tool")
    watch.add_argument("--interval", type=int, default=20, help="seconds between checks")
    watch.add_argument("--timeout", type=int, default=0, help="stop after N seconds and exit 2")
    watch.add_argument("--include-self", action="store_true", help="for testing")
    watch.set_defaults(run=cmd_watch)

    read = commands.add_parser("read", help="show recent posts")
    read.add_argument("-n", type=int, default=20)
    read.add_argument("--mine", action="store_true", help="only posts that tag you, read or not")
    read.set_defaults(run=cmd_read)
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    team = roster.load()
    try:
        args.run(args, team, roster.current_login())
    except RuntimeError as error:
        die(str(error))


if __name__ == "__main__":
    main()
