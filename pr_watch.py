#!/usr/bin/env python3
"""PR watch: which open GitHub PRs need something from you, and what.

Two sections, one line per PR:
  YOU      what to do (reply to a review, answer threads, fix a conflict or checks, review), with the question quoted
  NOT YOU  why not (waiting on whom, someone else is rebasing, threads you may resolve), same reasons grouped

It reads the open PRs you authored or review: comments, reviews and review threads from other people after your
last activity, merge conflicts, failing checks, and rebase notes ("I'm rebasing #652 and #654", "taking the rebase
of this one, please don't push"), wherever they were posted. ● marks PRs with something first seen in the last
12 hours (--new-hours).

Repositories, first match wins: --repo (repeatable), herdr's config folder for this plugin (config.json), or the
GitHub repository of the current directory. Write it with: pr_watch.py --setup owner/name [owner/name ...]

Usage:
  pr_watch.py                 one report
  pr_watch.py --watch 300     report every 300 s; bell and macOS notification when something new needs you
  pr_watch.py --json          the analysed items as JSON
Needs the GitHub CLI (`gh`) logged in. Read-only: it never writes to GitHub.
"""
import argparse, json, os, re, select, shutil, subprocess, sys, termios, textwrap, threading, time, tty, webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ID = "ferretorres.pr-watch"


def config_path():
    """config.json in herdr's config folder for this plugin: outside the plugin folder, so sharing or updating the
    plugin never touches your repositories. HERDR_PLUGIN_CONFIG_DIR is set inside a plugin pane."""
    if os.environ.get("PR_WATCH_CONFIG"):
        return os.environ["PR_WATCH_CONFIG"]
    folder = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "herdr", "plugins", "config", PLUGIN_ID)
    return os.path.join(folder, "config.json")


CONFIG = config_path()
STATE_DIR = os.environ.get("PR_WATCH_STATE_DIR") or os.path.expanduser("~/.cache/herdr-pr-watch")

QUERY = """
query($owner: String!, $name: String!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url isDraft updatedAt mergeable reviewDecision headRefName baseRefName
        author { __typename login ... on User { name } }
        reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login name } } } }
        commits(last: 40) { nodes { commit {
          oid authoredDate committedDate messageHeadline
          author { user { login name } name }
          statusCheckRollup { state }
        } } }
        comments(last: 40) { nodes { author { __typename login ... on User { name } } createdAt body url } }
        reviews(last: 40) { nodes { author { __typename login ... on User { name } } state submittedAt body url } }
        reviewThreads(last: 60) { nodes {
          isResolved isOutdated path
          comments(last: 20) { nodes { author { __typename login ... on User { name } } createdAt body url } }
        } }
      }
    }
  }
}
"""


@dataclass
class Item:
    kind: str      # comment | review | thread | changes | commit | checks | conflict | review-request | re-review
    who: str
    when: str
    text: str
    url: str
    new: bool = False
    first: bool = False  # first seen in this run
    lane: str = "code"   # reply | code | resolve | hold | review | info

    @property
    def key(self):
        return f"{self.kind}|{self.url}|{self.when}"


@dataclass
class PullRequest:
    number: int
    title: str
    url: str
    role: str      # author | reviewer
    status: str    # ACTION | WAITING | READY
    summary: str
    draft: bool
    items: list = field(default_factory=list)
    decision: str = ""
    turn: str = ""       # whose turn it is in the conversation
    names: dict = field(default_factory=dict)  # login -> display name
    base: str = ""
    last_commit_at: str = ""
    repo: str = ""       # owner/name
    waiting_on: list = field(default_factory=list)  # display names, when you spoke last
    last_speaker: str = ""                          # login of the last person who wrote
    checks: str = ""                                # SUCCESS | FAILURE | PENDING | NONE | …
    mergeable: str = ""                             # MERGEABLE | CONFLICTING | UNKNOWN
    head: str = ""                                  # branch name
    snoozed: bool = False                           # hidden from YOU until new activity
    agents: list = field(default_factory=list)      # herdr panes working on it


def gh(*args, timeout=60):
    try:
        return subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        sys.exit("pr_watch needs the GitHub CLI: install it (https://cli.github.com), then run `gh auth login`.")


CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")  # keeps \t and \n; drops ESC, so no terminal codes


def clean(node):
    """Make GitHub data safe and comparable, once, where it enters:
    - text: control characters removed (a title can't send escape codes to the terminal);
    - times (*At, *Date): normalised to UTC "…Z", so string comparisons are time comparisons;
    - bot authors: login gets "[bot]", so is_bot() knows them (GraphQL logins have no suffix)."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if isinstance(v, str) and (k.endswith("At") or k.endswith("Date")) and v:
                v = parse_time(v).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            out[k] = clean(v)
        if out.get("__typename") == "Bot" and out.get("login") and not out["login"].endswith("[bot]"):
            out["login"] += "[bot]"
        return out
    if isinstance(node, list):
        return [clean(v) for v in node]
    return CONTROL.sub("", node) if isinstance(node, str) else node


class GitHubError(RuntimeError):
    pass


def gh_graphql(owner, name):
    out = gh("api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"name={name}")
    if out.returncode:
        raise GitHubError(f"gh api graphql failed: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout).get("data") or {}
    if not data.get("repository"):
        raise GitHubError(f"can't read {owner}/{name}: check the name and your access")
    return clean(data)


def login(node):
    return ((node or {}).get("author") or {}).get("login") or "ghost"


def is_bot(who):
    return who.endswith("[bot]") or who in ("github-actions", "dependabot", "copilot-pull-request-reviewer")


def one_line(text, n=110):
    return re.sub(r"\s+", " ", text or "").strip()[:n]


QUESTION = re.compile(r"\?|\b(could|can|would|should) you\b|\bplease\b|\b(why|what|which|how)\b", re.I)
# a request without a question mark: "three things left", "the description still says", "this needs"
REQUEST = re.compile(r"\b(left|still (says|has|needs|misses|shows|fails)|needs?|missing|should|must|instead|wrong"
                     r"|not sure|let me know|thoughts)\b", re.I)
ACK = re.compile(r"^\W*(thanks|thank you|ok|okay|lgtm|looks good|good|great|right|fine|perfect|nice|done|resolved"
                 r"|yes|yep|sure|agreed|sounds good|makes sense)\b", re.I)


def collect_names(pr):
    names = {}
    nodes = ([pr] + pr["comments"]["nodes"] + pr["reviews"]["nodes"]
             + [c for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]])
    for n in nodes:
        a = n.get("author") or {}
        if a.get("login") and a.get("name"):
            names[a["login"]] = a["name"]
    for n in pr["reviewRequests"]["nodes"]:
        r = n.get("requestedReviewer") or {}
        if r.get("login") and r.get("name"):
            names[r["login"]] = r["name"]
    return {k: short_name(v) for k, v in names.items()}


def short_name(name):
    """'Lee, Ana (Example Corp)' -> 'Ana'. First names keep the lines short."""
    name = re.sub(r"\s*\(.*?\)", "", name).strip()
    last, _, first = name.partition(", ")
    return (first or name).split()[0]


def lane_of(item, items):
    """Where an item lands: does it need a message from you, code work, a click, or nothing."""
    if item.kind in ("comment", "review", "thread") and NO_PUSH.search(item.text):
        later = [i for i in items if i.who == item.who and i.when > item.when]
        return "info" if later else "hold"   # a later message from the same person usually lifts it
    if item.kind == "thread":
        # ponytail: keyword heuristics; a reply that only acknowledges needs a resolve click, not a message
        body = item.text.split(": ", 1)[-1]
        return "resolve" if ACK.search(body) and not QUESTION.search(body) else "reply"
    asks = lambda text: QUESTION.search(text) or REQUEST.search(text)
    if item.kind == "review":
        if item.text.startswith("approved"):
            return "info"
        if item.text.startswith("changes requested"):
            return "code"
        return "reply" if asks(item.text) else "read"
    if item.kind == "comment":
        return "reply" if asks(item.text) else "read"   # a status update ("rebased onto dev") needs no answer
    if item.kind in ("review-request", "re-review"):
        return "review"
    return "code"   # commit by someone else, conflict, failing checks


def parse_time(iso):
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)  # GitHub times are UTC


def age(iso):
    s = (datetime.now(timezone.utc) - parse_time(iso)).total_seconds()
    return f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 86400 else f"{int(s // 86400)}d"


def local_time(iso):
    return parse_time(iso).astimezone().strftime("%a %H:%M")


def analyse(pr, me, bots):
    commits = [c["commit"] for c in pr["commits"]["nodes"]]
    comments = pr["comments"]["nodes"]
    reviews = [r for r in pr["reviews"]["nodes"] if r.get("submittedAt")]
    threads = pr["reviewThreads"]["nodes"]
    others = lambda who: who != me and (bots or not is_bot(who))
    commit_login = lambda c: (((c.get("author") or {}).get("user") or {}).get("login")
                              or (c.get("author") or {}).get("name") or "")

    # authoredDate for my own commits: a rebase by someone else rewrites committedDate, not authorship
    mine = ([c["authoredDate"] for c in commits if commit_login(c) == me]
            + [c["createdAt"] for c in comments if login(c) == me]
            + [r["submittedAt"] for r in reviews if login(r) == me]
            + [c["createdAt"] for t in threads for c in t["comments"]["nodes"] if login(c) == me])
    my_last = max(mine, default="")
    role = "author" if login(pr) == me else "reviewer"
    items = []

    for c in comments:
        if others(login(c)) and c["createdAt"] > my_last:
            items.append(Item("comment", login(c), c["createdAt"], one_line(c["body"], 5000), c["url"]))
    for r in reviews:
        who = login(r)
        if others(who) and r["submittedAt"] > my_last and (r["body"] or r["state"] != "COMMENTED"):
            label = {"CHANGES_REQUESTED": "changes requested", "APPROVED": "approved"}.get(r["state"], "review")
            items.append(Item("review", who, r["submittedAt"], f"{label}: {one_line(r['body'], 5000)}".rstrip(": "),
                              r["url"]))
    for t in threads:  # an unresolved thread waits on you whenever its last word is someone else's
        last = (t["comments"]["nodes"] or [None])[-1]
        if last and not t["isResolved"] and others(login(last)):
            where = t.get("path") or ""
            items.append(Item("thread", login(last), last["createdAt"],
                              f"{where}{' (outdated)' if t['isOutdated'] else ''}: {one_line(last['body'], 5000)}",
                              last["url"]))
    my_last_commit = max((c["authoredDate"] for c in commits if commit_login(c) == me), default="")
    for c in commits:
        who = commit_login(c)
        if who != me and c["committedDate"] > (my_last_commit if role == "author" else my_last):
            kind = "commit" if role == "author" else "re-review"
            items.append(Item(kind, who, c["committedDate"], one_line(c["messageHeadline"]),
                              f"{pr['url']}/commits/{c['oid']}"))

    checks = ((commits[-1].get("statusCheckRollup") or {}).get("state") if commits else None) or "NONE"
    if role == "author" and checks in ("FAILURE", "ERROR"):
        items.append(Item("checks", "ci", commits[-1]["committedDate"], f"checks {checks.lower()} on the last commit",
                          f"{pr['url']}/checks"))
    if role == "author" and pr["mergeable"] == "CONFLICTING":
        items.append(Item("conflict", "git", pr["updatedAt"], "merge conflict with the base branch", pr["url"]))
    requested = [n["requestedReviewer"].get("login") for n in pr["reviewRequests"]["nodes"] if n["requestedReviewer"]]
    if role == "reviewer" and me in requested:
        items.append(Item("review-request", login(pr), pr["updatedAt"], "your review is requested", pr["url"]))

    items.sort(key=lambda i: i.when)
    names = collect_names(pr)
    talk = sorted([(c["createdAt"], login(c)) for c in comments]
                  + [(r["submittedAt"], login(r)) for r in reviews if r["body"]]
                  + [(c["createdAt"], login(c)) for t in threads for c in t["comments"]["nodes"]])
    talk = [(t, who) for t, who in talk if bots or not is_bot(who)]
    for i in items:
        i.lane = lane_of(i, items)
        if i.lane == "reply" and i.kind == "thread" and i.when < my_last:
            i.lane = "posted"  # you wrote or pushed after it: handled until someone writes again
    turn, waiting_names, last_who = "", [], ""
    if talk:
        when, who = talk[-1]
        last_who = who
        name = lambda l: names.get(l) or l
        if who == me:
            waiting = [n["requestedReviewer"].get("login") for n in pr["reviewRequests"]["nodes"]
                       if n["requestedReviewer"] and n["requestedReviewer"].get("login") != me]
            others_talking = sorted({w for _, w in talk if w != me})
            waiting_names = [name(l) for l in (waiting or others_talking)]
            on = ", ".join(waiting_names) or "nobody yet"
            turn = f"you spoke last {age(when)} ago · waiting on {on}"
        else:
            turn = f"{name(who)} spoke last {age(when)} ago"
    decision = pr["reviewDecision"] or "NO REVIEW"
    if any(needs_you(i) for i in items):
        status = "ACTION"
    elif (role == "author" and decision == "APPROVED" and checks in ("SUCCESS", "NONE")
          and pr["mergeable"] == "MERGEABLE" and not pr["isDraft"]):
        status = "READY"
    else:
        status = "WAITING"
    summary = f"{decision.lower().replace('_', ' ')} · checks {checks.lower()} · {pr['mergeable'].lower()}"
    return PullRequest(pr["number"], pr["title"], pr["url"], role, status, summary, pr["isDraft"], items, decision,
                       turn, names, pr.get("baseRefName") or "",
                       max((c["committedDate"] for c in commits), default=""), "", waiting_names, last_who,
                       checks, pr["mergeable"], pr.get("headRefName") or "")


def needs_you(item):
    """An item that is an action for you (not a note: someone else's commits, a thread to resolve, a read)."""
    return item.lane in ("reply", "review") or (item.lane == "code" and item.kind != "commit")


def mark_new(prs, seen, now, hours):
    """seen: item key -> first-seen epoch (updated in place). NEW = first seen within `hours`.
    Returns how many items appeared for the first time in this run."""
    fresh = 0
    for pr in prs:
        for i in pr.items:
            if i.key not in seen:
                seen[i.key], fresh, i.first = now, fresh + needs_you(i), True
            i.new = now - seen[i.key] < hours * 3600
    return fresh


def snooze_path(repo):
    return os.path.join(STATE_DIR, repo.replace("/", "__") + ".snooze.json")


def load_snoozes(repo):
    try:
        return {int(k): set(v) for k, v in json.load(open(snooze_path(repo))).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_snoozes(repo, snoozes):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(snooze_path(repo), "w") as f:
        json.dump({str(k): sorted(v) for k, v in snoozes.items()}, f)


def apply_snoozes(prs, snoozes):
    """A snoozed PR stays snoozed while all its items were already there when you snoozed it.
    Anything new wakes it, and the snooze is dropped. Returns the snoozes still in force."""
    keep = {}
    for pr in prs:
        if pr.number in snoozes:
            if {i.key for i in pr.items} <= snoozes[pr.number]:
                pr.snoozed, keep[pr.number] = True, snoozes[pr.number]
    return keep


CARD = re.compile(r"^[\w.-]+/(\d+)/")  # fix/4922/... -> 4922


def agent_links(prs):
    """herdr agent panes that work on each PR: the title names the PR, its card or branch, or the pane's
    folder is on the PR's branch. Empty when herdr is not there."""
    if not shutil.which("herdr"):
        return
    try:
        out = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True, timeout=5).stdout
        agents = json.loads(out)["result"]["agents"]
        spaces = json.loads(subprocess.run(["herdr", "workspace", "list"], capture_output=True, text=True,
                                           timeout=5).stdout)["result"]["workspaces"]
    except (subprocess.TimeoutExpired, ValueError, KeyError):
        return
    space_name = {w["workspace_id"]: w.get("label") or w["workspace_id"] for w in spaces}
    branch = {}
    for a in agents:
        cwd = a.get("foreground_cwd") or a.get("cwd") or ""
        if cwd not in branch:
            r = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
            branch[cwd] = r.stdout.strip() if r.returncode == 0 else ""
    for pr in prs:
        card = (CARD.match(pr.head) or [None, None])[1]
        pr.agents = []
        for a in agents:
            title = a.get("terminal_title_stripped") or ""
            why = ("branch" if pr.head and branch[a.get("foreground_cwd") or a.get("cwd") or ""] == pr.head else
                   "title" if re.search(rf"#{pr.number}\b|\bPR {pr.number}\b", title) or (pr.head and pr.head in title)
                   or (card and re.search(rf"\b{card}\b", title)) else "")
            if why:
                space = space_name.get(a.get("workspace_id"), a.get("workspace_id", ""))
                pr.agents.append({"pane": a["pane_id"], "agent": a["agent"], "status": a.get("agent_status", ""),
                                  "title": title, "workspace": space, "where": f"{space} · {title or a['agent']}",
                                  "match": why})


def state_path(repo):
    return os.path.join(STATE_DIR, repo.replace("/", "__") + ".json")


def load_seen(repo):
    try:
        seen = json.load(open(state_path(repo)))
        return seen if isinstance(seen, dict) else {}  # older format: a plain list
    except (OSError, ValueError):
        return {}


def save_seen(repo, seen, keep_days=30):
    cutoff = time.time() - keep_days * 86400
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(state_path(repo), "w") as f:
        json.dump({k: t for k, t in seen.items() if t > cutoff}, f)


NO_PUSH = re.compile(r"\b(don'?t|do not|please don'?t)\s+push\b", re.I)


def quote(text, n):
    """The question in a message if there is one, else its start."""
    body = text.split(": ", 1)[-1] if text.startswith(("review:", "changes requested:")) else text
    q = next((x for x in re.split(r"(?<=[.?!])\s+", body) if "?" in x), body)
    return q if len(q) <= n else q[:n - 1] + "…"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


REBASE_REF = re.compile(r"\b(rebased|rebasing)\b((?:[\s,]*(?:and\s+)?#\d+)+)", re.I)
REBASE_HERE = re.compile(r"\b(taking the rebase|rebasing (this|it)|i'?m rebasing)\b", re.I)


def rebase_notes(pr_nodes, me):
    """PR number -> (login, time, 'rebasing' | 'rebased'): the latest rebase someone else announced for it.
    Read from comments on any PR, so "I'm rebasing #652 and #654" posted on #645 counts for both."""
    notes = {}
    def note(number, who, when, state):
        if number not in notes or notes[number][1] < when:
            notes[number] = (who, when, state)
    for pr in pr_nodes:
        bodies = [(c, c["createdAt"]) for c in pr["comments"]["nodes"]]
        bodies += [(r, r["submittedAt"]) for r in pr["reviews"]["nodes"] if r.get("submittedAt")]
        bodies += [(c, c["createdAt"]) for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]]
        for node, when in bodies:
            who, body = login(node), node.get("body") or ""
            if who == me:
                continue
            refs = REBASE_REF.findall(body)
            for verb, nums in refs:
                for n in re.findall(r"#(\d+)", nums):
                    note(int(n), who, when, verb.lower())
            if refs and not NO_PUSH.search(body):
                continue  # "I'm rebasing #652 and #654" is about those PRs, not the one it was posted on
            if REBASE_HERE.search(body) or NO_PUSH.search(body):
                note(pr["number"], who, when, "rebasing")
            elif not refs and re.search(r"\brebased\b", body, re.I):
                note(pr["number"], who, when, "rebased")
    return notes


def plan_steps(pr, rebase, me):
    """([{"kind", "text", "you"}], message to answer or None). `you` steps are actions; the others explain."""
    name = lambda l: pr.names.get(l) or l
    of = lambda *lanes: [i for i in pr.items if i.lane in lanes]
    steps, ask = [], None
    add = lambda kind, text, you: steps.append({"kind": kind, "text": text, "you": you})
    rebasing = rebase and rebase[2] == "rebasing" and rebase[1] > pr.last_commit_at  # nothing pushed since
    # ponytail: GitHub can't tell whether you pulled, so the reminder just expires after a day
    recent = rebase and (datetime.now(timezone.utc) - parse_time(rebase[1])).total_seconds() < 86400

    if replies := of("reply"):
        ask = replies[-1]
        kinds = {i.kind for i in replies}
        who = " & ".join(sorted({name(i.who) for i in replies}))
        if "review" in kinds or "comment" in kinds:
            add("reply", f"reply to {who}'s {'review' if 'review' in kinds else 'comment'}", True)
        else:
            add("answer-threads", f"answer {who} in {plural(len(replies), 'review thread')}", True)
    for i in of("code"):
        if i.kind == "review":
            add("address-changes", f"address the changes {name(i.who)} asked for", True)
        elif i.kind == "checks" and not rebasing:
            add("fix-checks", "fix the failing checks", True)
        elif i.kind == "conflict" and not rebasing:
            add("update-base", f"update from {pr.base or 'the base'} (merge conflict)", True)
    if of("review"):
        if any(i.kind == "review-request" for i in of("review")):
            add("review", "review it", True)
        else:
            add("re-review", "re-review the new commits", True)
    if pr.status == "READY" and not any(st["you"] for st in steps):
        add("merge", "merge it", True)
    if not any(st["you"] for st in steps) and pr.waiting_on:
        add("waiting", f"waiting on {', '.join(pr.waiting_on)}", False)
    if recent and not rebasing:
        add("pull", f"pull before you push: {name(rebase[0])} rebased it", False)
    if rebasing:
        add("hold", f"don't push: {name(rebase[0])} is rebasing it", False)
    if not rebase and (pushed := [i for i in of("code") if i.kind == "commit"]):
        add("pull", f"pull before you push: {name(pushed[-1].who)} pushed commits", False)
    if threads := of("resolve"):
        add("resolve", f"{plural(len(threads), 'thread')} to resolve", False)
    if posted := of("posted"):
        add("posted", f"{plural(len(posted), 'open thread')}, you posted since", False)
    if pr.snoozed:  # keep the sentences, but none of them is yours until something new happens
        steps = [{"kind": "snoozed", "text": "snoozed until new activity", "you": False}] + \
                [dict(st, you=False) for st in steps]
    if not steps:
        add("waiting", "waiting for a review", False)
    seen, unique = set(), []
    for st in steps:  # same sentence once
        if st["text"] not in seen:
            seen.add(st["text"])
            unique.append(st)
    return unique, ask


def plan(pr, rebase, me):
    """(needs you?, the steps as one line, the message to answer or None) for one PR."""
    steps, ask = plan_steps(pr, rebase, me)
    return any(st["you"] for st in steps), " · ".join(st["text"] for st in steps), ask


def report(prs, me, rebases, only=None, errors=()):
    """The decisions as plain data for scripts and agents (schema pr-watch/1, see README)."""
    out = []
    for pr in sorted(prs, key=lambda p: (p.repo, p.number)):
        steps, ask = plan_steps(pr, rebases.get((pr.repo, pr.number)), me)
        needs = any(st["you"] for st in steps)
        if only == "you" and not needs or only == "notyou" and needs:
            continue
        out.append({
            "repo": pr.repo, "number": pr.number, "title": pr.title, "url": pr.url, "role": pr.role,
            "draft": pr.draft, "base": pr.base, "needs_you": needs, "new": any(i.new for i in pr.items),
            "summary": " · ".join(st["text"] for st in steps), "steps": steps,
            "ask": {"who": ask.who, "name": pr.names.get(ask.who, ask.who), "at": ask.when, "kind": ask.kind,
                    "url": ask.url, "text": ask.text} if ask else None,
            "waiting_on": pr.waiting_on, "last_speaker": pr.last_speaker,
            "review_decision": pr.decision, "checks": pr.checks, "mergeable": pr.mergeable,
            "head": pr.head, "snoozed": pr.snoozed, "agents": pr.agents,
        })
    return {"schema": "pr-watch/1", "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "viewer": me, "repos": sorted({p.repo for p in prs}),
            "counts": {"you": sum(p["needs_you"] for p in out), "notyou": sum(not p["needs_you"] for p in out)},
            "prs": out, "errors": list(errors)}


# Display settings. Each can come from a flag, then config.json ("display"), then these defaults.
COLUMNS = {  # name: (header, default width; 0 = fill the rest)
    "new": ("", 1), "repo": ("REPO", 16), "pr": ("PR", 5), "title": ("TITLE", 34),
    "action": ("ACTION", 0), "who": ("WHO", 10), "age": ("AGE", 4), "agent": ("AGENT", 32)}
DISPLAY = {
    "columns": ["new", "pr", "title", "action", "agent", "age"],
    "widths": {},                            # both tables
    "table_widths": {"you": {}, "notyou": {}},  # one table; wins over "widths"
    # 256-colour palette: the same in every terminal that herdr runs in
    "colours": {"header": "bold+255", "columns": "bold+252", "you": "bold+203", "notyou": "bold+114",
                "repo": "bold+117", "new": "213", "pr": "bold+75", "title": "250", "who": "180", "age": "244",
                "quote": "italic+246", "selected": "236", "agent": "141",
                "reply": "221", "fix": "209", "review": "81", "hold": "177", "waiting": "245"},
    "group": "auto",     # repo: one section per repository · none: all together · auto: repo when 2+ repos
    "quotes": True,      # quote the message to answer under each YOU row
    "detail_height": 12,  # lines of the details panel in --tui (drag its top line to change)
}
SGR = {"bold": "1", "dim": "2", "italic": "3", "underline": "4", "black": "30", "red": "31", "green": "32",
       "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "white": "37", "grey": "90", "gray": "90"}


def sgr(spec):
    """'bold+yellow', '208', '#89b4fa' or 'none' -> an ANSI escape ('' for none). Raises ValueError if unknown."""
    codes = []
    for part in filter(None, spec.lower().split("+")):
        if part == "none":
            continue
        if part in SGR:
            codes.append(SGR[part])
        elif part.isdigit() and int(part) < 256:
            codes.append(f"38;5;{part}")
        elif re.fullmatch(r"#[0-9a-f]{6}", part):
            codes.append("38;2;" + ";".join(str(int(part[i:i + 2], 16)) for i in (1, 3, 5)))
        else:
            raise ValueError(f"unknown colour {part!r}: use names ({', '.join(SGR)}), 0-255, #rrggbb or none")
    return f"\033[{';'.join(codes)}m" if codes else ""


def display_settings(args, cfg):
    d = json.loads(json.dumps(DISPLAY))  # deep copy of the defaults
    saved = cfg.get("display") or {}
    d.update({k: v for k, v in saved.items() if k in ("columns", "group", "quotes", "detail_height")})
    d["widths"].update(saved.get("widths") or {})
    for table, w in (saved.get("table_widths") or {}).items():
        d["table_widths"].setdefault(table, {}).update(w)
    d["colours"].update(saved.get("colours") or {})
    if args.columns:
        d["columns"] = [c.strip() for c in args.columns.split(",") if c.strip()]
    for spec in args.width or []:
        for pair in spec.split(","):
            name, _, n = pair.partition("=")
            table, _, col = name.strip().rpartition(".")   # "you.action=60" or "action=60"
            if table and table not in d["table_widths"]:
                sys.exit(f"--width {name}: the table is 'you' or 'notyou'")
            (d["table_widths"][table] if table else d["widths"])[col] = int(n)
    for spec in args.color or []:
        key, _, value = spec.partition("=")
        d["colours"][key.strip()] = value.strip()
    if args.group:
        d["group"] = args.group
    if args.quotes is not None:
        d["quotes"] = args.quotes
    bad = [c for c in d["columns"] if c not in COLUMNS] + [w for w in d["widths"] if w not in COLUMNS]
    bad += [w for t in d["table_widths"].values() for w in t if w not in COLUMNS]
    if bad:
        sys.exit(f"unknown column(s) {', '.join(bad)}: choose from {', '.join(COLUMNS)}")
    for key, value in d["colours"].items():
        try:
            sgr(value)
        except ValueError as e:
            sys.exit(f"--color {key}: {e}")
    return d


def layout(columns, widths, width):
    """[(name, x, width)]: fixed columns first, the 0-width ones share what is left."""
    w = {c: widths.get(c, COLUMNS[c][1]) for c in columns}
    fill = [c for c in columns if w[c] == 0]
    rest = max(8, (width - sum(w[c] + 1 for c in columns if w[c]) - len(fill)) // max(1, len(fill)))
    out, x = [], 0
    for c in columns:
        out.append((c, x, w[c] or rest))
        x += (w[c] or rest) + 1
    return out


def action_kind(text):
    first = text.split(" · ")[0]
    if first.startswith(("reply", "answer")):
        return "reply"
    if first.startswith(("review", "re-review")):
        return "review"
    if first.startswith("don't push"):
        return "hold"
    if first.startswith(("update from", "fix", "address", "merge")):
        return "fix"
    return "waiting"


def widths_for(d, table):
    return {**d["widths"], **d["table_widths"].get(table, {})}


def build(prs, me, rebases, colour, width, d=DISPLAY, selected=None, need_count=None):
    """Lines to print, and for each line what it is: None, ("header", table, spec) or ("row", pr, needs, text, ask).
    YOU and NOT YOU are separate tables, each with its own column header and widths."""
    pal = {k: sgr(v) for k, v in d["colours"].items()}
    c = (lambda key, s: f"{pal.get(key, '')}{s}\033[0m" if pal.get(key) else s) if colour else (lambda key, s: s)
    link = (lambda url, text: f"\033]8;;{url}\033\\{text}\033]8;;\033\\") if colour else (lambda url, text: text)
    fit = lambda s, n: s.ljust(n) if len(s) <= n else s[:n - 1] + "…"
    repos = sorted({p.repo for p in prs})
    by_repo = d["group"] == "repo" or (d["group"] == "auto" and len(repos) > 1)
    show_repo = len(repos) > 1 and not by_repo and "repo" not in d["columns"]
    specs = {t: layout(d["columns"], widths_for(d, t), width - 2) for t in ("you", "notyou")}
    lines, meta = [], []

    def add(text, what=None):
        lines.append(text)
        meta.append(what)

    def row(table, pr, needs, text, ask):
        who = ask.who if ask else ""
        if not who and "waiting on " in text:
            who = text.split("waiting on ", 1)[1].split(" · ")[0]
        cells = {"new": "●" if any(i.new for i in pr.items) else "", "repo": pr.repo.split("/")[1],
                 "pr": (f"{pr.repo.split('/')[1]}#" if show_repo else "#") + str(pr.number),
                 "title": short_title(pr.title, 500), "action": text, "who": pr.names.get(who, who),
                 "age": age(ask.when) if ask else "",
                 "agent": (("↗ " if colour else "") + pr.agents[0]["where"] +
                           (f" (+{len(pr.agents) - 1})" if len(pr.agents) > 1 else "")) if pr.agents else ""}
        parts = []
        for name, _, w in specs[table]:
            key = action_kind(text) if name == "action" else name
            piece = c(key, fit(cells[name], w))
            parts.append(link(pr.url, piece) if name == "pr" else piece)
        line = " ".join(parts).rstrip()
        if selected == (pr.repo, pr.number):
            bg = sgr(d["colours"].get("selected", "none")).replace("38;5;", "48;5;").replace("38;2;", "48;2;")
            line = ("▶ " + line).replace("\033[0m", "\033[0m" + bg) if colour and bg else "▶ " + line
            line = (bg + line if colour and bg else line)
        else:
            line = "  " + line
        add(line, ("row", pr, needs, text, ask))
        if needs and ask and d["quotes"]:
            said = ask.text.split(": ", 1)[-1] if ask.kind == "thread" else ask.text
            add(c("quote", fit(f"      “{quote(said, width)}”", width).rstrip()))

    def tables(group):
        rows = [(pr, *plan(pr, rebases.get((pr.repo, pr.number)), me)) for pr in sorted(group, key=lambda p: p.number)]
        for label, table, needs in (("YOU", "you", True), ("NOT YOU", "notyou", False)):
            part = [r for r in rows if r[1] == needs]
            if not part:
                continue
            add(c(table, f"{label} ({len(part)})"))
            add(c("columns", "  " + "│".join(fit(COLUMNS[n][0], w) for n, _, w in specs[table]).rstrip()),
                ("header", table, specs[table]))
            for r in part:
                row(table, *r)
            add("")

    mine = need_count if need_count is not None else sum(plan(p, rebases.get((p.repo, p.number)), me)[0] for p in prs)
    where = repos[0].split("/")[1] if len(repos) == 1 else f"{len(repos)} repos"
    add(c("header", fit(f"PRs · {where} · {time.strftime('%H:%M')} · {mine} need you", width).rstrip()))
    add("")
    if by_repo:
        for repo in repos:
            add(c("repo", repo))
            tables([p for p in prs if p.repo == repo])
    else:
        tables(prs)
    while lines and not lines[-1]:
        lines.pop(), meta.pop()
    return lines, meta


def render(prs, me, rebases, colour, width=None, d=DISPLAY, need_count=None):
    width = width or shutil.get_terminal_size((120, 40)).columns
    return "\n".join(build(prs, me, rebases, colour, width, d, need_count=need_count)[0])


def short_title(title, n):
    """'fix/4922: round prices per line, not per order' -> 'round prices per line, not…'"""
    title = re.sub(r"^[\w.-]+/[\w.-]+:\s*", "", title)
    return title if len(title) <= n else title[:n - 1].rstrip() + "…"


def herdr_toast(title, body):
    """A herdr toast when running under herdr. False when herdr can't show it (toasts off, no client attached)."""
    if not os.environ.get("HERDR_SOCKET_PATH") and not os.environ.get("HERDR_PANE_ID"):
        return False
    try:
        r = subprocess.run(["herdr", "notification", "show", title, "--body", body],
                           capture_output=True, text=True, timeout=5)
        return bool(json.loads(r.stdout).get("result", {}).get("shown"))
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError):
        return False


def notify(prs, rebases, me):
    """A herdr toast, else a macOS notification, for PRs that got something new that needs you in this refresh."""
    lines = []
    for pr in prs:
        needs, text, _ = plan(pr, rebases.get((pr.repo, pr.number)), me)
        if needs and any(i.first for i in pr.items):
            lines.append(f"#{pr.number} {text.split(' · ')[0]}")
    if not lines:
        return
    body = "\n".join(lines[:4]) + (f"\n+{len(lines) - 4} more" if len(lines) > 4 else "")
    if herdr_toast("PR watch", body) or sys.platform != "darwin":
        return
    # message passed as an argument, never spliced into the AppleScript source
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 1 of argv) with title \"PR watch\" sound name \"default\"",
                    "-e", "end run", body], capture_output=True, timeout=10)


def fit_height(text, rows):
    lines = text.splitlines()
    if len(lines) < rows:
        return text
    hidden = len(lines) - (rows - 2)
    return "\n".join(lines[:rows - 2] + [f"… {hidden} more lines: enlarge the pane, or run without --watch"])


def load_config():
    try:
        cfg = json.load(open(CONFIG))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


def check_access(repos):
    if gh("auth", "status", timeout=20).returncode:
        sys.exit("gh is not logged in: run `gh auth login` first.")
    for repo in repos:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            sys.exit(f"{repo!r} is not owner/name")
        if gh("repo", "view", repo, "--json", "name", timeout=20).returncode:
            sys.exit(f"Can't read {repo} with your gh login: check the name and your access.")


def repos_to_watch(args, cfg):
    if args.repo:
        return args.repo
    if cfg.get("repos"):
        return cfg["repos"]
    # The plugin folder is itself a git clone: never mistake it for a repository to watch.
    here = os.path.realpath(HERE)
    cwd = os.path.realpath(os.getcwd())
    inside_plugin = cwd == here or cwd.startswith(here + os.sep)
    out = None if inside_plugin else gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", timeout=20)
    if out is not None and out.returncode == 0 and out.stdout.strip():
        return [out.stdout.strip()]
    sys.exit("No repository to watch. Run `pr_watch.py --add-repo owner/name`, pass --repo, or start it inside a clone.")


def manage_config(args, cfg):
    """--setup / --add-repo / --remove-repo / --save / --reset-display / --show-config. True if it did one."""
    did = False
    if args.setup:
        check_access(args.setup)
        cfg["repos"], did = list(dict.fromkeys(args.setup)), True
    if args.add_repo:
        check_access(args.add_repo)
        cfg["repos"], did = list(dict.fromkeys((cfg.get("repos") or []) + args.add_repo)), True
    if args.remove_repo:
        missing = [r for r in args.remove_repo if r not in (cfg.get("repos") or [])]
        if missing:
            sys.exit(f"not in the config: {', '.join(missing)}")
        cfg["repos"], did = [r for r in cfg["repos"] if r not in args.remove_repo], True
    if args.reset_display:
        cfg.pop("display", None)
        did = True
    if args.save:
        d = display_settings(args, cfg)
        cfg["display"] = {"columns": d["columns"], "group": d["group"], "quotes": d["quotes"],
                          "detail_height": d["detail_height"],
                          "widths": d["widths"], "table_widths": d["table_widths"], "colours": {k: v for k, v in d["colours"].items()
                                                             if DISPLAY["colours"].get(k) != v}}
        did = True
    if did:
        save_config(cfg)
    if did or args.show_config:
        print(f"{CONFIG}\n{json.dumps(cfg, indent=2)}")
    return did or args.show_config


def run(repo, args):
    owner, name = repo.split("/")
    data = gh_graphql(owner, name)
    me = data["viewer"]["login"]
    prs = []
    nodes = data["repository"]["pullRequests"]["nodes"]
    for pr in nodes:
        requested = [n["requestedReviewer"].get("login") for n in pr["reviewRequests"]["nodes"] if n["requestedReviewer"]]
        involved = login(pr) == me or me in requested or any(login(r) == me for r in pr["reviews"]["nodes"])
        if involved:
            p = analyse(pr, me, args.bots)
            p.repo = repo
            prs.append(p)
    seen = load_seen(repo)
    first_run = not seen
    fresh = mark_new(prs, seen, time.time(), args.new_hours)
    if first_run:  # everything is "new" the first time: don't ring or notify for it
        fresh = 0
        for p in prs:
            for i in p.items:
                i.first = False
    save_seen(repo, seen)
    snoozes = load_snoozes(repo)
    if args.snooze or args.unsnooze:
        for p in prs:
            if p.number in (args.snooze or []):
                snoozes[p.number] = {i.key for i in p.items}
            if p.number in (args.unsnooze or []):
                snoozes.pop(p.number, None)
    save_snoozes(repo, apply_snoozes(prs, snoozes))
    fresh = sum(1 for p in prs if not p.snoozed for i in p.items if i.first and needs_you(i))  # snoozed: quiet
    rebases = {(repo, n): note for n, note in rebase_notes(nodes, me).items()}
    return prs, me, fresh, rebases


def fetch_all(repos, args):
    """Read every repository. One that can't be read (renamed, no access) is reported in `errors` and the
    others still show; only when none can be read is it an error."""
    prs, rebases, fresh, me, errors = [], {}, 0, "", []
    for repo in repos:
        try:
            p, who, f, r = run(repo, args)
        except (GitHubError, subprocess.TimeoutExpired) as e:
            errors.append(f"{repo}: {e}")
            continue
        me = who or me
        prs, fresh = prs + p, fresh + f
        rebases.update(r)
    if errors and len(errors) == len(repos):
        raise GitHubError("; ".join(errors))
    agent_links(prs)
    return prs, me, fresh, rebases, errors


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\")


def strip_ansi(text):
    return ANSI.sub("", text)


def selection_text(ys, frame, meta, top, divider_y):
    """Text for screen lines ys: table rows become the full PR text, panel and other lines are copied as shown."""
    out, done = [], set()
    for y in ys:
        line = top + y - 1
        m = meta[line] if (divider_y is None or y < divider_y) and 1 <= y and line < len(meta) else None
        if divider_y is None or y < divider_y:
            if 1 <= y and line < len(meta) and m and m[0] == "row":
                _, pr, _, text, ask = m
                if (pr.repo, pr.number) not in done:
                    done.add((pr.repo, pr.number))
                    out.append(copy_text(pr, text, ask) + "\n")
                continue
        if 0 <= y < len(frame):
            plain = strip_ansi(frame[y]).rstrip()
            if plain.lstrip().startswith("“") and done:
                continue  # the quote of a row already copied in full
            if plain and not plain.startswith(("[⧉", "─── ═══")):
                out.append(plain.strip())
    return "\n".join(out).strip() + "\n"


MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
KEYS = re.compile(r"\x1b\[<\d+;\d+;\d+[Mm]|\x1b\[[0-9;?]*[A-Za-z~]|\x1bO[A-Za-z]|\x1b|.", re.S)
UNFINISHED = re.compile(r"\x1b(\[[0-9;<?]*|O)?$")  # the start of a sequence cut by the end of a read


def detail_lines(row, width):
    _, pr, _, text, ask = row
    wrap = lambda s, indent="  ": textwrap.wrap(s, width - 2, initial_indent=indent, subsequent_indent=indent) or [indent]
    grip = " ═══ drag to resize · +/- · wheel scrolls here ═══ "
    out = [("─" * 3 + grip).ljust(width - 1, "─"), f"#{pr.number} {pr.title}"] + wrap(text)
    if ask:
        out += wrap(f"{pr.names.get(ask.who, ask.who)}, {local_time(ask.when)}:") + wrap(ask.text, "    ")
    for a in pr.agents:
        out.append(f"  agent: {a['where']} — {a['agent']}, {a['status']} (pane {a['pane']}, matched by {a['match']})")
    return out + [f"  {pr.url}    (enter: open · g: go to agent · s: snooze · esc: close)"]


def plain_report(prs, me, rebases, only=None):
    """The report as plain text, nothing cut: what you copy, and what the text view shows."""
    r = report(prs, me, rebases, only)
    out = []
    for label, needs in (("YOU", True), ("NOT YOU", False)):
        part = [p for p in r["prs"] if p["needs_you"] == needs]
        if not part:
            continue
        out += [f"{label} ({len(part)}) · {time.strftime('%Y-%m-%d %H:%M')}", ""]
        for p in part:
            out.append(f"#{p['number']} {p['title']}")
            out.append(f"  → {p['summary']}")
            if p["ask"] and needs:
                out.append(f"  {p['ask']['name']}, {local_time(p['ask']['at'])}: “{one_line(p['ask']['text'], 2000)}”")
                out.append(f"  {p['ask']['url']}")
            for a in p["agents"]:
                out.append(f"  agent: {a['where']}")
            out.append(f"  {p['url']}")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def text_view(out, fd, text, mouse_on):
    """Leave the full screen and print plain text, so the terminal can select and copy it. t or q returns."""
    out.write("\033[?1006l\033[?1002l\033[?25h\033[?1049l")  # normal screen, mouse back to the terminal
    out.write("\033[2J\033[H" + text.replace("\n", "\r\n") +
              "\r\n\033[2m── text view: select and copy with the mouse · t: back to the tables · q: quit ──\033[0m\r\n")
    out.flush()
    while True:
        key = os.read(fd, 16).decode("utf-8", "replace")
        if "q" in key:
            return False
        if "t" in key or "\x1b" == key:
            out.write("\033[?1049h\033[?25l" + ("\033[?1002h\033[?1006h" if mouse_on else ""))
            return True


BUTTONS = [("you", "⧉ copy YOU"), ("all", "⧉ copy all"), ("row", "⧉ copy row"), ("text", "▤ text view")]


def button_bar(colour):
    """The first screen line, and where each button is: [(key, x0, x1)]."""
    parts, spans, x = [], [], 0
    for key, label in BUTTONS:
        txt = f"[{label}]"
        spans.append((key, x, x + len(txt)))
        parts.append(f"\033[1;38;5;117m{txt}\033[0m" if colour else txt)
        x += len(txt) + 1
    return " ".join(parts), spans


def copy_text(pr, text, ask):
    lines = [f"#{pr.number} {pr.title}", pr.url, text]
    if ask:
        lines += [f"{pr.names.get(ask.who, ask.who)}, {local_time(ask.when)}:", ask.text, ask.url]
    return "\n".join(lines)


def copy_to_clipboard(text):
    """pbcopy on macOS, wl-copy / xclip on Linux, else OSC 52 (the terminal's own clipboard)."""
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd, input=text, text=True, timeout=5).returncode == 0
    import base64
    sys.stdout.write("\033]52;c;" + base64.b64encode(text.encode()).decode() + "\a")
    return True


def what_table(row_meta):
    return "you" if row_meta[2] else "notyou"   # ("row", pr, needs, text, ask)


def tui(args, d, repos, cfg):
    """Full-screen view: drag a │ in a table header to resize that column (saved), click or d for details,
    enter opens the PR in the browser, r refreshes, q quits. GitHub is read in a background thread."""
    state = {"data": None, "error": "", "fresh": 0}
    wake = threading.Event()

    def refresh():
        while True:
            try:
                state["data"] = fetch_all(repos, args)
                failed = state["data"][4]
                state["error"] = f"⚠ not shown: {'; '.join(failed)}" if failed else ""
                if state["data"][2]:
                    sys.stdout.write("\a")
                    if not args.no_notify:
                        notify(state["data"][0], state["data"][3], state["data"][1])
            except (GitHubError, subprocess.TimeoutExpired, OSError, ValueError, KeyError, TypeError) as e:
                state["error"] = f"⚠ GitHub read failed, showing the last data: {e}"  # keep the last data, say so
            wake.wait(args.watch or 300)
            wake.clear()

    threading.Thread(target=refresh, daemon=True).start()
    fd = sys.stdin.fileno()
    saved_tty = termios.tcgetattr(fd)
    out = sys.stdout
    out.write("\033[?1049h\033[?25l\033[?1002h\033[?1006h")  # alternate screen, no cursor, mouse
    sel, detail, drag, top, mouse_on, flash = None, False, None, 0, True, ""
    vdrag, dscroll = False, 0  # dragging the details divider; scroll inside the details
    pending, last_sel = "", None  # an escape sequence split across reads; the selection last shown
    lsel = None                # text selection: [start y, end y, x, y of the press]
    try:
        tty.setcbreak(fd)
        while True:
            cols, rows_ = shutil.get_terminal_size((120, 40))
            if state["data"] is None:
                lines, meta = ["PR watch · reading GitHub…"], [None]
            else:
                prs, me, _, rebases, _ = state["data"]
                lines, meta = build(prs, me, rebases, not args.no_colour, cols, d, sel)
            rows_idx = [i for i, m in enumerate(meta) if m and m[0] == "row"]
            specs_now = {t: layout(d["columns"], widths_for(d, t), cols - 2) for t in ("you", "notyou")}
            if rows_idx and sel not in [(m[1].repo, m[1].number) for m in meta if m and m[0] == "row"]:
                sel = (meta[rows_idx[0]][1].repo, meta[rows_idx[0]][1].number)
                lines, meta = build(prs, me, rebases, not args.no_colour, cols, d, sel)
            cur = next((i for i in rows_idx if (meta[i][1].repo, meta[i][1].number) == sel), None)
            extra = []
            if detail and cur is not None:  # a panel of d["detail_height"] lines: divider + scrolled content
                full = detail_lines(meta[cur], cols)
                height = max(3, min(d["detail_height"], rows_ - 6))
                dscroll = max(0, min(dscroll, len(full) - height))
                content = full[1 + dscroll:dscroll + height]
                more = len(full) - 1 - dscroll - len(content)
                if more > 0:
                    content[-1] = f"\033[2m  … {more} more lines: scroll here or drag the line above\033[0m"
                extra = [f"\033[1;38;5;117m{full[0]}\033[0m"] + content + [""] * (height - 1 - len(content))
            body = rows_ - 2 - len(extra)   # line 0: buttons, last line: footer
            divider_y = 1 + body if extra else None
            bar, spans = button_bar(not args.no_colour)
            if cur is not None and sel != last_sel:  # bring a newly selected row on screen; the wheel can scroll away
                top = min(max(top, cur - body + 1), cur)
                last_sel = sel
            top = max(0, min(top, max(0, len(lines) - body)))
            view = lines[top:top + body]
            footer = state["error"] or flash or (
                "drag over lines: copy · drag │: width · click: details · ↗/g: agent · enter: open · s: snooze"
                " · Y/A/y: copy · t: text · q" if mouse_on else
                "MOUSE OFF: select and copy text with the mouse · m: mouse back on · y: copy · u: copy link · q")
            frame = [bar] + view + [""] * (body - len(view)) + extra + [f"\033[2m{footer[:cols - 1]}\033[0m"]
            if lsel and lsel[0] != lsel[1]:
                for yy in range(min(lsel[:2]), max(lsel[:2]) + 1):
                    if 0 < yy < len(frame) - 1:
                        frame[yy] = "\033[7m" + strip_ansi(frame[yy]).ljust(cols - 1) + "\033[0m"
            out.write("\033[H" + "\n".join(l + "\033[K" for l in frame) + "\033[J")
            out.flush()

            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            data = pending + os.read(fd, 4096).decode("utf-8", "replace")
            cut = UNFINISHED.search(data)
            if cut and cut.group(0) != "\x1b":  # wait for the rest; a lone ESC is the Esc key
                data, pending = data[:cut.start()], data[cut.start():]
            else:
                pending = ""
            flash = ""  # a copy message lasts until the next key or click
            for tok in KEYS.findall(data):
                m = MOUSE.fullmatch(tok)
                if m and m[4] == "M" and m[1] == "0" and m[3] == "1":  # a click on the button bar = its key
                    hit = next((k for k, x0, x1 in spans if x0 <= int(m[2]) - 1 < x1), None)
                    if hit:
                        tok, m = {"you": "Y", "all": "A", "row": "y", "text": "t"}[hit], None
                if m:
                    b, x, y, down = int(m[1]), int(m[2]) - 1, int(m[3]) - 1, m[4] == "M"
                    line = top + y - 1
                    what = meta[line] if 1 <= y <= body and line < len(meta) else None
                    if vdrag and not down:  # release: keep the panel height
                        vdrag = False
                        cfg.setdefault("display", {})["detail_height"] = d["detail_height"]
                        save_config(cfg)
                    elif vdrag and b in (0, 32):
                        d["detail_height"] = max(3, min(rows_ - 6, rows_ - 1 - y))
                    elif b == 0 and down and divider_y is not None and y == divider_y:
                        vdrag = True
                    elif b in (64, 65) and divider_y is not None and y > divider_y:  # wheel over the panel
                        dscroll = max(0, dscroll + (3 if b == 65 else -3))
                    elif drag and not down:  # release: keep the new width
                        d_table, drag = drag[0], None
                        cfg.setdefault("display", {}).setdefault("table_widths", {})[d_table] = d["table_widths"][d_table]
                        save_config(cfg)
                    elif drag and b in (0, 32):
                        d_table, col, start = drag
                        d["table_widths"][d_table][col] = max(2, x - start)
                    elif b in (64, 65) and rows_idx:  # wheel
                        top = max(0, top + (3 if b == 65 else -3))
                    elif b == 0 and down and what and what[0] == "header":
                        _, table, spec = what
                        edge = [(n, cx) for n, cx, w in spec if abs(x - (2 + cx + w)) <= 1]
                        if edge:
                            col, cx = edge[0]
                            drag, d_table = (table, col, 2 + cx), table
                    elif lsel and b == 32:  # dragging a text selection
                        lsel[1] = max(1, min(y, rows_ - 2))
                    elif lsel and not down:  # release: copy what was selected, or act on a plain click
                        y0, y1, cx_, cy_ = lsel
                        lsel = None
                        if y0 != y1:
                            ys = range(min(y0, y1), max(y0, y1) + 1)
                            ok = copy_to_clipboard(selection_text(ys, frame, meta, top, divider_y))
                            flash = f"copied {len(ys)} lines to the clipboard" if ok else "copy failed"
                            continue
                        line_ = top + cy_ - 1
                        what = meta[line_] if 1 <= cy_ <= body and line_ < len(meta) else None
                        if what and what[0] == "row" and what[1].agents and any(
                                n == "agent" and 2 + cx <= cx_ < 2 + cx + w for n, cx, w in specs_now[what_table(what)]):
                            a = what[1].agents[0]
                            subprocess.run(["herdr", "agent", "focus", a["pane"]], capture_output=True, timeout=5)
                            flash = f"jumped to {a['where']}"
                        elif what and what[0] == "row":
                            key = (what[1].repo, what[1].number)
                            detail = not (detail and key == sel)
                            dscroll = 0 if key != sel else dscroll
                            sel = key
                    elif b == 0 and down and 1 <= y < rows_ - 1:  # start a selection (or a click)
                        lsel = [y, y, x, y]
                elif tok in ("q", "Q"):
                    return
                elif tok == "\x1b":
                    detail = False
                elif tok in ("+", "=", "-") and detail:
                    d["detail_height"] = max(3, min(rows_ - 6, d["detail_height"] + (2 if tok != "-" else -2)))
                    cfg.setdefault("display", {})["detail_height"] = d["detail_height"]
                    save_config(cfg)
                elif tok in ("\x1b[A", "\x1b[B", "k", "j") and cur is not None:
                    i = rows_idx.index(cur) + (1 if tok in ("\x1b[B", "j") else -1)
                    m2 = meta[rows_idx[max(0, min(i, len(rows_idx) - 1))]]
                    sel = (m2[1].repo, m2[1].number)
                elif tok in ("\r", "\n") and cur is not None:
                    webbrowser.open(meta[cur][1].url)
                elif tok == "d":
                    detail = not detail
                elif tok == "r":
                    wake.set()
                elif tok == "m":  # give the mouse back to the terminal, so text can be selected
                    mouse_on = not mouse_on
                    out.write("\033[?1002h\033[?1006h" if mouse_on else "\033[?1006l\033[?1002l")
                    drag = None
                elif tok == "s" and cur is not None:  # snooze until new activity, or wake it
                    pr = meta[cur][1]
                    snoozes = load_snoozes(pr.repo)
                    if pr.snoozed:
                        snoozes.pop(pr.number, None)
                    else:
                        snoozes[pr.number] = {i.key for i in pr.items}
                    save_snoozes(pr.repo, snoozes)
                    pr.snoozed = not pr.snoozed
                    flash = f"#{pr.number} snoozed until new activity" if pr.snoozed else f"#{pr.number} back"
                elif tok == "g" and cur is not None:  # jump to the agent working on it
                    pr = meta[cur][1]
                    if pr.agents:
                        subprocess.run(["herdr", "agent", "focus", pr.agents[0]["pane"]], capture_output=True, timeout=5)
                        flash = f"jumped to {pr.agents[0]['where']}"
                    else:
                        flash = f"no herdr agent found for #{pr.number}"
                elif tok in ("Y", "A") and state["data"]:
                    prs_, me_, _, reb_, _ = state["data"]
                    text_ = plain_report(prs_, me_, reb_, "you" if tok == "Y" else None)
                    what_ = "the YOU section" if tok == "Y" else "everything"
                    flash = f"copied {what_} to the clipboard" if copy_to_clipboard(text_) else "copy failed"
                elif tok == "t" and state["data"]:
                    prs_, me_, _, reb_, _ = state["data"]
                    if not text_view(out, fd, plain_report(prs_, me_, reb_), mouse_on):
                        return
                elif tok in ("y", "u") and cur is not None:
                    _, pr, _, text, ask = meta[cur]
                    copied = pr.url if tok == "u" else copy_text(pr, text, ask)
                    flash = "copied to the clipboard" if copy_to_clipboard(copied) else "copy failed"
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved_tty)
        out.write("\033[?1006l\033[?1002l\033[?25h\033[?1049l")
        out.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 epilog=f"columns: {', '.join(COLUMNS)} · colour keys: {', '.join(DISPLAY['colours'])}")
    g = ap.add_argument_group("repositories (saved in the config)")
    g.add_argument("--add-repo", action="append", metavar="OWNER/NAME", help="watch this repository too (repeatable)")
    g.add_argument("--remove-repo", action="append", metavar="OWNER/NAME", help="stop watching it (repeatable)")
    g.add_argument("--setup", nargs="+", metavar="OWNER/NAME", help="replace the list of repositories")
    g.add_argument("--show-config", action="store_true", help="print the config file and exit")
    g.add_argument("--repo", action="append", metavar="OWNER/NAME", help="watch only these, this run (not saved)")
    v = ap.add_argument_group("display (this run; add --save to keep them)")
    v.add_argument("--columns", metavar="A,B,…", help=f"columns in order, from: {', '.join(COLUMNS)}")
    v.add_argument("--width", action="append", metavar="COL=N", help="column width, 0 = fill the rest (repeatable)")
    v.add_argument("--color", action="append", metavar="KEY=SPEC",
                   help="colour: names joined by + (bold+yellow), 0-255, #rrggbb or none (repeatable)")
    v.add_argument("--group", choices=["auto", "repo", "none"], help="one section per repository, or all together")
    v.add_argument("--quotes", action=argparse.BooleanOptionalAction, default=None, help="quote the message to answer")
    v.add_argument("--no-colour", action="store_true", help="plain text")
    v.add_argument("--save", action="store_true", help="save the display flags above in the config")
    v.add_argument("--reset-display", action="store_true", help="forget saved display settings")
    r = ap.add_argument_group("run")
    r.add_argument("--watch", type=int, metavar="SECONDS", help="repeat every SECONDS")
    r.add_argument("--tui", action="store_true",
                   help="full-screen: drag │ to resize columns, click for details, enter opens the PR")
    r.add_argument("--bots", action="store_true", help="count comments from bots")
    r.add_argument("--new-hours", type=float, default=12, help="how long ● stays (default 12)")
    r.add_argument("--no-notify", action="store_true", help="no herdr toast or macOS notification, only the terminal bell")
    r.add_argument("--json", action="store_true", help="the decisions as JSON (schema pr-watch/1, for scripts and agents)")
    r.add_argument("--only", choices=["you", "notyou"], help="only PRs that need you, or only the others")
    r.add_argument("--exit-code", action="store_true", help="exit 3 if a PR needs you, 0 if none (1 = error)")
    r.add_argument("--snooze", type=int, action="append", metavar="PR",
                   help="hide this PR from YOU until it gets new activity (repeatable)")
    r.add_argument("--unsnooze", type=int, action="append", metavar="PR", help="undo --snooze")
    args = ap.parse_args()
    cfg = load_config()
    if manage_config(args, cfg):
        return
    d = display_settings(args, cfg)
    repos = repos_to_watch(args, cfg)
    if args.tui:
        return tui(args, d, repos, cfg)
    while True:
        try:
            prs, me, fresh, rebases, failed = fetch_all(repos, args)
        except GitHubError as e:
            if not args.watch:
                sys.exit(str(e))
            print(f"\033[2J\033[H⚠ {e} · retrying in {args.watch} s", flush=True)
            time.sleep(args.watch)
            continue
        if failed and not args.json:
            print("\n".join(f"⚠ not shown: {e}" for e in failed), file=sys.stderr)
        if args.json:
            print(json.dumps(report(prs, me, rebases, args.only, failed), indent=2))
        else:
            need = sum(plan(p, rebases.get((p.repo, p.number)), me)[0] for p in prs)
            if args.only:  # same filter for the text view
                prs = [p for p in prs if plan(p, rebases.get((p.repo, p.number)), me)[0] == (args.only == "you")]
            text = render(prs, me, rebases, sys.stdout.isatty() and not args.no_colour, d=d, need_count=need)
            if args.watch:  # fit the pane
                print("\033[2J\033[H", end="")
                text = fit_height(text, shutil.get_terminal_size((120, 40)).lines)
            print(text)
            if args.watch and fresh:  # something new that needs you appeared in this refresh
                print("\a", end="", flush=True)
                if not args.no_notify:
                    notify(prs, rebases, me)
        if not args.watch:
            if args.exit_code:
                sys.exit(3 if any(plan(p, rebases.get((p.repo, p.number)), me)[0] for p in prs) else 0)
            return
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
