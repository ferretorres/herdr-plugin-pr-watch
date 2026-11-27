import json
"""Offline check of the decision rules in pr_watch.analyse (no GitHub calls)."""
from pr_watch import analyse, mark_new

ME, BOB = "me", "bob"


def pr(author=ME, commits=(), comments=(), reviews=(), threads=(), decision=None, mergeable="MERGEABLE",
       checks="SUCCESS", requested=()):
    return {
        "number": 1, "title": "t", "url": "u", "isDraft": False, "updatedAt": "2026-01-09", "mergeable": mergeable,
        "reviewDecision": decision, "headRefName": "b", "author": {"login": author},
        "reviewRequests": {"nodes": [{"requestedReviewer": {"login": r}} for r in requested]},
        "commits": {"nodes": [{"commit": {"oid": f"c{i}", "committedDate": d, "authoredDate": d, "messageHeadline": "m",
                                          "author": {"user": {"login": who}, "name": who},
                                          "statusCheckRollup": {"state": checks}}}
                              for i, (who, d) in enumerate(commits)]},
        "comments": {"nodes": [{"author": {"login": w}, "createdAt": d, "body": "hi", "url": f"c{d}"} for w, d in comments]},
        "reviews": {"nodes": [{"author": {"login": w}, "state": s, "submittedAt": d, "body": b, "url": f"r{d}"}
                              for w, s, d, b in reviews]},
        "reviewThreads": {"nodes": [{"isResolved": res, "isOutdated": False, "path": "f.py",
                                     "comments": {"nodes": [{"author": {"login": w}, "createdAt": d, "body": "x",
                                                             "url": f"t{d}"} for w, d in cs]}}
                                    for res, cs in threads]},
    }


kinds = lambda p: [i.kind for i in p.items]

# A comment after my last commit counts; one before it does not. Only a question or request needs me.
p = analyse(pr(commits=[(ME, "2026-01-05")], comments=[(BOB, "2026-01-04"), (BOB, "2026-01-06")]), ME, False)
assert kinds(p) == ["comment"] and p.status == "WAITING"          # "hi": read, nothing to answer
q0 = pr(commits=[(ME, "2026-01-05")], comments=[(BOB, "2026-01-06")])
q0["comments"]["nodes"][0]["body"] = "could you rename it?"
assert analyse(q0, ME, False).status == "ACTION"

# An unresolved thread whose last word is Bob's needs me at any age; resolved or answered threads do not.
p = analyse(pr(commits=[(ME, "2026-01-05")], threads=[(False, [(BOB, "2026-01-01")]),
                                                       (True, [(BOB, "2026-01-02")]),
                                                       (False, [(BOB, "2026-01-02"), (ME, "2026-01-03")])]), ME, False)
assert kinds(p) == ["thread"]

# Changes requested after my last push; a commit someone else pushed; failing checks; a conflict.
p = analyse(pr(commits=[(ME, "2026-01-01"), (BOB, "2026-01-03")], reviews=[(BOB, "CHANGES_REQUESTED", "2026-01-02", "fix")],
               checks="FAILURE", mergeable="CONFLICTING"), ME, False)
assert kinds(p) == ["review", "commit", "checks", "conflict"]

# Approved, green and mergeable with nothing else pending: READY. An approval alone is not an action.
p = analyse(pr(commits=[(ME, "2026-01-01")], reviews=[(BOB, "APPROVED", "2026-01-02", "")], decision="APPROVED"), ME, False)
assert p.status == "READY", p

# Bot comments are ignored unless asked for.
bot = pr(commits=[(ME, "2026-01-01")], comments=[("ci-bot[bot]", "2026-01-02")])
assert analyse(bot, ME, False).items == [] and kinds(analyse(bot, ME, True)) == ["comment"]

# As reviewer: a pending request, and commits pushed after my last review.
p = analyse(pr(author=BOB, commits=[(BOB, "2026-01-03")], reviews=[(ME, "COMMENTED", "2026-01-02", "q")],
               requested=[ME]), ME, False)
assert p.role == "reviewer" and kinds(p) == ["re-review", "review-request"]

# Someone rebases my commit after asking me not to push: the request still shows, as a hold.
q = pr(commits=[(ME, "2026-01-01")], comments=[(BOB, "2026-01-02")])
q["comments"]["nodes"][0]["body"] = "please don't push to this branch"
q["commits"]["nodes"][0]["commit"]["committedDate"] = "2026-01-03"   # rebased later, authored before the comment
p2 = analyse(q, ME, False)
assert kinds(p2) == ["comment"] and p2.items[0].lane == "hold"

# Lanes: a question needs a reply, an acknowledgement only a resolve, a later "done" lifts a hold.
from pr_watch import Item, lane_of
t = lambda text: Item("thread", BOB, "2026-01-02", f"f.py: {text}", "u")
assert lane_of(t("thanks, looks good now"), []) == "resolve"
assert lane_of(t("thanks, but why is the cap 1.2M?"), []) == "reply"
assert lane_of(t("this breaks the worker limits"), []) == "reply"
hold = Item("comment", BOB, "2026-01-02", "please don't push until it's done", "u")
done = Item("comment", BOB, "2026-01-03", "rebased onto dev, done", "u")
assert lane_of(hold, [hold]) == "hold" and lane_of(hold, [hold, done]) == "info"
assert lane_of(Item("review", BOB, "d", "changes requested: x", "u"), []) == "code"
assert lane_of(Item("comment", BOB, "d", "rebased onto dev, CI green", "u"), []) == "read"
assert lane_of(Item("review", BOB, "d", "review: same changes i approved, so i'm still good with this", "u"), []) == "read"
assert lane_of(Item("review", BOB, "d", "review: the description still says 1.9M", "u"), []) == "reply"
assert lane_of(Item("review", BOB, "d", "review: looks good, three things left: the parser", "u"), []) == "reply"
from pr_watch import short_name
assert short_name("Lee, Ana (Example Corp, Inc.)") == "Ana" and short_name("Ann Lee") == "Ann"
assert lane_of(Item("conflict", "git", "d", "x", "u"), []) == "code"

# Whose turn: I spoke last, so the PR waits on the requested reviewers.
w = pr(commits=[(ME, "2026-01-01")], comments=[(BOB, "2026-01-02"), (ME, "2026-01-03")], requested=["ann"])
assert "you spoke last" in analyse(w, ME, False).turn and "ann" in analyse(w, ME, False).turn

# NEW only on the first sight of an item.
p.status = "ACTION"
seen = {}
assert mark_new([p], seen, 1000, 12) == 2 and all(i.new for i in p.items)            # first sight: NEW, counted
assert mark_new([p], seen, 1000 + 3600, 12) == 0 and all(i.new for i in p.items)     # an hour later: still NEW
mark_new([p], seen, 1000 + 13 * 3600, 12)
assert not any(i.new for i in p.items)                                               # after 12 h: not NEW
print("ok")

# Watch mode keeps the top (the summary) and says how much was cut.
from pr_watch import fit_height
assert fit_height("a\nb", 10) == "a\nb"
assert fit_height("\n".join("abcdef"), 4).splitlines() == ["a", "b", "… 4 more lines: enlarge the pane, or run without --watch"]
print("ok")

# Rebase notes are read from any PR: "I rebased #645 and I'm rebasing #652 and #654" posted on #645.
from pr_watch import plan, rebase_notes
note = pr(comments=[(BOB, "2026-01-05T10:00:00Z")])
note["number"] = 645
note["comments"]["nodes"][0]["body"] = "btw I rebased #645 onto dev and i'm rebasing #652 and #654 rn, so please pull"
here = pr(comments=[(BOB, "2026-01-04T10:00:00Z")])
here["number"] = 646
here["comments"]["nodes"][0]["body"] = "taking the rebase of this one, please don't push to this branch"
notes = rebase_notes([note, here], ME)
assert notes[645][2] == "rebased" and notes[652][2] == "rebasing" and notes[654][2] == "rebasing", notes
assert notes[646][2] == "rebasing"

# Plan: a conflict on a PR someone is rebasing is not yours; a reply is, with the question quoted.
busy = analyse(pr(commits=[(ME, "2026-01-01T00:00:00Z")], mergeable="CONFLICTING"), ME, False)
needs, text, ask = plan(busy, (BOB, "2026-01-05T10:00:00Z", "rebasing"), ME)
assert not needs and "is rebasing" in text, text
needs, text, _ = plan(busy, None, ME)
assert needs and "merge conflict" in text
rv = pr(commits=[(ME, "2026-01-01T00:00:00Z")], reviews=[(BOB, "COMMENTED", "2026-01-02T00:00:00Z", "three things left: x")])
from datetime import datetime, timezone, timedelta
recent = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
needs, text, ask = plan(analyse(rv, ME, False), (BOB, recent, "rebased"), ME)
assert needs and text.startswith("reply to bob's review") and "pull before you push" in text and ask.kind == "review"
_, old_text, _ = plan(analyse(rv, ME, False), (BOB, "2026-01-01T12:00:00Z", "rebased"), ME)
assert "pull" not in old_text                                   # the reminder expires after a day
print("ok")

# Titles lose the branch prefix and fit the space.
from pr_watch import short_title
assert short_title("fix/4922: round prices per line, not per order", 20) == "round prices per li…"
assert short_title("Add a README", 40) == "Add a README"

# An item is "first" only in the run that first sees it.
p.items[0].key  # noqa: existing PR from above
seen2 = {}
mark_new([p], seen2, 5000, 12)
assert all(i.first for i in p.items)
for i in p.items:
    i.first = False
mark_new([p], seen2, 5100, 12)
assert not any(i.first for i in p.items)
print("ok")

# Display settings: colours, column layout, flags over saved config over defaults.
import argparse
from pr_watch import DISPLAY, action_kind, display_settings, layout, sgr
assert sgr("bold+yellow") == "\033[1;33m" and sgr("208") == "\033[38;5;208m" and sgr("none") == ""
assert sgr("#89B4FA") == "\033[38;2;137;180;250m"
try:
    sgr("sparkly"); raise AssertionError("accepted a bad colour")
except ValueError:
    pass
assert layout(["pr", "title", "action"], {"title": 10}, 40) == [("pr", 0, 5), ("title", 6, 10), ("action", 17, 22)]
flags = argparse.Namespace(columns="pr,action", width=["action=30"], color=["reply=red"], group=None, quotes=False)
cfg = {"display": {"columns": ["pr", "title"], "group": "repo", "widths": {"title": 50}, "colours": {"fix": "blue"}}}
d = display_settings(flags, cfg)
assert d["columns"] == ["pr", "action"] and d["group"] == "repo" and d["quotes"] is False       # flag, saved, flag
assert d["widths"] == {"title": 50, "action": 30} and d["colours"]["reply"] == "red" and d["colours"]["fix"] == "blue"
assert d["colours"]["you"] == DISPLAY["colours"]["you"]                                          # default kept
assert [action_kind(t) for t in ("reply to Ana's review", "update from dev (merge conflict)", "review it",
                                 "don't push: Ana is rebasing it", "waiting on Ben")] == ["reply", "fix", "review", "hold", "waiting"]
assert lane_of(t("yes, sounds good"), []) == "resolve"
print("ok")

# Per-table widths: "you.action=60" sets one table, "action=40" both; the table value wins.
from pr_watch import widths_for
flags = argparse.Namespace(columns=None, width=["action=40", "you.action=60"], color=None, group=None, quotes=None)
d = display_settings(flags, {})
assert widths_for(d, "you")["action"] == 60 and widths_for(d, "notyou")["action"] == 40
print("ok")

# What "y" copies: title, link, the action, and the whole message with its link.
from pr_watch import PullRequest, copy_text
pr_ = PullRequest(7, "fix/1: t", "https://x/7", "author", "ACTION", "", False, [], names={"bob": "Bob"})
ask_ = Item("review", "bob", "2026-01-02T10:00:00Z", "review: two things left", "https://x/7#r1", lane="reply")
assert copy_text(pr_, "reply to Bob's review", ask_).splitlines()[:3] == ["#7 fix/1: t", "https://x/7", "reply to Bob's review"]
assert copy_text(pr_, "reply", ask_).splitlines()[-2:] == ["review: two things left", "https://x/7#r1"]
print("ok")

# Agent output: structured steps with stable kinds, and the pr-watch/1 report.
from pr_watch import plan_steps, report
rv2 = analyse(pr(commits=[(ME, "2026-01-01T00:00:00Z")], mergeable="CONFLICTING",
                 reviews=[(BOB, "COMMENTED", "2026-01-02T00:00:00Z", "could you rename it?")]), ME, False)
rv2.repo = "o/r"
steps, ask = plan_steps(rv2, None, ME)
assert [(st["kind"], st["you"]) for st in steps] == [("reply", True), ("update-base", True)] and ask.who == BOB
r = report([rv2], ME, {}, None)
assert r["schema"] == "pr-watch/1" and r["counts"] == {"you": 1, "notyou": 0}
row = r["prs"][0]
assert row["needs_you"] and row["ask"]["url"] and row["mergeable"] == "CONFLICTING" and row["steps"] == steps
assert report([rv2], ME, {}, "notyou")["prs"] == []
json.dumps(r)  # plain data only
print("ok")

# Snooze: hidden while nothing new arrives; one new item wakes it.
from pr_watch import apply_snoozes, plan_steps
s1 = analyse(pr(commits=[(ME, "2026-01-01T00:00:00Z")], comments=[(BOB, "2026-01-02T00:00:00Z")]), ME, False)
s1.items[0].text = "could you check this?"; s1.items[0].lane = "reply"
keep = apply_snoozes([s1], {1: {i.key for i in s1.items}})
assert s1.snoozed and 1 in keep and not plan(s1, None, ME)[0] and plan_steps(s1, None, ME)[0][0]["kind"] == "snoozed"
s2 = analyse(pr(commits=[(ME, "2026-01-01T00:00:00Z")], comments=[(BOB, "2026-01-02T00:00:00Z"), (BOB, "2026-01-03T00:00:00Z")]), ME, False)
assert apply_snoozes([s2], {1: {s1.items[0].key}}) == {} and not s2.snoozed            # new comment: awake

# Auto-snooze: a thread whose last word predates something you posted is not "answer" any more.
th = analyse(pr(commits=[(ME, "2026-01-05T00:00:00Z")], threads=[(False, [(BOB, "2026-01-02T00:00:00Z")])]), ME, False)
th.items[0].text = "f.py: why is this here?"
from pr_watch import lane_of as _lo
assert th.items[0].lane == "posted" and not plan(th, None, ME)[0]
print("ok")

# Plain text for copying: full message, links, YOU only when asked; buttons are where the bar says.
from pr_watch import button_bar, plain_report
rv3 = analyse(pr(commits=[(ME, "2026-01-01T00:00:00Z")], reviews=[(BOB, "COMMENTED", "2026-01-02T00:00:00Z", "could you rename it? " + "x" * 300)]), ME, False)
rv3.repo, rv3.title, rv3.url = "o/r", "fix/1: rename", "https://x/1"
txt = plain_report([rv3], ME, {}, "you")
assert txt.startswith("YOU (1)") and "#1 fix/1: rename" in txt and "x" * 300 in txt and "https://x/1" in txt
assert "NOT YOU" not in txt
bar, spans = button_bar(False)
assert [k for k, _, _ in spans] == ["you", "all", "row", "text"] and bar[spans[1][1]:spans[1][2]] == "[⧉ copy all]"
print("ok")

# Drag-to-copy: colours and links are stripped; a dragged table row copies the whole PR, not the cut cells.
from pr_watch import selection_text, strip_ansi
assert strip_ansi("\x1b[1;38;5;75m#652\x1b[0m \x1b]8;;https://x\x1b\\link\x1b]8;;\x1b\\") == "#652 link"
frame = ["[⧉ copy YOU]", "\x1b[1mYOU (1)\x1b[0m", "  ● #7  t…  reply…", "  “two things…”", "footer"]
meta = [None, ("row", pr_, True, "reply to Bob's review", ask_), None]  # body line 0 = screen line 1
got = selection_text(range(1, 4), frame, [None] + meta[:1] + [meta[1], None], 0, None)
assert "#7 fix/1: t" in got and "review: two things left" in got and "https://x/7" in got, got
print("ok")


# --- review fixes (2026-11-27) ---
from pr_watch import KEYS, UNFINISHED, clean, needs_you, selection_text
# Times with an offset compare as times: 11:00+01:00 is before 10:30Z.
data = clean({"a": {"createdAt": "2026-01-02T11:00:00+01:00"}, "b": {"committedDate": "2026-01-02T10:30:00Z"}})
assert data["a"]["createdAt"] == "2026-01-02T10:00:00Z" and data["a"]["createdAt"] < data["b"]["committedDate"]
# Text from GitHub can't carry terminal codes; newlines stay.
assert clean({"title": "fix\x1b]52;c;aGk=\x07 it\nnow"})["title"] == "fix]52;c;aGk= it\nnow"
# Bots are known by their account type.
assert clean({"author": {"__typename": "Bot", "login": "coderabbitai"}})["author"]["login"] == "coderabbitai[bot]"
# A commit with no author does not crash.
na = pr(commits=[(ME, "2026-01-01T00:00:00Z")])
na["commits"]["nodes"][0]["commit"]["author"] = None
analyse(na, ME, False)
# Approved and mergeable with a harmless comment: merge it. A draft: not.
ok = pr(commits=[(ME, "2026-01-01T00:00:00Z")], comments=[(BOB, "2026-01-02T00:00:00Z")], decision="APPROVED")
ok["comments"]["nodes"][0]["body"] = "nice work"
assert analyse(ok, ME, False).status == "READY"
ok["isDraft"] = True
assert analyse(ok, ME, False).status == "WAITING"
# The bell counts only new items that need you.
rs = analyse(pr(commits=[(ME, "2026-01-05T00:00:00Z")], threads=[(False, [(BOB, "2026-01-06T00:00:00Z")])]), ME, False)
rs.items[0].lane = "resolve"
assert mark_new([rs], {}, 1, 12) == 0 and not needs_you(rs.items[0])
# "I'm rebasing #652 and #654" posted on #645 is about #652 and #654 only; "don't push" in a thread counts.
r1 = pr(comments=[(BOB, "2026-01-05T10:00:00Z")]); r1["number"] = 645
r1["comments"]["nodes"][0]["body"] = "btw I rebased #645 onto dev and i'm rebasing #652 and #654 rn"
n1 = rebase_notes([r1], ME)
assert n1[645][2] == "rebased" and n1[652][2] == "rebasing", n1
r2 = pr(threads=[(False, [(BOB, "2026-01-05T10:00:00Z")])]); r2["number"] = 9
r2["reviewThreads"]["nodes"][0]["comments"]["nodes"][0]["body"] = "please don't push, I'm on it"
assert rebase_notes([r2], ME)[9][2] == "rebasing"
# Keys: Ctrl-Up is one token (not "A" = copy all); a mouse report cut by a read is kept for the next read.
assert KEYS.findall("\x1b[1;5A") == ["\x1b[1;5A"] and KEYS.findall("\x1b[A") == ["\x1b[A"]
assert UNFINISHED.search("x\x1b[<0;20;").start() == 1 and UNFINISHED.search("x") is None
# Dragging a selection below a short list does not crash.
selection_text(range(1, 30), ["a"] * 3, [None, None], 0, None)
# Config lives in herdr's folder for this plugin; a plugin pane's HERDR_PLUGIN_CONFIG_DIR wins over the default.
import os, subprocess, types
import pr_watch
for k in ("PR_WATCH_CONFIG", "HERDR_PLUGIN_CONFIG_DIR", "XDG_CONFIG_HOME"):
    os.environ.pop(k, None)
assert pr_watch.config_path().endswith("/.config/herdr/plugins/config/ferretorres.pr-watch/config.json")
os.environ["HERDR_PLUGIN_CONFIG_DIR"] = "/x/cfg"
assert pr_watch.config_path() == "/x/cfg/config.json"
os.environ["PR_WATCH_CONFIG"] = "/y.json"
assert pr_watch.config_path() == "/y.json"
# Toast: herdr first; a toast herdr did not show (toasts off, no client) falls back; outside herdr nothing is tried.
calls, real_run = [], subprocess.run
def fake(out):
    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=out, returncode=0)
    return run
os.environ.pop("HERDR_PANE_ID", None); os.environ.pop("HERDR_SOCKET_PATH", None)
subprocess.run = fake("")
assert pr_watch.herdr_toast("t", "b") is False and calls == []
os.environ["HERDR_PANE_ID"] = "p1"
subprocess.run = fake('{"result":{"shown":true}}')
assert pr_watch.herdr_toast("t", "b") is True and calls[-1][:3] == ["herdr", "notification", "show"]
subprocess.run = fake('{"result":{"shown":false,"reason":"disabled"}}')
assert pr_watch.herdr_toast("t", "b") is False
subprocess.run = fake("not json")
assert pr_watch.herdr_toast("t", "b") is False
subprocess.run = real_run
# With no config, a pane started in the plugin folder (a git clone) does not watch the plugin's own repo.
os.environ.pop("PR_WATCH_CONFIG", None)
old_cwd, old_gh = os.getcwd(), pr_watch.gh
pr_watch.gh = lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh called"))
os.chdir(pr_watch.HERE)
try:
    pr_watch.repos_to_watch(types.SimpleNamespace(repo=None), {})
    raise AssertionError("expected exit")
except SystemExit as e:
    assert "No repository to watch" in str(e)
finally:
    os.chdir(old_cwd); pr_watch.gh = old_gh
# One repository that can't be read is reported; the others still show. All failing is an error.
def fake_run(repo, args):
    if repo == "o/gone":
        raise pr_watch.GitHubError("can't read o/gone: check the name and your access")
    return [], "me", 0, {}
real_run, real_links = pr_watch.run, pr_watch.agent_links
pr_watch.run, pr_watch.agent_links = fake_run, lambda prs: None
try:
    got = pr_watch.fetch_all(["o/ok", "o/gone"], None)
    assert got[1] == "me" and got[4] == ["o/gone: can't read o/gone: check the name and your access"], got
    try:
        pr_watch.fetch_all(["o/gone"], None)
        raise AssertionError("expected GitHubError")
    except pr_watch.GitHubError as e:
        assert "o/gone" in str(e)
    assert pr_watch.report([], "me", {}, None, got[4])["errors"] == got[4]
finally:
    pr_watch.run, pr_watch.agent_links = real_run, real_links
print("ok")
