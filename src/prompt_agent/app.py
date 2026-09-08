#!/usr/bin/env python3
"""Floating prompt-rewrite pad.

Left pane: the current prompt (always the thing Copy puts on the clipboard).
Right pane: a chat thread with the rewriting agent. Answer its questions
inline and the prompt on the left is replaced with each revision.

The thread is a real Claude Code session -- turn 1 creates it with a fixed
--session-id, later turns --resume it -- so the agent remembers the prompt it
already wrote instead of starting over from the raw text each time."""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import uuid
from tkinter import font as tkfont

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = "prompt-rewrite"
TIMEOUT = 180
# Send and Explain must never execute the task they are describing -- that is
# what makes their output safe to paste anywhere. Advise is different: there you
# are asking the agent to actually do something, so it gets tools.
NO_TOOLS = ["Write", "Edit", "Bash", "Agent", "Task"]
ADVISE_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash",
                "ListAgents", "SendMessage"]


def find_claude():
    """Locate the Claude Code CLI.

    PATH first, so a normal install just works; the explicit paths below are
    for launchers (desktop files, Finder) that start with a stripped PATH.
    PROMPT_AGENT_CLAUDE overrides everything for an unusual install."""
    env = os.environ.get("PROMPT_AGENT_CLAUDE")
    if env:
        return env
    found = shutil.which("claude")
    if found:
        return found
    guesses = [
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.claude/local/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]
    if os.name == "nt":
        guesses = [os.path.expanduser("~/AppData/Roaming/npm/claude.cmd")]
    for g in guesses:
        if os.path.exists(g):
            return g
    return "claude"          # let the run fail with a readable error


CLAUDE = find_claude()


def config_dir():
    """Per-user data lives outside the package: an installed package sits in
    a venv that gets replaced on upgrade, taking the saved thread with it."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.expanduser("~/.config"))
    d = os.path.join(base, "prompt-agent")
    os.makedirs(d, exist_ok=True)
    return d


CONFIG = config_dir()
STATE = os.path.join(CONFIG, ".session.json")


def open_in_editor(path):
    """Open a file in whatever the OS considers its default editor."""
    if os.name == "nt":
        os.startfile(path)                       # noqa: S606 -- Windows only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


SKILL_SRC = os.path.join(HERE, "skills", "prompt-rewrite.md")
SKILL_DST = os.path.expanduser(
    os.path.join("~", ".claude", "skills", "prompt-rewrite", "SKILL.md"))


def ensure_skill():
    """Install the prompt-rewrite skill into ~/.claude/skills on first run.

    The pad invokes `/prompt-rewrite`, which only exists if the skill file is
    on disk. Without this the very first turn fails for anyone who did not
    already happen to have this skill. An existing file is left alone -- the
    user may have edited it."""
    try:
        if os.path.exists(SKILL_DST):
            return False
        if not os.path.exists(SKILL_SRC):
            return False
        os.makedirs(os.path.dirname(SKILL_DST), exist_ok=True)
        with open(SKILL_SRC) as f:
            body = f.read()
        with open(SKILL_DST, "w") as f:
            f.write(body)
        return True
    except OSError:
        return False


# The pad is a rewrite tool, not an intake interview. The usable prompt is the
# deliverable every single turn; a question is optional and never a gate.
STYLE = (
    "This is a quick rewrite pad, not an interview. Always return the finished "
    "text and nothing else. Never interrogate the user and never ask a "
    "follow-up question.\n\n"
    "Never leave a blank, a placeholder, or a bracketed marker of any kind for "
    "the user to fill in. If a detail is missing, either write the text so it "
    "does not need that detail, or fold it in as a plain clause the reader can "
    "answer ('...and say what that depends on'). The user must always be able "
    "to paste the result immediately, with no editing.\n\n"
    "If the input is a QUESTION -- an opinion, an explanation, a comparison, a "
    "recommendation -- rewrite it as a sharper version of the SAME question. "
    "No Task / Constraints / Deliverable / Done-when sections. Keep the user's "
    "own words and their level of technical language: sharpen the question, do "
    "not make it sound like an engineer wrote it.\n\n"
    "If the input is a job to be done, write the shortest prompt that makes "
    "the assignment clear, using only the sections that genuinely help."
)

# Replies in rewrite mode are ambiguous: "make it shorter" wants a new prompt,
# but "why is this useful?" wants an answer. Forcing a fenced block on both
# turned a plain question into a silent prompt rewrite -- the chat showed only
# "(prompt updated)".
REPLY = (
    "If my message is a QUESTION about the prompt or about what to do next, "
    "just answer it in plain prose. Do NOT return a prompt, do NOT use a "
    "fenced block.\n\n"
    "If my message asks for a CHANGE to the prompt, return the full revised "
    "prompt in a single fenced block and nothing else."
)

# Translate mode. The opposite of STYLE: here the questions ARE the point.
# The agent's job is to decode jargon and hand back a reply you can paste.
ASK = (
    "Below is a question a coding agent asked me. I am not a developer and "
    "some of the wording is over my head.\n\n"
    "Do this, in order:\n"
    "1. In two or three plain sentences, say what it is actually asking and "
    "why it matters. No jargon. If you must use a technical term, define it "
    "in the same breath.\n"
    "2. If you need something from me to answer it, ask ONE short everyday "
    "question -- never two joined by 'and', never a list. Offer the likely "
    "choices in plain words when there are only a few, and say which one is "
    "the normal pick and why.\n"
    "3. Then give me the reply to paste back, in a fenced block, written in "
    "my voice as the answer to the agent's question.\n\n"
    "If you genuinely cannot draft the reply without my answer, still give a "
    "fenced block using the most reasonable assumption and say in one line "
    "what you assumed, so I always leave with something to paste."
)

ADVISE = (
    "Below is a question I am asking you directly. I want your advice, not a "
    "rewritten prompt and not a prompt to paste anywhere.\n\n"
    "Answer it, in this order:\n"
    "1. Give me the answer first, in one or two plain sentences. If it is a "
    "choice, pick one -- do not hand me a numbered menu and ask me to decide. "
    "No jargon; if you must use a technical term, define it in the same "
    "breath.\n"
    "2. Then say briefly why that is the pick, and what the main tradeoff or "
    "risk is.\n"
    "3. If there is an obvious next step I should take, say it in one line.\n\n"
    "If something you would need is missing, make the most reasonable "
    "assumption, say in one line what you assumed, and still give me a "
    "straight answer. Never end with only a question.\n\n"
    "You have real tools here: you can read and write files, run commands, "
    "list my other Claude Code sessions (ListAgents) and send them messages "
    "(SendMessage). Use them when they answer the question better than "
    "guessing would -- check the file rather than assuming what is in it.\n\n"
    "Two rules on those tools. Do not change files or run anything that "
    "alters my system unless I asked for that in the message you are "
    "answering; reading, checking and reporting are always fine. And never "
    "message another session without me asking you to -- if I do ask, say "
    "which session you sent it to and what you said."
)

BG = "#1c1c1e"
FG = "#f2f2f7"
MUTED = "#8e8e93"
ACCENT = "#ff8c42"
FIELD = "#2c2c2e"
PANE = "#161617"
BUB_YOU = "#3a2a1c"     # warm, matches the accent -- your side
BUB_THEM = "#26262a"    # cool grey -- the agent's side
ERRBG = "#3a1f1f"


def run_claude(text, session, first, tools=False):
    """One turn against the rewrite session. Returns (ok, output).

    `first` opens the session at the chosen id; afterwards we resume it, so
    the agent still has the prompt it wrote and every answer given since.
    `tools` swaps the deny-list for the advise allow-list."""
    if tools:
        cmd = [CLAUDE, "-p", text, "--allowedTools", *ADVISE_TOOLS]
    else:
        cmd = [CLAUDE, "-p", text, "--disallowedTools", *NO_TOOLS]
    cmd += ["--session-id", session] if first else ["--resume", session]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return False, f"claude CLI not found at {CLAUDE}"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {TIMEOUT}s"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "claude exited non-zero").strip()
    return True, p.stdout.strip()

def live_sessions():
    """Other Claude Code sessions on this machine, newest-looking first.

    Claude Code keeps a registry at ~/.claude/sessions/<pid>.json while a
    session is running. A stale file outlives a crashed session, so a name only
    counts as reachable if its pid is still alive AND it left a message socket.
    Our own headless turns are excluded -- answering yourself is never useful.
    """
    out = []
    d = os.path.expanduser("~/.claude/sessions")
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn)) as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        pid, name = j.get("pid"), j.get("name")
        if not pid or not name or not j.get("messagingSocketPath"):
            continue
        if not os.path.isdir("/proc/%s" % pid):
            continue
        out.append({"name": name, "pid": pid,
                    "status": j.get("status") or "?",
                    "sid": j.get("sessionId") or "",
                    "cwd": j.get("cwd") or ""})
    out.sort(key=lambda s: s["name"])
    return out


def send_to_session(name, message):
    """Hand `message` to another session, as a message -- not as keystrokes.

    Claude Code delivers it into that session's queue; it appears there the way
    a message from a person does, and its agent decides what to do with it. We
    never type into someone else's input box, so nothing can be executed behind
    your back.
    """
    q = ("Use SendMessage to send exactly this message to the session named "
         "%s, then reply with one line saying whether it was delivered:\n\n%s"
         % (name, message))
    cmd = [CLAUDE, "-p", q, "--allowedTools", "ListAgents", "SendMessage"]
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=TIMEOUT)
    except FileNotFoundError:
        return False, "claude CLI not found at %s" % CLAUDE
    except subprocess.TimeoutExpired:
        return False, "timed out after %ss" % TIMEOUT
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "claude exited non-zero").strip()
    return True, p.stdout.strip()


def last_question(session_id, limit=200000):
    """The last thing a session said on screen, read from its transcript.

    Claude Code appends every turn to
    ~/.claude/projects/<slug>/<session-id>.jsonl. These run to tens of MB, so
    only the tail is read -- enough for the final turn, never the whole file.
    Returns "" if nothing usable is there; the caller falls back to pasting.
    """
    base = os.path.expanduser("~/.claude/projects")
    path = ""
    try:
        for d in os.listdir(base):
            cand = os.path.join(base, d, session_id + ".jsonl")
            if os.path.exists(cand):
                path = cand
                break
    except OSError:
        return ""
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()        # drop the partial line the seek landed in
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("type") != "assistant":
            continue
        content = j.get("message", {}).get("content")
        if isinstance(content, list):
            text = "".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        if text.strip():
            return text.strip()
    return ""


def split_output(s):
    """Return (prompt, aside). The skill puts the prompt in a fenced block and
    may put a clarifying question outside it. The prompt is what gets copied;
    the aside is what goes in the chat thread -- dropping it silently turns
    "ask, then draft" into "draft on an assumption"."""
    body, aside, inside, found = [], [], False, False
    for ln in s.splitlines():
        if ln.strip().startswith("```"):
            if not inside and not found:
                inside, found = True, True
                continue
            if inside:
                inside = False
                continue
        (body if inside else aside).append(ln)
    if not (found and body):
        return "", s.strip()
    return "\n".join(body).strip(), "\n".join(aside).strip()


def round_poly(cv, x1, y1, x2, y2, r, **kw):
    """Rounded rectangle on a Canvas: a polygon stepping through the corner
    points, smoothed so the corners come out as real curves."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, splinesteps=12, **kw)


class RoundButton(tk.Canvas):
    """A button with rounded corners.

    tk.Button is drawn by the toolkit as a hard rectangle with no way to
    round it, so this is a Canvas that paints its own pill and forwards
    clicks. It mimics the parts of the Button API the app actually uses:
    config(state=...), config(text=...) and cget."""

    def __init__(self, parent, text, command, font, bg, fg,
                 hover=None, pad=(14, 5), radius=9, **kw):
        self.font, self.txt = font, text
        self.bg, self.fg = bg, fg
        self._base_bg, self._base_fg = bg, fg
        self.hover = hover or bg
        self.command, self.state = command, "normal"
        self.radius = radius
        w = font.measure(text) + pad[0] * 2
        h = font.metrics("linespace") + pad[1] * 2
        super().__init__(parent, width=w, height=h, highlightthickness=0,
                         bd=0, bg=parent.cget("bg"), cursor="hand2", **kw)
        self._draw(self.bg)
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self._draw(
            self.hover if self.state == "normal" else self.bg))
        self.bind("<Leave>", lambda e: self._draw(self.bg))

    def _draw(self, fill):
        self.delete("all")
        w, h = int(self["width"]), int(self["height"])
        dim = self.state == "disabled"
        round_poly(self, 1, 1, w - 1, h - 1, self.radius,
                   fill=FIELD if dim else fill,
                   outline=FIELD if dim else fill)
        self.create_text(w / 2, h / 2, text=self.txt, font=self.font,
                         fill=MUTED if dim else self.fg)

    def _click(self, _):
        if self.state == "normal" and self.command:
            self.command()

    def highlight(self, on):
        """Tint the pill to point at the button the next step wants."""
        want = ACCENT if on else self._base_bg
        if want == self.bg:
            return
        self.bg = want
        self.fg = "#1c1c1e" if on else self._base_fg
        self._draw(self.bg)

    def config(self, **kw):                     # only what the app calls
        if "state" in kw:
            self.state = kw.pop("state")
            # super(), not self -- self.configure is this same method, and
            # routing a cursor change back through it would recurse.
            super().configure(cursor="hand2" if self.state == "normal" else "")
        if "text" in kw:
            self.txt = kw.pop("text")
        self._draw(self.bg)
        if kw:
            super().configure(**kw)
    configure = config


def save_state(session, started, result, msgs):
    """Remember the live thread so closing the pad does not lose it.

    Claude Code keeps the real conversation under the session id; this only
    records which id to resume plus what to redraw on screen."""
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"session": session, "started": started,
                       "result": result, "msgs": msgs[-40:]}, f)
        os.replace(tmp, STATE)      # atomic: never leave a half-written file
    except OSError:
        pass


def load_state():
    try:
        with open(STATE) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        return (d.get("session", ""), bool(d.get("started")),
                d.get("result", ""), d.get("msgs") or [])
    except (OSError, ValueError):
        return None




class RoundFrame(tk.Canvas):
    """A rounded panel that hosts one child widget.

    The child is placed inside the curve via a window item, so the visible
    corners belong to the canvas while the child keeps its own behaviour."""

    def __init__(self, parent, fill, radius=10, inset=2, **kw):
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent.cget("bg"), **kw)
        self.fill, self.radius, self.inset = fill, radius, inset
        self.child = None
        self.bind("<Configure>", self._redraw)

    def hold(self, child):
        self.child = child
        self._win = self.create_window(0, 0, window=child, anchor="nw")
        self._redraw()
        return child

    def _redraw(self, _=None):
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.delete("bgshape")
        shape = round_poly(self, 0, 0, w, h, self.radius,
                           fill=self.fill, outline=self.fill)
        self.itemconfig(shape, tags="bgshape")
        self.tag_lower(shape)
        if self.child is not None:
            i = self.inset
            self.coords(self._win, i + 6, i + 4)
            self.itemconfig(self._win, width=w - (i + 6) * 2,
                            height=h - (i + 4) * 2)


class App:
    def __init__(self, root):
        self.root = root
        self.open = False
        self.result = ""        # current prompt == what Copy yields
        self.session = ""       # uuid of the live rewrite conversation
        self.started = False    # has the session been opened yet
        self.busy = False
        self.q = queue.Queue()

        root.title("Prompt Agent")
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        root.geometry("760x620+80+80")
        # A normal, WM-managed window. Rounded corners need
        # overrideredirect, but that leaves the window unmanaged: the desktop
        # never hands it the keyboard on a click, so the pad could not be
        # typed into. Typing matters more than corners. The WM now supplies
        # the titlebar, close button, move and resize, so the pad no longer
        # draws its own.
        root.overrideredirect(False)

        self.mono = tkfont.Font(family="monospace", size=10)
        self.ui = tkfont.Font(family="sans-serif", size=10)
        self.uib = tkfont.Font(family="sans-serif", size=10, weight="bold")

        # --- toolbar. The WM draws the real titlebar (name, move, close),
        # so this row keeps only the controls the WM does not provide.
        self.bar = tk.Frame(root, bg=BG)
        self.bar.pack(fill="x")
        tk.Label(self.bar, text="✎", bg=BG, fg=ACCENT,
                 font=("sans-serif", 12, "bold"), padx=8, pady=4).pack(side="left")
        self.newbtn = tk.Label(self.bar, text="new", bg=BG, fg=MUTED,
                               font=self.ui, padx=8, cursor="hand2")
        self.newbtn.pack(side="right")
        self.newbtn.bind("<Button-1>", lambda e: self.reset())
        # Advise and Explain hide the prompt pane, which used to strand a
        # rewritten prompt off-screen with no way back to it. This is that way
        # back; it only appears once there is a prompt worth returning to.
        self.panebtn = tk.Label(self.bar, text="", bg=BG, fg=ACCENT,
                                font=self.ui, padx=8, cursor="hand2")
        self.panebtn.pack(side="right")
        self.panebtn.bind("<Button-1>", lambda e: self.toggle_pane())
        self.undobtn = tk.Label(self.bar, text="", bg=BG, fg=MUTED,
                                font=self.ui, padx=8, cursor="hand2")
        self.undobtn.pack(side="right")
        self.undobtn.bind("<Button-1>", lambda e: self.undo_grab())

        # --- body: prompt pane | chat pane
        self.body = tk.Frame(root, bg=BG)
        panes = tk.Frame(self.body, bg=BG)
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        left = self.leftpane = tk.Frame(panes, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="PROMPT", bg=BG, fg=MUTED, font=self.ui,
                 anchor="w").pack(fill="x", pady=(0, 3))
        outwrap = RoundFrame(left, PANE, radius=12)
        outwrap.pack(fill="both", expand=True)
        self.out = outwrap.hold(tk.Text(
            outwrap, height=20, width=48, bg=PANE, fg=FG, font=self.mono,
            relief="flat", wrap="word", padx=4, pady=2, highlightthickness=0,
            bd=0))

        right = self.rightpane = tk.Frame(panes, bg=BG, width=300)
        right.pack(side="right", fill="both", expand=True, padx=(8, 0))
        tk.Label(right, text="CHAT", bg=BG, fg=MUTED, font=self.ui,
                 anchor="w").pack(fill="x", pady=(0, 3))
        # A Text tag background is always a rectangle, so bubbles are drawn on
        # a Canvas instead: a rounded polygon per message with the text on top.
        chatwrap = RoundFrame(right, PANE, radius=12)
        chatwrap.pack(fill="both", expand=True)
        self.chat = chatwrap.hold(tk.Canvas(
            chatwrap, bg=PANE, highlightthickness=0, bd=0,
            width=300, height=380))
        self.msgs = []          # (who, text, err) -- kept so we can re-lay out
        self.chat.bind("<Configure>", self.relayout)
        self.chat.bind("<MouseWheel>", self.on_wheel)
        self.chat.bind("<Button-4>", self.on_wheel)
        self.chat.bind("<Button-5>", self.on_wheel)

        # --- input row: one box for both the first prompt and every reply
        # Two rows, not four: the box only holds a sentence or a pasted
        # paragraph, and the height it was taking came out of the panes.
        entrywrap = RoundFrame(self.body, FIELD, radius=12, height=56)
        entrywrap.pack(padx=8, pady=(0, 6), fill="x")
        entrywrap.pack_propagate(False)
        self.entry = entrywrap.hold(tk.Text(
            entrywrap, height=2, bg=FIELD, fg=FG, insertbackground=FG,
            font=self.mono, relief="flat", wrap="word", padx=4, pady=2,
            highlightthickness=0, bd=0))
        self.entry.bind("<Control-Return>", lambda e: (self.send(), "break"))
        self.wire_edit_keys(self.entry)
        self.entry.bind("<Return>", self.on_return)
        # An empty dark box reads as disabled. A greyed hint says it is a
        # place to type, and which button the typing is headed for.
        self.hint_on = False
        self.entry.bind("<Key>", self.on_key)

        row = tk.Frame(self.body, bg=BG)
        row.pack(fill="x", padx=8, pady=(0, 8))
        self.btn = RoundButton(row, "Send", self.send, self.ui,
                               ACCENT, "#1c1c1e", hover="#ffa76b")
        self.btn.pack(side="left")
        # Paste an agent's jargon-heavy question here instead: this explains
        # it, asks back in plain words, and drafts the reply to paste.
        self.askbtn = RoundButton(row, "Explain", 
                                  lambda: self.send("translate"), self.ui,
                                  FIELD, FG, hover="#3a3a3c")
        self.askbtn.pack(side="left", padx=(6, 0))
        # Ask a straight question and get a straight answer -- no rewriting,
        # no prompt to paste anywhere.
        self.advbtn = RoundButton(row, "Advise",
                                  lambda: self.send("advise"), self.ui,
                                  FIELD, FG, hover="#3a3a3c")
        self.advbtn.pack(side="left", padx=(6, 0))
        # Pulls the last thing another terminal said, so a question does not
        # have to be copied across by hand before Explain can work on it.
        self.grabbtn = RoundButton(row, "Grab", self.grab, self.ui,
                                   FIELD, FG, hover="#3a3a3c")
        self.grabbtn.pack(side="left", padx=(6, 0))
        self.status = tk.Label(row, text="paste a prompt — enter to send",
                               bg=BG, fg=MUTED, font=self.ui)
        self.status.pack(side="left", padx=8)
        self.copybtn = RoundButton(row, "Copy", self.copy, self.ui,
                                   FIELD, FG, hover="#3a3a3c")
        self.copybtn.config(state="disabled")
        self.copybtn.pack(side="right")
        # Explain drafts the reply to the terminal's question; this delivers it
        # there instead of making you paste it back by hand. Same text as Copy.
        self.sendtobtn = RoundButton(row, "Send to", self.send_to, self.ui,
                                     FIELD, FG, hover="#3a3a3c")
        self.sendtobtn.config(state="disabled")
        self.sendtobtn.pack(side="right", padx=(0, 6))

        # The WM border handles resizing, so the pad draws no grip of its own.

        self.toggle()
        self.root.after(60, self.restore)
        self.root.after(80, self.pump)
        self.root.after(120, self.grab_focus)
        root.bind("<Escape>", lambda e: root.destroy())
        # Clicking a widget should put the caret in that widget, including the
        # BLANKS fields -- see claim_focus.
        root.bind_all("<Button-1>", self.claim_focus, add="+")
        self.hint_show("type or paste here…")

    # --- chat pane
    def round_rect(self, x1, y1, x2, y2, r, fill):
        """A rounded rectangle: a polygon whose corners step through the arc
        points, drawn smooth so the corners come out as real curves."""
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.chat.create_polygon(pts, fill=fill, outline=fill,
                                        smooth=True, splinesteps=12)

    def prompt_pane(self, show):
        """Advise has no prompt to copy, so the left pane steps aside and the
        chat takes the whole window. Re-packed before the chat frame to keep
        PROMPT on the left when it comes back."""
        packed = bool(self.leftpane.winfo_manager())
        if show and not packed:
            self.leftpane.pack(side="left", fill="both", expand=True,
                               before=self.rightpane)
        elif not show and packed:
            self.leftpane.pack_forget()
        self.pane_label()
        self.root.after(30, self.relayout)

    def pane_label(self):
        """Show the way back only when there is something to go back to."""
        if not getattr(self, "panebtn", None):
            return
        if not self.result:
            self.panebtn.config(text="")
        elif self.leftpane.winfo_manager():
            self.panebtn.config(text="hide prompt")
        else:
            self.panebtn.config(text="show prompt")

    def toggle_pane(self):
        """Bring the prompt back, or step it aside again."""
        if not self.result:
            return
        self.prompt_pane(not self.leftpane.winfo_manager())

    def say(self, who, text, err=False):
        self.msgs.append((who, text.strip(), err))
        self.relayout()
        self.chat.yview_moveto(1.0)          # newest message into view

    def relayout(self, _=None):
        """Redraw every bubble. Cheap at these message counts, and it keeps
        wrapping correct when the pane is resized."""
        self.chat.delete("all")
        pane = self.chat.winfo_width()
        if pane <= 1:                        # not mapped yet
            return
        pad, gap, radius = 10, 8, 12
        # Advice answers are long, so they get more of the pane. A rewrite
        # thread stays at 78% so you can still see which side a bubble is on.
        share = 0.92 if getattr(self, "mode", "rewrite") == "advise" else 0.78
        maxw = max(120, int(pane * share))
        y = pad

        for who, text, err in self.msgs:
            mine = who == "you"
            bubble = ERRBG if err else (BUB_YOU if mine else BUB_THEM)
            ink = "#ff9b9b" if err else FG

            # Measure by laying the text out first, then draw the bubble
            # behind it -- the wrapped height is not knowable in advance.
            probe = self.chat.create_text(0, 0, text=text, font=self.ui,
                                          width=maxw - 20, anchor="nw")
            bx1, by1, bx2, by2 = self.chat.bbox(probe)
            tw, th = bx2 - bx1, by2 - by1
            self.chat.delete(probe)

            bw = tw + 20
            x1 = pane - pad - bw if mine else pad
            hdr = self.chat.create_text(
                x1 + 10 if not mine else x1 + bw - 10, y,
                text=who, font=self.uib, fill=ACCENT if mine else MUTED,
                anchor="nw" if not mine else "ne")
            y = self.chat.bbox(hdr)[3] + 3

            self.round_rect(x1, y, x1 + bw, y + th + 16, radius, bubble)
            self.chat.create_text(x1 + 10, y + 8, text=text, font=self.ui,
                                  fill=ink, width=maxw - 20, anchor="nw")
            y += th + 16 + gap

        self.chat.config(scrollregion=(0, 0, pane, y + pad))

    def on_wheel(self, e):
        step = -1 if getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0 else 1
        self.chat.yview_scroll(step, "units")

    def claim_focus(self, _=None):
        """Take the keyboard on any click into the pad.

        focus_force() alone only moves Tk's internal focus -- on an
        unmanaged (overrideredirect) window X keeps routing keystrokes to
        whatever the WM last focused, so the pad renders fine and swallows
        every key. XSetInputFocus is what actually redirects the keyboard."""
        try:
            self._xfocus()
            # Give Tk focus to the widget actually under the click. Forcing
            # it to self.entry stole focus back from the BLANKS fields the
            # moment they were clicked, so they could not be typed into.
            w = getattr(_, "widget", None)
            if isinstance(w, (tk.Entry, tk.Text)):
                w.focus_set()
            elif self.open and self.root.focus_get() in (None, self.root):
                self.entry.focus_set()
        except tk.TclError:
            pass

    def wire_edit_keys(self, w):
        """Explicit clipboard keys.

        Tk normally supplies these, but the pad is an overrideredirect
        window and the default bindings proved unreliable there -- pasting
        into a BLANKS field did nothing. These call the widget directly."""
        def paste(ev):
            try:
                txt = self.root.clipboard_get()
            except tk.TclError:
                return "break"
            try:
                if ev.widget.selection_present():
                    ev.widget.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            ev.widget.insert("insert", txt)
            return "break"

        def copy(ev):
            try:
                sel = ev.widget.selection_get()
            except tk.TclError:
                return "break"
            self.root.clipboard_clear()
            self.root.clipboard_append(sel)
            return "break"

        def select_all(ev):
            try:
                if isinstance(ev.widget, tk.Text):
                    ev.widget.tag_add("sel", "1.0", "end-1c")
                else:
                    ev.widget.select_range(0, "end")
            except tk.TclError:
                pass
            return "break"

        w.bind("<Control-v>", paste)
        w.bind("<Control-V>", paste)
        w.bind("<Control-c>", copy)
        w.bind("<Control-C>", copy)
        w.bind("<Control-a>", select_all)
        w.bind("<Control-A>", select_all)

    def _xfocus(self):
        try:
            from Xlib import X
            if getattr(self, "_xdpy", None) is None:
                from Xlib import display
                self._xdpy = display.Display()
            w = self._xdpy.create_resource_object(
                "window", self.root.winfo_id())
            w.set_input_focus(X.RevertToParent, X.CurrentTime)
            self._xdpy.sync()
        except Exception:
            pass                    # square-corner/managed fallback still types

    def grab_focus(self):
        try:
            self.root.lift()
            self._xfocus()
            self.root.focus_force()
            self.entry.focus_set()
        except tk.TclError:
            pass

    def toggle(self, _=None):
        self.open = not self.open
        if self.open:
            self.body.pack(fill="both", expand=True)
            self.entry.focus_set()
        else:
            self.body.pack_forget()
            self.root.geometry("")

    def hint_show(self, text):
        """Grey placeholder, only while the box is genuinely empty."""
        if self.entry.get("1.0", "end").strip():
            return
        self.hint_on = True
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        self.entry.config(fg=MUTED)

    def on_key(self, ev):
        """Real typing clears the placeholder; arrows and modifiers do not."""
        if ev.char and ev.char.isprintable():
            self.hint_clear()

    def hint_clear(self):
        if getattr(self, "hint_on", False):
            self.hint_on = False
            self.entry.delete("1.0", "end")
            self.entry.config(fg=FG)

    def typed(self):
        """What the user actually wrote -- never the placeholder."""
        if getattr(self, "hint_on", False):
            return ""
        return self.entry.get("1.0", "end").strip()

    def on_return(self, _):
        """Enter sends; shift+enter is a newline (handled by not binding it)."""
        self.send()
        return "break"

    def restore(self):
        """Bring back the thread from the last time the pad was open."""
        got = load_state()
        if not got:
            return
        session, started, result, msgs = got
        if not (result or msgs):
            return
        self.session, self.started, self.result = session, started, result
        if result:
            self.out.delete("1.0", "end")
            self.out.insert("1.0", result)
            self.copybtn.config(state="normal")
            self.sendtobtn.config(state="normal")
            self.pane_label()
        for m in msgs:
            try:
                who, text, err = m
            except (TypeError, ValueError):
                continue
            self.say(who, text, err=err)
        # No saved prompt but a thread present means the last session was
        # advice, which keeps everything in the chat.
        if msgs and not result:
            self.mode = "advise"
            self.prompt_pane(False)
        self.status.config(
            text="picked up where you left off — New starts over")

    def reset(self):
        """Drop the thread and start a fresh prompt."""
        self.session, self.started, self.result = "", False, ""
        self.msgs = []
        self.chat.delete("all")
        self.out.delete("1.0", "end")
        self.copybtn.config(state="disabled")
        self.sendtobtn.config(state="disabled")
        self.pane_label()
        self.mode = "rewrite"
        self.prompt_pane(True)
        self.status.config(text="paste a prompt — enter to send")
        self.hint_show("type or paste here…")
        self.entry.focus_set()
        try:
            os.remove(STATE)
        except OSError:
            pass

    # --- run
    def send(self, mode="rewrite"):
        self.askbtn.highlight(False)
        if self.busy:
            return
        text = self.typed()
        if not text:
            return
        follow_up = False
        if mode in ("translate", "advise"):
            # Its own session: these threads are a Q&A, and mixing them into
            # the rewrite history would leave the agent revising the wrong
            # thing.
            self.session = str(uuid.uuid4())
            self.started = False
            if mode == "advise":
                payload = f"{ADVISE}\n\nMy question:\n{text}"
            else:
                payload = f"{ASK}\n\nThe agent asked:\n{text}"
        elif not self.started:
            self.session = str(uuid.uuid4())
            payload = f"/{SKILL} {text}\n\n{STYLE}"
        elif getattr(self, "mode", "rewrite") in ("advise", "translate"):
            # A reply inside an advice thread is a follow-up question, not a
            # prompt to rewrite. Asking for a fenced block here turned
            # "what about speed?" into a rewritten prompt instead of an answer.
            payload = text
            follow_up = True
        else:
            payload = f"{text}\n\n{REPLY}"
        self.say("you", text)
        self.entry.delete("1.0", "end")
        self.busy = True
        self.btn.config(state="disabled")
        self.askbtn.config(state="disabled")
        self.advbtn.config(state="disabled")
        self.status.config(text="thinking…")
        # A follow-up resumes the thread; only a fresh button press starts one.
        first = not follow_up and (not self.started
                                   or mode in ("translate", "advise"))
        if not follow_up:
            self.mode = mode
        self.prompt_pane(self.mode != "advise")
        threading.Thread(
            target=lambda: self.q.put(run_claude(payload, self.session, first)),
            daemon=True).start()

    def pump(self):
        try:
            self._pump_once()
        except Exception:
            # A crash here used to kill the whole pad with no message, since
            # the loop only re-armed on the success path and stdout is not
            # on screen. Log it and keep the pad alive.
            import traceback
            traceback.print_exc()
            try:
                self.busy = False
                self.btn.config(state="normal")
                self.askbtn.config(state="normal")
                self.advbtn.config(state="normal")
                self.status.config(text="internal error — see log")
            except Exception:
                pass
        self.root.after(80, self.pump)

    def _pump_once(self):
        try:
            item = self.q.get_nowait()
        except queue.Empty:
            return
        # A delivery to another terminal is not a turn of the thread: it must
        # not touch self.result, the prompt pane, or the saved session.
        if item and item[0] == "sendto":
            _, name, ok, out = item
            self.sendtobtn.config(state="normal")
            if ok:
                self.say("agent", "Sent to %s.\n\n%s" % (name, out.strip()))
                self.status.config(text="sent to %s" % name)
            else:
                self.say("agent", "Could not send to %s: %s" % (name, out),
                         err=True)
                self.status.config(text="send failed")
            save_state(self.session, self.started, self.result, self.msgs)
            return
        ok, payload = item
        self.busy = False
        self.btn.config(state="normal")
        self.askbtn.config(state="normal")
        self.advbtn.config(state="normal")
        if ok:
            self.started = True
            prompt, aside = split_output(payload)
            if getattr(self, "mode", "rewrite") == "advise" and not prompt:
                # In advise mode the answer IS the deliverable -- there is
                # no prompt to copy. It goes to the chat in full, and the
                # prompt pane hides so the thread gets the whole window.
                # Truncating it into a bubble read as a cut-off answer.
                aside = payload.strip()
            if prompt:
                self.result = prompt
                self.out.delete("1.0", "end")
                self.out.insert("1.0", prompt)
                self.copybtn.config(state="normal")
                self.sendtobtn.config(state="normal")
                self.pane_label()
            if aside:
                self.say("agent", aside)
            elif prompt:
                self.say("agent", "(prompt updated)")
            save_state(self.session, self.started, self.result, self.msgs)
            if getattr(self, "mode", "rewrite") == "advise":
                self.status.config(text="advice above — reply to dig in")
                self.hint_show("ask a follow-up…")
                # Advice writes no prompt, but it is still text you may want
                # to hand to a terminal, so Send to stays usable.
                if self.outgoing():
                    self.sendtobtn.config(state="normal")
            elif not prompt:
                # A question answered in the chat: the prompt on the left
                # is untouched and still the thing worth copying.
                self.status.config(
                    text="answered in the chat — prompt on the left is "
                         "unchanged")
            else:
                # The prompt is always usable -- a question is optional.
                self.status.config(
                    text="ready to copy — reply only if you want to refine")
                self.hint_show("reply to refine it…")
        else:
            self.say("agent", payload, err=True)
            self.status.config(text="failed")
        self.entry.focus_set()

    def grab(self):
        """Pull the last message from another terminal into the input box.

        Same picker as Send to, so one list of sessions serves both directions:
        grab the question from a terminal, and send the answer back to it."""
        peers = [x for x in live_sessions() if x["pid"] != os.getpid()]
        if not peers:
            self.status.config(text="no other Claude sessions running")
            return
        self.pick_session("Grab the last message from which terminal?",
                          self._grabbed)

    def _grabbed(self, peer):
        text = last_question(peer["sid"])
        if not text:
            self.status.config(
                text="nothing to grab from %s yet" % peer["name"])
            return
        # Remembered so Send to can offer the same terminal back without
        # making you find it in the list a second time.
        self.grabbed_from = peer["name"]
        # Whatever was half-typed in the box is not worth silently destroying;
        # putting it back is one click on "undo grab".
        self.pre_grab = self.typed()
        self.hint_on = False
        self.entry.config(fg=FG)
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        self.entry.focus_set()
        # A grabbed question wants Explain, not Advise. The status line said
        # so and got missed under a wall of grabbed text, so the button
        # itself now says it.
        self.askbtn.highlight(True)
        if self.pre_grab:
            self.status.config(text="grabbed from %s — press Explain "
                                    "(undo grab restores your text)"
                                    % peer["name"])
        else:
            self.status.config(
                text="grabbed from %s — press Explain" % peer["name"])
        self.undo_label()

    def undo_label(self):
        """Offer the typed-over text back, and only while it exists."""
        if not getattr(self, "undobtn", None):
            return
        self.undobtn.config(text="undo grab" if getattr(self, "pre_grab", "")
                            else "")

    def undo_grab(self):
        if not getattr(self, "pre_grab", ""):
            return
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", self.pre_grab)
        self.pre_grab = ""
        self.undo_label()
        self.status.config(text="your text is back")

    def pick_session(self, title, on_pick):
        """A small list of the live sessions. Nothing happens until a click."""
        peers = [x for x in live_sessions() if x["pid"] != os.getpid()]
        if not peers:
            self.status.config(text="no other Claude sessions running")
            return
        win = tk.Toplevel(self.root)
        win.title("Sessions")
        win.configure(bg=BG)
        win.transient(self.root)
        tk.Label(win, text=title, bg=BG, fg=FG,
                 font=self.ui).pack(padx=14, pady=(12, 8), anchor="w")
        for peer in peers:
            label = "%s  ·  %s  ·  pid %s" % (peer["name"], peer["status"],
                                              peer["pid"])
            RoundButton(win, label,
                        lambda p=peer: (win.destroy(), on_pick(p)),
                        self.ui, FIELD, FG,
                        hover="#3a3a3c").pack(anchor="w", padx=14, pady=3)
        cancel = tk.Label(win, text="cancel", bg=BG, fg=MUTED, font=self.ui,
                          cursor="hand2")
        cancel.pack(pady=(8, 12))
        cancel.bind("<Button-1>", lambda e: win.destroy())
        win.update_idletasks()
        win.geometry("+%d+%d" % (self.root.winfo_rootx() + 40,
                                 self.root.winfo_rooty() + 60))

    def outgoing(self):
        """The text Send to would deliver.

        Normally the prompt pane -- that is what Explain drafts and what Copy
        yields. In advise mode there is no prompt pane, and the answer itself
        is the thing worth sending, so the last agent message stands in."""
        if self.result:
            return self.result
        for who, text, err in reversed(self.msgs):
            if who == "agent" and not err:
                return text
        return ""

    def send_to(self):
        """Deliver the drafted reply to whichever terminal asked the question.

        You pick the session by name; nothing is sent until you click one. The
        text delivered is exactly what Copy would put on the clipboard, so what
        you see on the left is what the other session gets."""
        if not self.outgoing():
            return
        title = "Send this reply to which terminal?"
        was = getattr(self, "grabbed_from", "")
        if was:
            title = "Send this reply back to %s?" % was
        self.pick_session(title, self._deliver)

    def _deliver(self, peer):
        """Hand the reply to the chosen session, off the UI thread."""
        text = self.outgoing()
        name = peer["name"]
        self.sendtobtn.config(state="disabled")
        self.status.config(text="sending to %s…" % name)
        self.say("you", "Send to %s:\n\n%s" % (name, text))

        def work():
            ok, out = send_to_session(name, text)
            self.q.put(("sendto", name, ok, out))

        threading.Thread(target=work, daemon=True).start()

    def copy(self):
        if not self.result:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.result)
        self.root.update()
        self.status.config(text="copied")


def _report(exc, val, tb):
    import traceback
    traceback.print_exception(exc, val, tb)


def main():
    """Console entry point (`prompt-agent`)."""
    ensure_skill()
    tk.Tk.report_callback_exception = staticmethod(_report)
    try:
        r = tk.Tk(className="Prompt-agent")
    except tk.TclError as exc:
        # No display: the usual cause is SSH without X forwarding, or a
        # headless box. A traceback here reads as a crash, so say it plainly.
        print(f"cannot open a window: {exc}\n"
              "prompt-agent is a desktop app and needs a graphical session.")
        return 1
    App(r)
    r.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
