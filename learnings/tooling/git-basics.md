# Git basics: snapshots and getting back to old versions

*Written 2026-10-19.*

## Key ideas
- The repo is this project folder plus a hidden `.git` folder. **Everything is local.**
- A commit is a **snapshot** of the project: what changed, when, and a message.
- Most "look at the past" commands are **read-only**. Only a few change files on disk.
- `git show <hash>:<path>` lets you view an old file without touching anything. **Start there.**
- Don't run `git switch --detach` while another Claude session is editing this folder.

---

## 1. What the repo is

```
  test folder/                 <- your working files (what you edit)
  ├── src/  specs/  learnings/ ...
  └── .git/                    <- hidden: every snapshot + settings
        ├── config             <- user.name = zhibungus (repo-local)
        └── objects/ ...       <- the stored snapshots
```

- Git lives entirely on this machine.
- Nothing leaves it unless you add a **remote** (e.g. GitHub) and run `git push`.
- This repo has **no remote** right now. Its `.git/config` has no `[remote]` section.

**Remember: no remote + no push = nothing is shared.**

---

## 2. What a commit is

| Part | Example (this repo) |
|---|---|
| Hash (ID) | `197e28f` (short form of a 40-character ID) |
| Message | "Add Binance USD-M data layer (work in progress)" |
| Author | zhibungus `<email>` |
| Time | when it was made |
| Parent | the previous commit, `8d48172` |
| Content | a full snapshot; git shows it as the diff from the parent |

- The author name and email are just a label git requires.
- Others only see them if the repo is pushed somewhere.
- This repo sets them with `git config` (no `--global`), so the setting is **repo-local**. Name: zhibungus.

**Remember: a commit is a full snapshot with a label, linked to its parent.**

---

## 3. The chain and HEAD

The two real commits, oldest on the left:

```
   8d48172  ─────────>  197e28f
   "Add project         "Add Binance USD-M
    setup and            data layer (work
    market-setup         in progress)"
    learning notes"           ▲
                              │
                           master   <- branch name: a movable pointer
                              ▲
                              │
                            HEAD    <- "where you are now"
```

- Each new commit goes on the right. `master` moves forward to it.
- **HEAD** = the commit your folder is based on.
- Normally HEAD points at `master`. After `git switch --detach 8d48172`, HEAD points straight at `8d48172` ("detached HEAD").

Check the hashes yourself any time with `git log --oneline`:

```
197e28f (HEAD -> master) Add Binance USD-M data layer (work in progress)
8d48172 Add project setup and market-setup learning notes
```

---

## 4. Commands for looking at the past

Run these in a terminal opened in the project folder.

| I want to… | Command | Safe? |
|---|---|---|
| List commits, one per line | `git log --oneline` | Read-only |
| List commits + which files each touched | `git log --stat` | Read-only |
| See everything one commit changed | `git show 197e28f` | Read-only |
| See what's changed but not committed | `git status` | Read-only |
| See the exact uncommitted line edits | `git diff` | Read-only |
| View an old version of one file | `git show 8d48172:learnings/README.md` | Read-only |
| Put an old version of one file back | `git restore --source=8d48172 learnings/README.md` | **Changes files** (that one file) |
| Turn the whole folder into an old version | `git switch --detach 8d48172` | **Changes files** (everything tracked) |
| Go back to where you were | `git switch -` | **Changes files** (back to normal) |

Notes:
- **Pager:** `git log` and `git show` open a scrolling viewer. Space = next page, `q` = quit.
- **Paths** in `<hash>:<path>` are from the project root, with forward slashes `/`.
- `git status` today shows `?? src/mm/backtest/`. `??` = **untracked**: git sees the folder but has never saved it in a commit.

### Worked example: get an older README back
```
git log --oneline -- learnings/README.md          # which commits touched it?
git show 8d48172:learnings/README.md              # read the old version (no change)
git restore --source=8d48172 learnings/README.md  # only if you really want it back
git diff learnings/README.md                      # see what the restore changed
```
- `git restore --source` **overwrites** the current file. Uncommitted edits to it are lost.
- The restore is not a commit. It's an ordinary edit. Commit it to keep it, or `git restore learnings/README.md` to undo it (back to the HEAD version).

**Remember: `git show <hash>:<path>` to look; `git restore --source` only when you mean to overwrite.**

---

## 5. Warning: `git switch --detach`

```
  before:  folder = 197e28f files
  git switch --detach 8d48172
  after:   folder = 8d48172 files   (files added in 197e28f vanish from disk;
                                     untracked files like src/mm/backtest/ stay)
  git switch -
  back:    folder = 197e28f files
```

- It swaps **every tracked file** in the folder at once.
- Another Claude session editing this folder would have files swapped under it. Its edits could land on the wrong version or be confused by missing files.
- Commits made in detached HEAD aren't on any branch. They're easy to lose.
- Git may refuse to switch if you have uncommitted edits that would be overwritten. That's a safety feature.
- Use `git switch -` to return to `master`.

**Remember: to just look at the past, use `git show`. Save `switch --detach` for when nothing else is editing the folder.**

---

## 6. GUI options

| Tool | What it gives you |
|---|---|
| VS Code **Source Control** tab (branch icon, left bar) | Changed files, click a file for a side-by-side diff, stage and commit with buttons |
| VS Code **Git Graph** extension | The commit chain as a picture; click a commit to see its files and diffs |

- Both run the same git commands underneath. Same safety rules.

---

## 7. Making commits from now on

```
  edit files  ──>  git add <files>  ──>  git commit -m "message"
  (working)        (staging area:        (new snapshot on master,
                    "include these")      HEAD moves forward)
```

- `git add` picks which changes go into the next snapshot.
- `git commit` saves them with a message.
- The **main Claude session handles commits** when you ask. You don't need to type these.

### The line-ending warning
You may see: `warning: in the working copy of '...', LF will be replaced by CRLF`.
- Linux/Mac end lines with LF. Windows uses CRLF.
- Git is just saying it converts between them on Windows.
- **It's harmless.** Your content is unchanged.

---

## 8. Decisions log
- **2026-10-19. Git author set repo-local to zhibungus; no remote.** Everything stays on this machine.

---

## Check your understanding
1. You want to read `learnings/README.md` as it was in `8d48172` without changing anything. Which command, and why not `git restore --source`?
2. Another Claude session is writing code in this folder. Why is `git switch --detach 8d48172` a bad idea right now, and what would you use instead?
3. `git status` shows `?? src/mm/backtest/`. If you deleted that folder by accident, could git bring it back? Why or why not?
