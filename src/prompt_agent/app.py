#!/usr/bin/env python3
"""Floating prompt-rewrite pad.

Left pane: the current prompt (always the thing Copy puts on the clipboard).
Right pane: a chat thread with the rewriting agent. Answer its questions
inline and the prompt on the left is replaced with each revision.

The thread is a real Claude Code session -- turn 1 creates it with a fixed
--session-id, later turns --resume it -- so the agent remembers the prompt it
already wrote instead of starting over from the raw text each time."""

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
NO_TOOLS = ["Write", "Edit", "Bash", "Agent", "Task"]


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
    a venv that gets replaced on upgrade, taking facts.md with it."""
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
FACTS = os.path.join(CONFIG, "facts.md")
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

FACTS_TEMPLATE = """# Standing facts

Facts the rewriter should assume instead of asking about. Edit this file
whenever something changes -- everything here is injected into every rewrite,
so a fact listed here never comes back as [NEEDED: ...].

Delete a line if it stops being true. A bullet still holding a [placeholder]
is treated as unfilled and is skipped.

## Me

- Name: [your name]
- Business: [what you do, in one line]

## Defaults

- [anything you always want assumed -- tone, format, house rules]
"""


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


def ensure_facts():
    """Create a starter facts.md the first time, so the facts button always
    opens something instead of failing on a missing file."""
    try:
        if not os.path.exists(FACTS):
            with open(FACTS, "w") as f:
                f.write(FACTS_TEMPLATE)
            return True
    except OSError:
        pass
    return False


def load_facts():
    """Standing facts, injected into turn 1 so they never come back as
    [NEEDED: ...].

    Only "- " bullets under "## " headings are facts; the prose at the top is
    instructions to the human. A bullet still holding a [placeholder] is an
    unfilled template and is dropped -- handing one over would plant an
    invented fact in every prompt. A bullet can wrap onto continuation lines,
    so it is assembled whole before that check."""
    try:
        with open(FACTS) as f:
            raw = f.read()
    except OSError:
        return ""

    out, bullet, heading = [], None, None

    def flush():
        if bullet and "[" not in " ".join(bullet):
            if heading and heading not in out:
                out.append(heading)
            out.append(" ".join(bullet))

    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("##"):
            flush()
            bullet, heading = None, s
        elif s.startswith("- "):
            flush()
            bullet = [s]
        elif s and bullet is not None:
            bullet.append(s)          # wrapped continuation of this bullet
        else:
            flush()
            bullet = None
    flush()
    return "\n".join(out).strip()

# The pad is a rewrite tool, not an intake interview. The usable prompt is the
# deliverable every single turn; a question is optional and never a gate.
STYLE = (
    "This is a quick rewrite pad, not an interview. Always return the finished "
    "prompt. Do not interrogate the user: skip the question entirely unless one "
    "unknown genuinely changes the prompt, and then ask at most ONE short "
    "question -- never two joined by 'and'. Mark other unknowns [NEEDED: ...] "
    "inside the prompt rather than asking about them. The user can ignore any "
    "question and still have something they can paste. "
    "Use [NEEDED: ...] ONLY as an actual blank standing where a missing fact "
    "goes. Never mention the marker in an instruction to the reader -- do not "
    "write sentences like 'mark unverified facts [NEEDED: ...]'. If nothing is "
    "missing, the prompt contains no [NEEDED] at all.\n\n"
    "Some input is a QUESTION, not a job to do -- the user wants an opinion, "
    "an explanation, a comparison or a recommendation. Recognise that case and "
    "rewrite it as a sharper version of the SAME question. Then: no Task / "
    "Constraints / Deliverable / Done-when sections, and NO [NEEDED] blanks at "
    "all. A question does not need a repo name, a file path, a framework or a "
    "success criterion -- if the answer would change depending on some detail, "
    "write that into the question as a plain clause ('...and say what it "
    "depends on') instead of demanding the detail up front. Keep the user's "
    "own words and their level of technical language; sharpen the question, do "
    "not make it sound like an engineer wrote it."
)

# Translate mode. The opposite of STYLE: here the questions ARE the point.
# The agent's job is to decode jargon and hand back a reply Moe can paste.
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
    "straight answer. Never end with only a question."
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


def run_claude(text, session, first):
    """One turn against the rewrite session. Returns (ok, output).

    `first` opens the session at the chosen id; afterwards we resume it, so
    the agent still has the prompt it wrote and every answer given since."""
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
        import json
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"session": session, "started": started,
                       "result": result, "msgs": msgs[-40:]}, f)
        os.replace(tmp, STATE)      # atomic: never leave a half-written file
    except OSError:
        pass


def load_state():
    try:
        import json
        with open(STATE) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return None
        return (d.get("session", ""), bool(d.get("started")),
                d.get("result", ""), d.get("msgs") or [])
    except (OSError, ValueError):
        return None


def save_fact(label, value):
    """Append one answered blank to facts.md so it is never asked again.

    Written under a "## Learned" heading so hand-written sections stay as the
    user arranged them. Returns False if the fact is already recorded --
    re-answering the same blank must not stack duplicate lines."""
    line = f"- {label.strip().rstrip(':')}: {value.strip()}"
    try:
        try:
            with open(FACTS) as f:
                cur = f.read()
        except OSError:
            cur = ""
        if line in cur:
            return False
        if "## Learned" not in cur:
            cur = cur.rstrip() + "\n\n## Learned\n\n"
        else:
            cur = cur.rstrip() + "\n"
        with open(FACTS, "w") as f:
            f.write(cur + line + "\n")
        return True
    except OSError:
        return False


def find_blanks(prompt):
    """Every [NEEDED: ...] in the prompt, as (whole_marker, label).

    Scanned with a bracket counter rather than a regex so a label that itself
    contains brackets still yields one blank instead of a truncated one.
    Duplicates collapse: the same question asked twice gets one box, and
    filling it replaces every copy."""
    out, i, seen = [], 0, set()
    while True:
        i = prompt.find("[NEEDED", i)
        if i < 0:
            return out
        depth, j = 0, i
        while j < len(prompt):
            if prompt[j] == "[":
                depth += 1
            elif prompt[j] == "]":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth:                       # unclosed marker -- ignore it
            return out
        whole = prompt[i:j + 1]
        label = whole[len("[NEEDED"):-1].lstrip(":").strip() or "value"
        if whole not in seen:
            seen.add(whole)
            out.append((whole, label))
        i = j + 1


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
        self.factbtn = tk.Label(self.bar, text="facts", bg=BG, fg=MUTED,
                                font=self.ui, padx=8, cursor="hand2")
        self.factbtn.pack(side="right")
        self.factbtn.bind("<Button-1>", lambda e: self.edit_facts())

        # --- body: prompt pane | chat pane
        self.body = tk.Frame(root, bg=BG)
        panes = tk.Frame(self.body, bg=BG)
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        left = tk.Frame(panes, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="PROMPT", bg=BG, fg=MUTED, font=self.ui,
                 anchor="w").pack(fill="x", pady=(0, 3))
        outwrap = RoundFrame(left, PANE, radius=12)
        outwrap.pack(fill="both", expand=True)
        self.out = outwrap.hold(tk.Text(
            outwrap, height=20, width=48, bg=PANE, fg=FG, font=self.mono,
            relief="flat", wrap="word", padx=4, pady=2, highlightthickness=0,
            bd=0))

        # --- BLANKS: one field per [NEEDED: ...] the rewrite left behind.
        # Packed only when there is something to fill in.
        self.blanks_wrap = tk.Frame(left, bg=BG)
        hdr = tk.Frame(self.blanks_wrap, bg=BG)
        hdr.pack(fill="x", pady=(8, 3))
        tk.Label(hdr, text="BLANKS", bg=BG, fg=ACCENT, font=self.ui,
                 anchor="w").pack(side="left")
        self.fillbtn = RoundButton(hdr, "Fill in", self.fill_blanks, self.ui,
                                   ACCENT, "#1c1c1e", hover="#ffa76b",
                                   pad=(12, 3))
        self.fillbtn.pack(side="right")
        self.blanks_box = tk.Frame(self.blanks_wrap, bg=PANE)
        self.blanks_box.pack(fill="x")
        self.blank_rows = []            # (marker, Entry)

        right = tk.Frame(panes, bg=BG, width=300)
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
        entrywrap = RoundFrame(self.body, FIELD, radius=12, height=88)
        entrywrap.pack(padx=8, pady=(0, 6), fill="x")
        entrywrap.pack_propagate(False)
        self.entry = entrywrap.hold(tk.Text(
            entrywrap, height=4, bg=FIELD, fg=FG, insertbackground=FG,
            font=self.mono, relief="flat", wrap="word", padx=4, pady=2,
            highlightthickness=0, bd=0))
        self.entry.bind("<Control-Return>", lambda e: (self.send(), "break"))
        self.wire_edit_keys(self.entry)
        self.entry.bind("<Return>", self.on_return)

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
        self.status = tk.Label(row, text="paste a prompt — enter to send",
                               bg=BG, fg=MUTED, font=self.ui)
        self.status.pack(side="left", padx=8)
        self.copybtn = RoundButton(row, "Copy", self.copy, self.ui,
                                   FIELD, FG, hover="#3a3a3c")
        self.copybtn.config(state="disabled")
        self.copybtn.pack(side="right")

        # The WM border handles resizing, so the pad draws no grip of its own.

        self.toggle()
        self.root.after(60, self.restore)
        self.root.after(80, self.pump)
        self.root.after(120, self.grab_focus)
        root.bind("<Escape>", lambda e: root.destroy())
        # Clicking a widget should put the caret in that widget, including the
        # BLANKS fields -- see claim_focus.
        root.bind_all("<Button-1>", self.claim_focus, add="+")

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
        maxw = max(120, int(pane * 0.78))    # leaves the other side showing
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

    def on_return(self, _):
        """Enter sends; shift+enter is a newline (handled by not binding it)."""
        self.send()
        return "break"

    # --- blanks
    def show_blanks(self):
        """Rebuild the fill-in fields from whatever the current prompt needs."""
        for w in self.blanks_box.winfo_children():
            w.destroy()
        self.blank_rows = []

        blanks = find_blanks(self.result)
        if not blanks:
            self.blanks_wrap.pack_forget()
            return

        for marker, label in blanks:
            row = tk.Frame(self.blanks_box, bg=PANE)
            row.pack(fill="x", padx=6, pady=3)
            tk.Label(row, text=label, bg=PANE, fg=MUTED, font=self.ui,
                     anchor="w", wraplength=360, justify="left").pack(fill="x")
            e = tk.Entry(row, bg=FIELD, fg=FG, insertbackground=FG,
                         font=self.mono, relief="flat", highlightthickness=1,
                         highlightbackground=FIELD, highlightcolor=ACCENT)
            e.pack(fill="x", ipady=3, pady=(2, 0))
            e.bind("<Return>", lambda ev: (self.fill_blanks(), "break"))
            self.wire_edit_keys(e)
            # Ticked by default: a fact worth typing once is nearly always
            # worth keeping, and an unwanted line is one edit away.
            keep = tk.BooleanVar(value=True)
            tk.Checkbutton(row, text="remember this", variable=keep,
                           bg=PANE, fg=MUTED, font=self.ui,
                           selectcolor=FIELD, activebackground=PANE,
                           activeforeground=FG, bd=0, highlightthickness=0,
                           anchor="w").pack(fill="x", pady=(1, 0))
            self.blank_rows.append((marker, e, keep, label))

        self.blanks_wrap.pack(fill="x")

    def fill_blanks(self):
        """Substitute the filled-in answers into the prompt.

        Only non-empty fields are substituted, so a blank you skip stays a
        visible [NEEDED: ...] rather than collapsing into an empty string --
        an unnoticed empty slot is worse than an obvious marker."""
        filled, learned = 0, 0
        for marker, entry, keep, label in self.blank_rows:
            val = entry.get().strip()
            if val:
                self.result = self.result.replace(marker, val)
                filled += 1
                if keep.get() and save_fact(label, val):
                    learned += 1
        if not filled:
            self.status.config(text="nothing filled in yet")
            return
        self.out.delete("1.0", "end")
        self.out.insert("1.0", self.result)
        self.show_blanks()
        left = len(self.blank_rows)
        tail = f" — {left} left" if left else " — ready to copy"
        note = f", {learned} saved to facts" if learned else ""
        self.status.config(text=f"filled {filled}{note}{tail}")

    def edit_facts(self):
        """Open facts.md in the desktop's default editor."""
        try:
            ensure_facts()
            open_in_editor(FACTS)
            self.status.config(text="facts.md opened — saved edits apply to "
                                    "the next new prompt")
        except OSError as exc:
            self.status.config(text=f"could not open facts.md: {exc}")

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
            self.show_blanks()
        for m in msgs:
            try:
                who, text, err = m
            except (TypeError, ValueError):
                continue
            self.say(who, text, err=err)
        self.status.config(
            text="picked up where you left off — New starts over")

    def reset(self):
        """Drop the thread and start a fresh prompt."""
        self.session, self.started, self.result = "", False, ""
        self.msgs = []
        self.chat.delete("all")
        self.out.delete("1.0", "end")
        self.copybtn.config(state="disabled")
        self.show_blanks()
        self.status.config(text="paste a prompt — enter to send")
        self.entry.focus_set()
        try:
            os.remove(STATE)
        except OSError:
            pass

    # --- run
    def send(self, mode="rewrite"):
        if self.busy:
            return
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return
        if mode in ("translate", "advise"):
            # Its own session: these threads are a Q&A, and mixing them into
            # the rewrite history would leave the agent revising the wrong
            # thing.
            self.session = str(uuid.uuid4())
            self.started = False
            facts = load_facts()
            known = (f"\n\nStanding facts about me:\n{facts}"
                     if facts else "")
            if mode == "advise":
                payload = f"{ADVISE}\n\nMy question:\n{text}{known}"
            else:
                payload = f"{ASK}\n\nThe agent asked:\n{text}{known}"
        elif not self.started:
            self.session = str(uuid.uuid4())
            facts = load_facts()
            known = (f"\n\nStanding facts about me -- use these directly, and "
                     f"never ask about or mark [NEEDED] anything answered "
                     f"here:\n{facts}" if facts else "")
            payload = f"/{SKILL} {text}{known}\n\n{STYLE}"
        else:
            payload = (f"{text}\n\nReturn the full revised prompt in a fenced "
                       f"block. {STYLE}")
        self.say("you", text)
        self.entry.delete("1.0", "end")
        self.busy = True
        self.btn.config(state="disabled")
        self.askbtn.config(state="disabled")
        self.advbtn.config(state="disabled")
        self.status.config(text="thinking…")
        first = not self.started or mode in ("translate", "advise")
        self.mode = mode
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
            ok, payload = self.q.get_nowait()
        except queue.Empty:
            pass
        else:
            self.busy = False
            self.btn.config(state="normal")
            self.askbtn.config(state="normal")
            self.advbtn.config(state="normal")
            if ok:
                self.started = True
                prompt, aside = split_output(payload)
                if getattr(self, "mode", "rewrite") == "advise" and not prompt:
                    # Advice is prose, not a fenced prompt. Without this it
                    # would all land in the narrow chat strip and the big
                    # pane would sit empty.
                    prompt, aside = payload.strip(), ""
                if prompt:
                    self.result = prompt
                    self.out.delete("1.0", "end")
                    self.out.insert("1.0", prompt)
                    self.copybtn.config(state="normal")
                    self.show_blanks()
                if aside:
                    self.say("agent", aside)
                elif getattr(self, "mode", "rewrite") != "advise":
                    self.say("agent", "(prompt updated)")
                save_state(self.session, self.started, self.result, self.msgs)
                if getattr(self, "mode", "rewrite") == "advise":
                    self.status.config(text="advice above — reply to dig in")
                else:
                    # The prompt is always usable -- a question is optional.
                    self.status.config(
                        text="ready to copy — reply only if you want to refine")
            else:
                self.say("agent", payload, err=True)
                self.status.config(text="failed")
            self.entry.focus_set()

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
    ensure_facts()
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
