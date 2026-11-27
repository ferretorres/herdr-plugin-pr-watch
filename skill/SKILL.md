---
name: pr-watch
description: Find which open GitHub PRs need something from the user, and what, with pr_watch.py. Use it when the user asks what needs them on their PRs, what to do next on reviews, or who is waiting on whom. Also use it before you push to a PR branch, because someone else can be rebasing it.
---

# PR watch

`pr_watch.py` reads the open PRs that the user authored or reviews. It decides which PRs need the user and what to
do. It only reads from GitHub.

## Get the decisions

`pr_watch.py` is in the plugin folder. Find it once:

```sh
PRW="$(herdr plugin list --json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(next(p["plugin_root"] for p in d["result"]["plugins"] if p["plugin_id"]=="ferretorres.pr-watch"))')/pr_watch.py"
```

```sh
python3 "$PRW" --json --only you   # only what needs the user
python3 "$PRW" --json              # all PRs
python3 "$PRW" --exit-code         # 3 = something needs the user, 0 = nothing
```

Add `--repo owner/name` to look at one repository. With no `--repo`, it uses the repositories in the user's config.
The output schema is `pr-watch/1`; the README describes it. For each PR, read these fields:

- `needs_you`, `summary`: the decision and the one-line sentence.
- `steps[]`: `{kind, text, you}`. The steps with `you: true` are the actions.
- `ask`: the message to answer (`name`, `at`, `url`, full `text`), or `null`.
- `waiting_on`, `checks`, `mergeable`, `head` (the branch), `snoozed`.
- `agents[]`: the herdr panes that work on this PR (`pane`, `agent`, `title`).

## Act on a step

| kind | what to do |
| --- | --- |
| `reply`, `answer-threads` | Read `ask.text`. Draft the answer for the user. Do not post it until the user approves it. |
| `address-changes` | Make the requested change on `head`, then tell the user what changed. |
| `update-base`, `fix-checks` | Rebase or merge the base into `head`, or fix CI. First do the `hold` and `pull` checks below. |
| `review`, `re-review` | Review the PR or its new commits. |
| `merge` | Tell the user that the PR is ready. Do not merge unless the user asks. |
| `hold` | Do not push to this branch. Someone else is rebasing it. |
| `pull` | Pull the branch before you push. Someone rebased it or pushed to it. |
| `waiting`, `resolve`, `posted`, `snoozed` | No action. Tell the user only if they ask. |

## Rules

- Do not post comments, push or merge without the user's approval of that specific action.
- When a PR has `agents[]`, tell the user. That agent probably has the context; let the user choose who acts.
- When the user handled a PR, hide it until it gets new activity:
  `python3 "$PRW" --snooze <number>` (undo with `--unsnooze <number>`).
- The tool decides with keyword rules. Read `ask.text` yourself before you treat a step as certain.
