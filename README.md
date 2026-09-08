# prompt-agent

A small floating pad for your desktop. Paste in rough text, get back a clean
prompt you can hand to a coding agent. It also explains an agent's jargon-heavy
question in plain words, and answers straight questions with a straight answer.

Four buttons, one text box:

| Button | What it does |
| --- | --- |
| **Send** | Rewrites whatever you typed into a sharp, ready-to-paste prompt. Reply in the box to refine it — it remembers the thread. |
| **Explain** | Paste a question *an agent asked you*. Get it in plain English, plus a draft reply to paste back. |
| **Advise** | Ask a straight question. Get an answer with a recommendation, the tradeoff, and the next step — not a menu. |
| **Grab** | Pull the last thing another Claude Code session said into the box, so you don't have to copy it across. |

The **Copy** button always copies whatever is in the left pane. **Send to**
delivers that same text to another Claude Code session on your machine — see
[Talking to your other terminals](#talking-to-your-other-terminals).

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

## Talking to your other terminals

If a Claude Code session in another terminal asks you something you don't
follow, the round trip is three clicks:

1. **Grab** — pick that terminal from the list; its last message drops into the
   input box. (Or just paste the question yourself, if you'd rather.)
2. **Explain** — you get the question in plain words, plus a drafted reply.
3. **Send to** — the reply goes back to that terminal. It offers the one you
   grabbed from, so you don't hunt for it twice.

Both lists show the Claude Code sessions currently running on your machine, by
name and status. Nothing is grabbed or sent until you click one.

**Naming your sessions.** The names in that list are auto-generated
(`yourname-a1`) unless you set them. Start a session with `claude -n crm` and it
shows up as `crm`, which makes the picker readable when several are open. The
name is set at launch; there's no way to rename a running session.

Grab reads that session's transcript on disk, so it sees what the agent last
said — not what's scrolled on your screen. A session that hasn't answered
anything yet has nothing to grab, and says so.

The reply arrives there as a *message* — the same way a message from a person
does. It is not typed into that session's input box and it does not press enter
for you; the agent on the other end reads it and decides what to do.

## Safety

**Send** and **Explain** run with `Write`, `Edit`, `Bash`, `Agent` and `Task`
disabled. They can only hand you text — they will never execute the task they
are describing.

**Advise** is different, because there you are asking the agent to actually
look at something. It can read and write files, run commands, list your other
Claude Code sessions and send them messages. It is told not to change anything
unless you asked in that message, and never to message another session unless
you asked — and to tell you what it sent and to whom when you do.

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
