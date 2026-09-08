# prompt-agent

A small floating pad for your desktop. Paste in rough text, get back a clean
prompt you can hand to a coding agent. It also explains an agent's jargon-heavy
question in plain words, and answers straight questions with a straight answer.

Three buttons, one text box:

| Button | What it does |
| --- | --- |
| **Send** | Rewrites whatever you typed into a sharp, ready-to-paste prompt. Reply in the box to refine it — it remembers the thread. |
| **Explain** | Paste a question *an agent asked you*. Get it in plain English, plus a draft reply to paste back. |
| **Advise** | Ask a straight question. Get an answer with a recommendation, the tradeoff, and the next step — not a menu. |

The **Copy** button always copies whatever is in the left pane.

## Requirements

**This is a front-end for Claude Code. It does nothing on its own.** Before
installing, you need:

- **[Claude Code](https://claude.com/claude-code) installed and signed in**,
  with a working `claude` command. This requires a paid Claude subscription.
  The pad shells out to that CLI for every answer.
- **Python 3.9+ with tkinter.** Bundled with python.org and Windows installers.
  On Debian/Ubuntu: `sudo apt install python3-tk`. On Fedora:
  `sudo dnf install python3-tkinter`.
- **A graphical desktop.** It is a window; it will not run over plain SSH.

## Install

```
pipx install prompt-agent
prompt-agent
```

No `pipx`? `python3 -m pip install --user prompt-agent` works too.

On Linux, one optional extra sharpens keyboard focus:

```
pipx install "prompt-agent[x11]"
```

The app runs fine without it.

## First run

The **`prompt-rewrite` skill** is copied into `~/.claude/skills/` — the pad
invokes it, so without it the first turn fails. An existing file of that name
is left untouched.

## Where your thread is saved

Your conversation is saved outside the package, so closing the pad and
reopening it picks the thread back up.

| OS | Path |
| --- | --- |
| Linux | `~/.config/prompt-agent/` |
| macOS | `~/Library/Application Support/prompt-agent/` |
| Windows | `%APPDATA%\prompt-agent\` |

## Configuration

| Variable | Purpose |
| --- | --- |
| `PROMPT_AGENT_CLAUDE` | Full path to the `claude` binary, if it isn't on your `PATH`. |

## Safety

The rewriter runs with `Write`, `Edit`, `Bash`, `Agent` and `Task` disabled. It
can only hand you text — it will never execute the task it is describing.

## Troubleshooting

**"claude CLI not found"** — make sure `claude --version` works in your
terminal. If it only works in some shells, set `PROMPT_AGENT_CLAUDE` to its
full path.

**"cannot open a window"** — you are on a headless machine or an SSH session
with no display forwarding.

**The first turn fails mentioning a skill** — check that
`~/.claude/skills/prompt-rewrite/SKILL.md` exists; delete it and restart the
pad to have it reinstalled.

**Nothing happens for a while** — each turn is a real Claude Code call and can
take several seconds. The status line shows `thinking…`. Turns time out at 180s.

## License

MIT — see [LICENSE](LICENSE).
