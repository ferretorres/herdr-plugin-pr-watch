# herdr PR watch

Made by Josep Ferre Torres.

A herdr plugin pane that tells you which of your open GitHub PRs need something from you, and what:

```
PRs · my-repo · 11:18 · 2 need you

YOU
● #643 add retries to the webhoo…  reply to Ana's review · pull before you push: Ana rebased it · 1 thread to resolve   1m
       “… only small things left before it can go in, could you please rebase onto dev?”
● #655 round prices per line…  update from dev (merge conflict)

NOT YOU
  #640 #646 waiting on Ben, Ana
  #652 waiting on Ben · don't push: Ana is rebasing it · 6 threads to resolve
```

It looks at the open PRs you authored or review, and reads:
- comments, reviews and review threads from other people after your last activity;
- merge conflicts and failing checks;
- requests to review;
- rebase notes such as "I'm rebasing #652 and #654" or "taking the rebase, please don't push", on any PR.

`●` marks PRs with something new in the last 12 hours. When something new needs you, the terminal bell rings,
and you get a herdr toast (with `[ui.toast] delivery = "herdr"` in herdr's config) or, failing that, a macOS
notification. `--no-notify` keeps only the bell. Nothing rings on the first run.
It refreshes every 5 minutes. It is read-only: it never writes to GitHub.

## Setup

1. Install the GitHub CLI and log in: `brew install gh` (or https://cli.github.com), then `gh auth login`.
2. Install the plugin:

   ```sh
   herdr plugin install ferretorres/herdr-plugin-pr-watch
   ```

   Update it later with `herdr plugin update ferretorres.pr-watch`. To work on a clone instead, use
   `herdr plugin link /path/to/herdr-plugin-pr-watch`.

3. Add the repositories to watch. This checks that your `gh` login can read them. They are saved in
   `~/.config/herdr/plugins/config/ferretorres.pr-watch/config.json`, outside the plugin folder, so an update
   keeps them. `herdr plugin list --json` shows the plugin folder (`plugin_root`).

   ```sh
   python3 <plugin folder>/pr_watch.py --add-repo owner/repo --add-repo owner/other-repo
   ```

Requirements: herdr 0.9.1 or later, `python3` 3.9 or later, `gh`. No other packages.

## Open it

- In herdr, open a pane next to the current one:
  `herdr plugin pane open --plugin ferretorres.pr-watch --entrypoint watch` (add `--placement zoomed` or `tab` if you want).
- Or run it in any pane: `python3 <plugin folder>/pr_watch.py --watch 300`.
  Inside a clone, it watches that repository when no config exists.

## Repositories

| Flag | Effect |
| --- | --- |
| `--add-repo owner/name` | watch it too (repeatable; checks access) |
| `--remove-repo owner/name` | stop watching it |
| `--setup owner/name …` | replace the whole list |
| `--show-config` | print the config file |
| `--repo owner/name` | watch only this, for this run (not saved) |
| `--group repo` / `none` / `auto` | one section per repository, or all together (`auto`: sections when 2+ repos) |

## Full-screen mode

The herdr pane runs `pr_watch.py --tui --watch 300`:

- YOU and NOT YOU are separate tables, each with its own column header.
- Drag a `│` in a header to resize that column for that table. The width is saved when you release.
- Click a row, or press `d`, to see the full action, the whole message and the link. `esc` closes it.
  To resize the panel, drag its top line (`═══ drag to resize ═══`) or press `+` / `-`. The height is saved.
  The list always keeps at least 4 lines. The wheel scrolls the panel when the pointer is over it.
- `enter` opens the PR in your browser, `↑`/`↓` select, `r` refreshes now, `q` quits.
- **Drag over lines to copy them**, as in a terminal. Rows in a table copy the whole PR: title, link, action,
  and the full message with its link. Lines in the details panel copy as shown. The copy happens on release. A
  click without moving still opens the details.
- The buttons on the first line copy as plain text, with nothing cut: `[⧉ copy YOU]` (or `Y`), `[⧉ copy all]` (`A`),
  `[⧉ copy row]` (`y`, the selected PR). `u` copies only the selected PR's link.
- `[▤ text view]` (or `t`) leaves the tables and prints everything as plain text. The terminal can then select and
  scroll it as usual. `t` returns to the tables.
- `s` snoozes the selected PR: it leaves YOU until it gets new activity. Press `s` again to bring it back.
- `g` jumps to the herdr agent pane that works on the PR (the AGENT column).
- `m` turns the mouse off, so you can select and copy any text with the mouse as usual. Press `m` again to drag
  and click again. While the program uses the mouse, the terminal can't select text.
- GitHub is read in the background, so the screen stays responsive.

Without `--tui` it prints the same tables as text (with `--watch`, it reprints every N seconds).

## Columns and colours

Every display flag works for one run. Add `--save` to keep it in the config, or `--reset-display` to drop the saved display.

```sh
# a wider ACTION column, a WHO column, no quotes, saved for the herdr pane
pr_watch.py --columns new,pr,title,who,action,age --width title=40 --width action=0 --no-quotes --save
# colours: names joined by +, 0-255, #rrggbb or none
pr_watch.py --color reply=bold+#f9e2af --color fix=208 --color title=none --save
```

- Columns: `new` (●), `repo`, `pr`, `title`, `who`, `action`, `agent`, `age`.
- Width `0` means "fill the rest". Defaults: pr 5, title 34, who 10, age 4, action fills.
  `--width action=60` sets both tables; `--width you.action=80` or `notyou.title=20` sets one.
- Colour keys:
  - `header`, `columns`: the title line and the column names;
  - `you`, `notyou`: the section headings;
  - `repo`: the repository headings;
  - `new`, `pr`, `title`, `who`, `age`: the columns;
  - `quote`: the quoted message;
  - `selected`: the background of the selected row in `--tui`;
  - `reply`, `fix`, `review`, `hold`, `waiting`: the ACTION text, by kind.

## Snooze

- **Automatic.** A review thread where someone else had the last word asks for your answer. When you write or push
  on the PR after that word, the thread shows as "open thread, you posted since", not as a step for you. A new
  comment brings it back. Facts that are still true, such as a merge conflict or failing checks, stay until they
  are fixed.
- **By hand.** `--snooze 643` (or `s` in `--tui`) hides a PR from YOU until any new item arrives. `--unsnooze 643`
  brings it back. Snoozes are kept in `~/.cache/herdr-pr-watch/`.

## Agent column

When herdr runs, AGENT shows where the agent that works on each PR is: the workspace name and the session title,
for example `↗ checkout · fix 4922 rounding`. `(+N)` means more panes. Click the cell (or press `g`) to jump there. A pane matches when:
- its title has the PR number (`#655`, `PR 655`), the card number from the branch (`fix/4922/…` → `4922`) or the
  branch name;
- or its folder is checked out on the PR branch.

In `--tui`, `g` jumps there. The JSON lists the panes in `agents[]`.

## For scripts and agents

```sh
pr_watch.py --json --only you     # what needs you, as JSON
pr_watch.py --exit-code           # exit 3 if a PR needs you, 0 if none, 1 on an error
```

`--json` prints the decisions, not the raw GitHub data. The format is versioned (`"schema": "pr-watch/1"`). Fields are
only added under the same version.

```json
{
  "schema": "pr-watch/1", "generated_at": "2026-11-26T12:16:00Z", "viewer": "you",
  "repos": ["owner/repo"], "counts": {"you": 1, "notyou": 4},
  "prs": [{
    "repo": "owner/repo", "number": 643, "title": "…", "url": "https://github.com/…/pull/643",
    "role": "author", "draft": false, "base": "dev", "needs_you": true, "new": true,
    "summary": "reply to Ana's review · pull before you push: Ana rebased it",
    "steps": [{"kind": "reply", "text": "reply to Ana's review", "you": true},
              {"kind": "pull", "text": "pull before you push: Ana rebased it", "you": false}],
    "ask": {"who": "ana-login", "name": "Ana", "at": "2026-11-26T10:17:00Z", "kind": "review",
            "url": "https://github.com/…#pullrequestreview-…", "text": "review: … could you rebase onto dev?"},
    "waiting_on": [], "last_speaker": "ana-login",
    "review_decision": "CHANGES_REQUESTED", "checks": "SUCCESS", "mergeable": "MERGEABLE"
  }]
}
```

- `needs_you` is true when at least one step has `"you": true`.
- Step kinds that need you: `reply`, `answer-threads`, `address-changes`, `fix-checks`, `update-base`, `review`,
  `re-review`, `merge`.
- Step kinds that only inform: `waiting`, `pull`, `hold`, `resolve`, `posted`, `snoozed`.
- More fields: `head` (the branch), `snoozed`, and `agents` (`pane`, `agent`, `status`, `title`, `match`).
- `skill/SKILL.md` is a short briefing for coding agents (Claude Code, Codex) on how to use this output. To
  install it, link it into your agent's skills folder, for example
  `ln -s "<plugin folder>/skill" ~/.claude/skills/pr-watch`.
- `ask` is the message to answer, or `null`. `text` is the whole message. `url` opens it on GitHub.
- `--only you` or `--only notyou` filters the PRs. The same filter works for the text view.

## Other options

- `--no-notify` keeps only the terminal bell.
- `--no-colour` prints plain text.
- `--bots` counts bot comments.
- `--new-hours` sets how long `●` stays.
- `--help` lists every flag.

Remove it: `herdr plugin uninstall ferretorres.pr-watch`. The saved state for `●` is in `~/.cache/herdr-pr-watch/`.

## How it decides

| Shown as | When |
| --- | --- |
| reply to …'s review / comment | someone wrote to you after your last activity, with a question or a request ("could you", "?", "three things left", "still says") |
| answer … in N review threads | an unresolved thread where someone else had the last word, and that word is more than "thanks" |
| N threads to resolve | an unresolved thread where the reviewer only acknowledged ("thanks, looks good") |
| update from base (merge conflict), fix the failing checks | the branch conflicts or the checks fail, and nobody else is rebasing it |
| don't push: X is rebasing it | X said they are rebasing it, and nothing was pushed since |
| pull before you push | someone rebased or pushed to it in the last 24 hours |
| review it / re-review | your review is requested, or there are commits after your last review |
| waiting on … | you wrote last; it names the requested reviewers |

These rules use keywords. They can be wrong, so the question is quoted under each "reply" line.

Limits:
- Up to 50 open PRs for each repository.
- The last 40 comments, reviews and commits of each PR.

## Check

```sh
python3 test_pr_watch.py   # prints "ok" (offline, no GitHub calls)
```

## License

MIT, see [LICENSE](LICENSE).
