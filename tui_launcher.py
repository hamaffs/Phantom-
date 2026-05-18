"""Phantom interactive launcher (Option A).

When the user runs `phantom` with no arguments and stdout is a TTY, we
open this. It's a tiny REPL — banner on top, single-line input below,
keybinding hints at the bottom — that lets you queue scans, jump into
the full Textual TUI, run --self-check, and so on without retyping the
command each time.

Design:
- Zero external dependencies. Pure stdlib, works in any reasonable
  terminal (Python 3.11+ stdlib supports the ANSI codes we use).
- The "scan" action just rebuilds an argv list and calls `cli.main`
  in-process. That keeps a single source of truth for what each flag
  does, and any improvements to the scan engine show up here for free.
- Single-key shortcuts work without Enter where reasonable (E, F, S,
  T, Q, H) via raw-mode tty.

Keybindings (visible at the bottom of the screen):
    Enter   scan the current input
    [E]     set --export path (html/pdf/json/md/csv/mmd)
    [F]     open the flag menu (Phase 1-5 toggles)
    [G]     set --graph path (json/gexf/html cytoscape)
    [A]     set --analyze-out path (LLM analyst JSON)
    [S]     --self-check
    [T]     launch the Textual full TUI
    [H]     help
    [Q]     quit
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys

import termios
import tty
from pathlib import Path
from typing import Optional

from tui_assets import (
    ACCENT, BOLD, DIM, ERR, INK, MUTED, RESET,
    c, hr, key_hint, render_banner, render_section_rich, status_line,
)


def banner(color: bool = True) -> str:
    """Backwards-compat: existing code calls banner(); delegate to the
    new gradient-coloured renderer."""
    return render_banner()


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------
CLEAR = "\033[2J\033[H"           # clear screen + home cursor
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CURSOR_UP = lambda n: f"\033[{n}A"  # noqa: E731
ERASE_LINE = "\033[2K\r"
# Bracketed / synchronized output (DECSCUSR mode 2026) - the terminal
# buffers everything between BEGIN and END and applies it in one
# repaint. Eliminates the I-beam cursor flicker that the user saw as
# stray `|` characters between sections during multi-step redraws.
SYNC_BEGIN = "\033[?2026h"
SYNC_END = "\033[?2026l"
# Alternate screen buffer (xterm extension, supported by every modern
# terminal: Konsole, kitty, iTerm2, Alacritty, WezTerm, Foot, GNOME
# Terminal, etc). Opens a fresh blank screen on enter, restores the
# original terminal contents on exit. Critically: keeps redraws OUT
# of the scrollback, so previous frames don't leak above the current
# one when the user scrolls up. Same mechanism used by vim / less /
# htop / man.
ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
# Solid background fill. When the user has Konsole / Alacritty / iTerm2
# with window opacity < 100%, the "empty" cells of the alt screen show
# whatever's behind the terminal (other windows, the desktop). Filling
# every visible row with a bg-coloured space row makes the alt screen
# fully opaque - same trick vim, less, and htop use. Colour 232 is the
# darkest non-black in the 256-colour palette; visually identical to
# black on any sane theme but explicitly OPAQUE.
BG_DARK = "\033[48;5;232m"
BG_RESET = "\033[49m"


def _solid_screen_fill() -> str:
    """Build a screen-spanning solid-bg fill. Goes BEFORE the actual
    content in each repaint so any cells the content doesn't write to
    still have an opaque background."""
    try:
        size = shutil.get_terminal_size((80, 24))
        cols, rows = size.columns, size.lines
    except Exception:
        cols, rows = 80, 24
    blank = " " * cols
    return "\033[H" + BG_DARK + ("\n".join([blank] * rows)) + "\033[H"


def term_width() -> int:
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


def _read_key() -> str:
    """Read a single keypress in raw mode. Returns the character or one
    of the synthetic names: ENTER, ESC, BACKSPACE, ARROW_*, EOF.

    Falls back to a blocking input() when stdin isn't a tty (unusual
    for the launcher but defensive)."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return "EOF" if not line else line.rstrip("\n")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\r" or ch == "\n":
        return "ENTER"
    if ch == "\x7f" or ch == "\x08":
        return "BACKSPACE"
    if ch == "\x1b":
        # Possible escape sequence - read up to 2 more chars
        nxt = sys.stdin.read(1) if sys.stdin.readable() else ""
        if not nxt:
            return "ESC"
        if nxt == "[":
            arrow = sys.stdin.read(1) if sys.stdin.readable() else ""
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(
                arrow, "ESC"
            )
        return "ESC"
    return ch


def _line_edit(prompt: str, initial: str = "") -> Optional[str]:
    """Read a line with backspace + cursor, render the prompt, return
    the entered text. Returns None when Ctrl-C / ESC pressed."""
    buf = list(initial)
    sys.stdout.write(prompt + initial + SHOW_CURSOR)
    sys.stdout.flush()
    while True:
        try:
            k = _read_key()
        except KeyboardInterrupt:
            return None
        if k == "ENTER":
            sys.stdout.write("\n")
            return "".join(buf).strip()
        if k == "ESC":
            return None
        if k == "BACKSPACE":
            if buf:
                buf.pop()
                # Move cursor back, erase one char, move back again.
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if k == "EOF":
            return None
        if k in ("UP", "DOWN", "LEFT", "RIGHT"):
            continue
        if len(k) == 1 and k.isprintable():
            buf.append(k)
            sys.stdout.write(k)
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
# Names of every toggleable boolean attribute on LauncherState. Used by
# the persistence layer (save/load) AND by the preset system so a single
# source of truth governs which fields are flags.
_TOGGLE_ATTRS = (
    "exact", "no_cache", "no_identity", "found_only", "analyze",
)


# Three presets: fast (--exact), default (everything auto-on),
# default + LLM analyst.
_PRESETS: list[tuple[str, str, str, dict]] = [
    (
        "1", "Quick",
        "Fast scan — exact handle, single variant, no enrichment. ~15s.",
        {a: False for a in _TOGGLE_ATTRS} | {"exact": True, "found_only": True},
    ),
    (
        "2", "Default",
        "30 variants + full enrichment (expand, wayback, github-deep, photo-ocr). ~3-5 min.",
        {a: False for a in _TOGGLE_ATTRS} | {"found_only": True},
    ),
    (
        "3", "Default + LLM Analyst",
        "Default scan plus Claude/Groq analyst (dossier, contradictions, pivots). +30-60s.",
        {a: False for a in _TOGGLE_ATTRS} | {"found_only": True, "analyze": True},
    ),
    (
        "4", "Reset",
        "Clear every flag and start fresh.",
        {a: False for a in _TOGGLE_ATTRS} | {"found_only": True},
    ),
]


class LauncherState:
    """All the toggleable flags the launcher exposes via [F].

    Each attr maps to an argparse flag. The launcher composes argv from
    these when the user presses Enter."""
    def __init__(self) -> None:
        self.handle: str = ""
        self.export_path: Optional[str] = None  # set via [E]
        # ----- Discovery / scan behaviour -----
        # expand/wayback/github_deep/photo_ocr/tls_rotate/simulate_session
        # are NOT here anymore - they're auto-on for non-exact scans (see
        # cli.py's _enrich block). `exact` is the single escape hatch.
        self.exact: bool = False
        self.no_cache: bool = False
        self.no_identity: bool = False
        self.found_only: bool = True
        # ----- Phase 1-3: Graph layer -----
        self.graph_path: Optional[str] = None      # set via [G]
        # ----- Phase 4: LLM analyst -----
        self.analyze: bool = False
        self.analyze_out: Optional[str] = None     # set via [A]

    def argv(self) -> list[str]:
        """Build the argv list for cli.main(). Mirrors the actual CLI."""
        out: list[str] = []
        if self.handle:
            # Allow URLs via --parse; otherwise positional.
            if "://" in self.handle:
                out += ["--parse", self.handle]
            else:
                out += [self.handle]
        if self.exact:
            out.append("--exact")
        if self.found_only:
            out.append("--found-only")
        if self.no_cache:
            out.append("--no-cache")
        if self.no_identity:
            out.append("--no-identity")
        # expand/wayback/github-deep/photo-ocr/tls-rotate/simulate-session
        # are no longer emitted - cli auto-fires them when --exact is unset.
        if self.graph_path:
            out += ["--graph", self.graph_path]
        if self.analyze:
            out.append("--analyze")
        if self.analyze_out:
            out += ["--analyze-out", self.analyze_out]
        if self.export_path:
            out += ["--export", self.export_path]
        return out


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Persistence - flag toggles survive across launcher sessions
# ---------------------------------------------------------------------------
def _state_config_path() -> Path:
    """Where the persisted launcher state lives. Mirrors apis.config_path()
    so all phantom config is XDG-compliant and in one directory."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "phantom" / "launcher.json"


def _save_state(state: LauncherState) -> None:
    """Persist the boolean toggles to disk. Paths (handle, export_path,
    graph_path, analyze_out) are per-target and NOT persisted — saving
    them would surprise users who don't remember which target's paths
    are loaded next session."""
    p = _state_config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {attr: getattr(state, attr) for attr in _TOGGLE_ATTRS}
        p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, TypeError):
        # Best-effort: a broken save shouldn't crash the launcher.
        pass


def _load_state_into(state: LauncherState) -> None:
    """Apply persisted toggles to `state` (in place). Silent on missing
    or malformed config — fall back to whatever defaults LauncherState
    initialized with."""
    p = _state_config_path()
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    for attr in _TOGGLE_ATTRS:
        v = data.get(attr)
        if isinstance(v, bool):
            setattr(state, attr, v)


def _apply_preset(state: LauncherState, preset_updates: dict) -> None:
    """Apply a preset's flag changes to `state` and persist."""
    for attr, val in preset_updates.items():
        if attr in _TOGGLE_ATTRS:
            setattr(state, attr, bool(val))
    _save_state(state)


def _collect_active_flags(state: LauncherState) -> list[str]:
    """Single source of truth for what flags are currently 'on'."""
    on: list[str] = []
    if state.exact:
        on.append("exact (fast, no enrichment)")
    if state.no_cache: on.append("no-cache")
    if state.no_identity: on.append("no-identity")
    if state.found_only: on.append("found-only")
    if state.analyze: on.append("analyze")
    if state.graph_path: on.append(f"graph→{state.graph_path}")
    if state.analyze_out: on.append(f"analyst-out→{state.analyze_out}")
    if state.export_path: on.append(f"export→{state.export_path}")
    return on


def _flag_status(state: LauncherState) -> str:
    """Compact summary of what's currently on."""
    on = _collect_active_flags(state)
    return ", ".join(on) if on else c("(defaults)", DIM)


def _flag_status_markup(state: LauncherState) -> str:
    """Same as _flag_status but uses Rich markup so it can sit inside a
    Rich Panel without throwing the panel's width calculation off."""
    on = _collect_active_flags(state)
    return ", ".join(on) if on else "[dim](defaults)[/dim]"


def draw_home(state: LauncherState) -> None:
    """Render the main launcher screen as one atomic write so the
    terminal can't show intermediate cursor positions mid-repaint.

    The bracketed-sync escapes around the write tell modern terminals
    (Konsole, kitty, iTerm2, Alacritty, Foot, WezTerm) to buffer the
    entire frame and apply it in a single flip — fixing the stray
    I-beam-cursor `|` artifacts visible in earlier builds.
    """
    # Rich markup, not raw ANSI - Rich's Panel measures markup widths
    # correctly but mis-measures embedded escape codes, which used to
    # make the right border float at a different column on each row.
    handle_cell = state.handle or "[dim](type a handle below — lowercase letters enter text, Shift+letter triggers menus)[/dim]"
    export_cell = state.export_path or "[dim](none)[/dim]"
    status_lines = [
        f"  [bold]Handle[/bold]   {handle_cell}",
        f"  [bold]Flags [/bold]   {_flag_status_markup(state)}",
        f"  [bold]Export[/bold]   {export_cell}",
    ]
    section = render_section_rich("Target", status_lines)
    if not section:
        section = (
            hr(min(78, term_width())) + "\n"
            + "\n".join(status_lines) + "\n"
            + hr(min(78, term_width())) + "\n"
        )

    # Menus fire on Shift+letter (uppercase) so they don't collide
    # with typing handles like "alice" that start with the same
    # letter. Visible hints use the capital so users can read them
    # as Shift+H, Shift+F, etc.
    # Menus fire on Shift+letter (uppercase) so they don't collide
    # with typing handles like "alice" that start with the same
    # letter. Visible hints use the capital so users can read them
    # as Shift+H, Shift+F, etc.
    keys = "   ".join([
        key_hint("Enter", "scan"),
        key_hint("⇧P", "presets"),
        key_hint("⇧F", "flags"),
        key_hint("⇧E", "export"),
        key_hint("⇧G", "graph"),
        key_hint("⇧A", "analyst"),
        key_hint("⇧S", "self-check"),
        key_hint("⇧T", "TUI"),
        key_hint("⇧H", "help"),
        key_hint("⇧Q", "quit"),
    ])

    # Build the frame so that:
    #   1. The entire visible screen is first painted with a solid
    #      dark bg (so terminal transparency can't show through).
    #   2. The content is then written on top - but with `BG_DARK`
    #      re-set before every section so Rich Panel's internal
    #      `\033[0m` resets don't punch transparent holes between
    #      the panel and the surrounding text.
    frame = (
        SYNC_BEGIN
        + CLEAR
        + HIDE_CURSOR
        + _solid_screen_fill()
        + BG_DARK + render_banner() + BG_DARK + "\n\n"
        + BG_DARK + section + BG_DARK
        + "\n"
        + BG_DARK + "  " + keys + BG_DARK + "\n\n"
        + SYNC_END
    )
    sys.stdout.write(frame)
    sys.stdout.flush()


def screen_help() -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.write(banner(color=True))
    sys.stdout.write("\n\n  " + c("Help", BOLD) + "\n")
    sys.stdout.write(hr(min(78, term_width())) + "\n\n")
    lines = [
        ("Target",       "Type a username (`alice`) or paste a profile URL"),
        ("",             "(`https://github.com/user`). Then press Enter."),
        ("",             ""),
        ("[Enter]",      "Run the scan with the current flags."),
        ("[Shift+P]",    "Apply a flag preset (Quick / Thorough / Full / Reset)."),
        ("[Shift+F]",    "Toggle individual scan flags."),
        ("[Shift+E]",    "Set report export — pick format (html/pdf/json/md/csv/mmd), then name."),
        ("[Shift+G]",    "Set graph emit — pick format (html/json/gexf), then name."),
        ("[Shift+A]",    "Set analyst JSON output — name the file."),
        ("[Shift+S]",    "Run --self-check: probe canary handles, report drift."),
        ("[Shift+T]",    "Launch the full Textual TUI."),
        ("[Shift+H]",    "This help.  (`?` also works.)"),
        ("[Shift+Q]",    "Quit."),
        ("",             ""),
        ("Why Shift?",   "Lowercase letters are reserved for typing handles. So"),
        ("",             "`alice` types the handle; `Shift+H` opens this help."),
        ("",             ""),
        ("Other keys",   "ESC anywhere closes the current sub-screen. Ctrl-C exits."),
        ("",             ""),
        ("Flags persist", "Toggled flags are saved to ~/.config/phantom/launcher.json"),
        ("",             "and reloaded next time you launch phantom. Use [Shift+P]"),
        ("",             "→ Reset to clear them."),
        ("",             ""),
        ("Phase 4",      "--analyze requires an LLM endpoint configured via:"),
        ("",             "  phantom --api add llm_endpoint URL"),
        ("",             "  phantom --api add llm_model NAME"),
        ("",             "  phantom --api add llm_api_key KEY    (if remote)"),
    ]
    for k, v in lines:
        sys.stdout.write(f"  {c(k, ACCENT, BOLD):<14}  {v}\n")
    sys.stdout.write("\n  " + key_hint("any key", "back") + "\n")
    sys.stdout.flush()
    _read_key()


def screen_flags(state: LauncherState) -> None:
    """Cycle the toggleable flags via single-character shortcuts.

    Organised by phase so the user can see which subsystem each flag
    belongs to. Digits 1-8 are the legacy / discovery flags; letters
    a-d are the Phase 4/5 additions (Analyst + OPSEC).
    """
    # (key, attr, label, group_header)
    # Only the meaningful choices: speed, data integrity, output filter,
    # LLM analyst. Enrichment toggles are auto-on for non-exact scans.
    toggles = [
        ("1", "exact",       "Exact handle, single variant (fast, no enrichment, ~15s)",  "Scan speed"),
        ("2", "found_only",  "Hide [?] / [MISSING] from terminal (cosmetic)",             ""),
        ("3", "no_cache",    "Skip the 1h response cache (force-fresh)",                  "Data integrity"),
        ("4", "no_identity", "Skip photo / cluster correlation (faster post-scan)",       ""),
        ("5", "analyze",     "Run the LLM analyst (Claude/Groq dossier — costs tokens)",  "LLM"),
    ]
    # Build a key→(attr, label) lookup once.
    toggle_map = {k: (attr, label) for k, attr, label, _ in toggles}

    while True:
        sys.stdout.write(CLEAR)
        sys.stdout.write(banner(color=True))
        sys.stdout.write("\n\n  " + c("Scan flags", BOLD) + "\n")
        sys.stdout.write(hr(min(78, term_width())) + "\n\n")
        last_group = None
        for k, attr, label, group in toggles:
            if group and group != last_group:
                # Group separator
                if last_group is not None:
                    sys.stdout.write("\n")
                sys.stdout.write(f"  {c(group, DIM)}\n")
                last_group = group
            on = getattr(state, attr)
            mark = c("●", ACCENT) if on else c("○", MUTED)
            sys.stdout.write(f"  [{c(k, BOLD)}]  {mark}  {label}\n")

        # Path-flag rows (read-only summary here; user sets these from
        # the home screen via Shift+G / Shift+A / Shift+E pickers).
        sys.stdout.write("\n  " + c("Output paths (set from home screen)", DIM) + "\n")
        graph_val = state.graph_path or c("(unset — Shift+G on home screen)", DIM)
        sys.stdout.write(f"  {c('--graph', BOLD):<22}  {graph_val}\n")
        ao_val = state.analyze_out or c("(unset — Shift+A on home screen)", DIM)
        sys.stdout.write(f"  {c('--analyze-out', BOLD):<22}  {ao_val}\n")
        ex_val = state.export_path or c("(unset — Shift+E on home screen)", DIM)
        sys.stdout.write(f"  {c('--export', BOLD):<22}  {ex_val}\n")

        sys.stdout.write("\n  " + key_hint("char", "toggle") + "   " +
                         key_hint("ESC", "back") + "\n")
        sys.stdout.flush()
        k = _read_key()
        if k in ("ESC", "ENTER", "q", "Q"):
            return
        # Single-key toggle - accept both upper and lower case for letters.
        if k and k.lower() in toggle_map:
            attr, _ = toggle_map[k.lower()]
            setattr(state, attr, not getattr(state, attr))
            _save_state(state)


def screen_presets(state: LauncherState) -> None:
    """One-keystroke flag bundles. Lets the user load Quick / Thorough /
    Full / Reset without manually toggling 11 flags."""
    while True:
        sys.stdout.write(CLEAR)
        sys.stdout.write(banner(color=True))
        sys.stdout.write("\n\n  " + c("Flag presets", BOLD) + "\n")
        sys.stdout.write(hr(min(78, term_width())) + "\n\n")
        sys.stdout.write(
            "  " + c("Pick a preset", DIM)
            + " — applies a bundle of flags instantly.\n"
            + "  " + c("(Reset returns to defaults so you can start fresh.)", DIM)
            + "\n\n"
        )
        for key, name, desc, _updates in _PRESETS:
            sys.stdout.write(
                f"  [{c(key, BOLD)}]  {c(name, ACCENT, BOLD)}\n"
                f"        {c(desc, DIM)}\n\n"
            )
        sys.stdout.write(
            "  " + key_hint("digit", "apply preset") + "   "
            + key_hint("ESC", "back") + "\n"
        )
        sys.stdout.flush()
        k = _read_key()
        if k in ("ESC", "ENTER", "q", "Q"):
            return
        for digit, name, _desc, updates in _PRESETS:
            if k == digit:
                _apply_preset(state, updates)
                # Flash a confirmation so the user knows it landed.
                sys.stdout.write("\n  " + c(f"✓ Applied: {name}", ACCENT, BOLD) + "\n")
                sys.stdout.flush()
                # Brief pause via a key wait - they'll press ESC or another digit
                # next anyway.
                k2 = _read_key()
                if k2 in ("ESC", "ENTER", "q", "Q"):
                    return
                # If they pressed another digit, apply that one too.
                for d2, _n2, _d2, u2 in _PRESETS:
                    if k2 == d2:
                        _apply_preset(state, u2)
                        break
                break


def _path_picker(
    state: LauncherState,
    *,
    title: str,
    blurb: str,
    formats: list[tuple[str, str, str]],
    default_basename: str,
    current_value: Optional[str],
) -> Optional[str]:
    """Two-step picker: choose format → enter a name → return absolute path.

    `formats` is a list of (key, label, extension) where the extension
    INCLUDES the dot (e.g. '.html'). The user picks a digit; we tack
    that extension onto whatever name they type next (defaulting to
    `default_basename` if they just hit Enter).

    `current_value` is shown so the user knows what's already set.
    Returns None when the user ESCs out (caller leaves state unchanged),
    or empty string to mean "clear it".
    """
    while True:
        sys.stdout.write(CLEAR)
        sys.stdout.write(banner(color=True))
        sys.stdout.write("\n\n  " + c(title, BOLD) + "\n")
        sys.stdout.write(hr(min(78, term_width())) + "\n\n")
        sys.stdout.write(f"  {c(blurb, DIM)}\n\n")
        if current_value:
            sys.stdout.write(f"  current: {c(current_value, ACCENT)}\n\n")
        for key, label, ext in formats:
            sys.stdout.write(f"  [{c(key, BOLD)}]  {label} {c('(' + ext + ')', DIM)}\n")
        sys.stdout.write(f"\n  [{c('0', BOLD)}]  clear (unset this output)\n")
        sys.stdout.write(
            "\n  " + key_hint("digit", "choose format")
            + "   " + key_hint("ESC", "cancel") + "\n"
        )
        sys.stdout.flush()
        k = _read_key()
        if k in ("ESC", "q", "Q"):
            return None
        if k == "0":
            return ""   # explicit clear
        chosen = next((f for f in formats if f[0] == k), None)
        if chosen is None:
            continue
        _key, label, ext = chosen

        # Step 2: ask for a filename. They can:
        #   - press Enter immediately to accept the default name in $HOME
        #   - type a bare name → goes to $HOME/<name><ext>
        #   - type a full path → used as-is (ext added if missing)
        sys.stdout.write(CLEAR)
        sys.stdout.write(banner(color=True))
        sys.stdout.write("\n\n  " + c(title + " — " + label, BOLD) + "\n")
        sys.stdout.write(hr(min(78, term_width())) + "\n\n")
        default = f"~/{default_basename}{ext}"
        sys.stdout.write(
            f"  Enter filename or full path. Press Enter for default:\n"
            f"  default → {c(default, ACCENT)}\n\n"
        )
        prompt = "  " + c("name>", BOLD) + " "
        result = _line_edit(prompt, initial="")
        if result is None:
            return None
        name = result.strip()
        if not name:
            name = default
        # If they just typed "alice", treat as ~/alice<ext>.
        # If they typed a path with /, use it.
        # If extension missing, add ours.
        if "/" not in name and not name.startswith("~"):
            name = f"~/{name}"
        if not any(name.lower().endswith(e) for _k, _l, e in formats):
            name = name + ext
        return os.path.expanduser(name)


# Available export formats - user picks one. Extension determines
# which exporter in `exporters/__init__.py` runs.
_EXPORT_FORMATS = [
    ("1", "HTML report (interactive)", ".html"),
    ("2", "PDF report (Playwright)",   ".pdf"),
    ("3", "JSON (raw data)",           ".json"),
    ("4", "Markdown",                  ".md"),
    ("5", "CSV",                       ".csv"),
    ("6", "Mermaid diagram",           ".mmd"),
]

# Graph emit formats.
_GRAPH_FORMATS = [
    ("1", "HTML viewer (cytoscape.js)", ".html"),
    ("2", "JSON (raw graph)",           ".json"),
    ("3", "GEXF (open in Gephi)",       ".gexf"),
]


def screen_export(state: LauncherState) -> None:
    """Pick a format, then name the file. No typing `.html` by hand."""
    default = state.handle or "report"
    result = _path_picker(
        state,
        title="Export report",
        blurb="Pick a format, then name the file (or accept the default).",
        formats=_EXPORT_FORMATS,
        default_basename=default,
        current_value=state.export_path,
    )
    if result is None:
        return   # ESC = no change
    state.export_path = result or None


def screen_graph(state: LauncherState) -> None:
    """Pick a graph format, then name the file."""
    default = (state.handle or "graph") + "_graph"
    result = _path_picker(
        state,
        title="Graph output",
        blurb="Emit the typed investigation graph after the scan.",
        formats=_GRAPH_FORMATS,
        default_basename=default,
        current_value=state.graph_path,
    )
    if result is None:
        return
    state.graph_path = result or None


def screen_analyze_out(state: LauncherState) -> None:
    """Pick a name for the analyst JSON output (only JSON makes sense here)."""
    default = (state.handle or "analyst") + "_analysis"
    result = _path_picker(
        state,
        title="Analyst JSON output",
        blurb=(
            "Save the LLM analyst's dossier + contradictions + pivots "
            "+ adversarial as JSON."
        ),
        formats=[("1", "JSON (the only format)", ".json")],
        default_basename=default,
        current_value=state.analyze_out,
    )
    if result is None:
        return
    state.analyze_out = result or None


# ---------------------------------------------------------------------------
# Action runners
# ---------------------------------------------------------------------------
def _run_scan(state: LauncherState) -> None:
    """Invoke cli.main with the launcher's composed argv. The scan
    streams output to stdout exactly as if the user had run it
    directly, so the existing terminal renderer just works."""
    if not state.handle:
        sys.stdout.write("\n  " + c("Enter a handle first.", ERR) + "\n\n")
        sys.stdout.write("  " + key_hint("any key", "back") + "\n")
        sys.stdout.flush()
        _read_key()
        return
    sys.stdout.write(SHOW_CURSOR)
    sys.stdout.write("\n")
    sys.stdout.flush()
    from cli import main as cli_main
    try:
        cli_main(state.argv())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        sys.stdout.write("\n  " + c("Scan interrupted.", ERR) + "\n")
    sys.stdout.write("\n  " + key_hint("any key", "back") + "\n")
    sys.stdout.flush()
    _read_key()


def _run_self_check() -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.write(banner(color=True))
    sys.stdout.write("\n\n  " + c("Self-check", BOLD) + " — probing canaries...\n\n")
    sys.stdout.flush()
    from cli import main as cli_main
    try:
        cli_main(["--self-check"])
    except SystemExit:
        pass
    except KeyboardInterrupt:
        sys.stdout.write("\n  " + c("Self-check interrupted.", ERR) + "\n")
    sys.stdout.write("\n  " + key_hint("any key", "back") + "\n")
    sys.stdout.flush()
    _read_key()


def _launch_textual(state: LauncherState) -> None:
    """Open the Textual full TUI (Option B). Falls back to a clean
    error message when Textual isn't installed."""
    try:
        from tui_app import run_tui
    except ImportError as e:
        sys.stdout.write(
            "\n  " + c("Textual TUI is not installed.", ERR) + "\n"
            "  Install with: " + c("pip install textual", ACCENT) + "\n"
            "  (" + str(e) + ")\n\n  "
            + key_hint("any key", "back") + "\n"
        )
        sys.stdout.flush()
        _read_key()
        return
    sys.stdout.write(SHOW_CURSOR)
    sys.stdout.write(CLEAR)
    sys.stdout.flush()
    run_tui(initial_handle=state.handle)
    # When the Textual app exits, are back here.

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main_loop() -> int:
    state = LauncherState()
    # Load persisted toggles so the user's last flag setup survives across
    # launcher runs. Paths (handle, export, graph, analyst-out) are NOT
    # persisted - they're per-target.
    _load_state_into(state)
    # Enter the alternate screen so redraws never pollute scrollback.
    # On exit (finally block) restore the user's original terminal
    # state - they'll see whatever was there before they ran `phantom`.
    sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR)
    try:
        while True:
            # draw_home is wrapped in sync-output and ends with the
            # cursor hidden. Now build the prompt line and re-enable
            # the cursor right at its final position so the terminal
            # never paints an intermediate I-beam elsewhere.
            draw_home(state)
            prompt = "  " + c("phantom>", BOLD, ACCENT) + " "
            sys.stdout.write(prompt + state.handle + SHOW_CURSOR)
            sys.stdout.flush()
            # Read the first key. Letters that aren't shortcuts feed
            # into a normal line edit.
            try:
                k = _read_key()
            except KeyboardInterrupt:
                sys.stdout.write("\n")
                return 0
            if k == "ENTER":
                _run_scan(state)
                continue
            # Menu shortcuts: UPPERCASE letters only (Shift+letter) so
            # they don't conflict with typing a handle that starts with
            # the same letter. `?` is safe to leave case-insensitive
            # since handles can't start with it.
            if k == "Q":
                sys.stdout.write("\n")
                return 0
            if k == "H" or k == "?":
                screen_help()
                continue
            if k == "F":
                screen_flags(state)
                continue
            if k == "E":
                screen_export(state)
                continue
            if k == "G":
                screen_graph(state)
                continue
            if k == "A":
                screen_analyze_out(state)
                continue
            if k == "P":
                screen_presets(state)
                continue
            if k == "S":
                _run_self_check()
                continue
            if k == "T":
                _launch_textual(state)
                continue
            if k == "BACKSPACE":
                state.handle = state.handle[:-1]
                continue
            if k in ("ESC", "EOF"):
                continue
            if len(k) == 1 and k.isprintable():
                # ANY other printable character (letters, digits,
                # symbols, lowercase shortcuts) starts a fresh handle
                # entry and hands off to the line editor. This is what
                # makes typing a lowercase-letter handle actually work even though that letter
                # is also bound to a menu - at the home screen we
                # consider lowercase letters to be text input and
                # uppercase letters to be menu commands.
                state.handle = ""
                sys.stdout.write("\r" + ERASE_LINE + prompt)
                sys.stdout.flush()
                line = _line_edit("", initial=k)
                if line is not None:
                    state.handle = line
    finally:
        # Restore terminal state. Order matters: show cursor + reset
        # styling, then drop the alternate screen so the user lands
        # back on their original shell prompt.
        sys.stdout.write(SHOW_CURSOR + RESET + ALT_SCREEN_OFF)
        sys.stdout.flush()


def is_interactive_session() -> bool:
    """Decide whether to open the launcher when invoked with no args.

    Opens only when both stdin and stdout are a real TTY. Piped /
    redirected runs keep the existing behaviour (the parser prints its
    usage error)."""
    return sys.stdin.isatty() and sys.stdout.isatty()
