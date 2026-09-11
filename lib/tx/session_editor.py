"""Two-field terminal form for the focused session's name and tags."""

from __future__ import annotations

import curses
import unicodedata

from .palette import SELECTION_BG


def _width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(character) else
        2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        for character in text
    )


def edit_session(name: str, tags: str) -> tuple[str, str] | None:
    return curses.wrapper(_form, [name, tags])


def _form(screen, values: list[str]) -> tuple[str, str] | None:
    curses.raw()  # Read Ctrl-C as cancellation, including before either field is saved.
    curses.set_escdelay(25)
    screen.keypad(True)
    accent, muted, selected, warning = curses.A_BOLD, curses.A_DIM, curses.A_REVERSE, curses.A_BOLD
    if curses.has_colors():
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLUE, -1)
        curses.init_pair(2, curses.COLOR_WHITE, -1)
        curses.init_pair(3, curses.COLOR_WHITE, int(SELECTION_BG) if curses.COLORS >= 256 else curses.COLOR_BLACK)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        accent = curses.color_pair(1) | curses.A_BOLD
        muted = curses.color_pair(2) | curses.A_DIM
        selected = curses.color_pair(3)
        warning = curses.color_pair(4)
    positions = [len(value) for value in values]
    offsets = [0, 0]
    active = 0
    error = ""
    while True:
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 7 or width < 24:
            screen.addnstr(0, 0, "Resize to edit", max(0, width - 1))
        else:
            available = width - 14
            for index, label in enumerate(("Name", "Tags")):
                row = index * 2 + 1
                offsets[index] = min(offsets[index], positions[index])
                while _width(values[index][offsets[index]:positions[index]]) >= available:
                    offsets[index] += 1
                visible = ""
                for character in values[index][offsets[index]:]:
                    if _width(visible + character) > available:
                        break
                    visible += character
                screen.addstr(row, 2, f"{'›' if index == active else ' '} {label}:",
                              accent if index == active else muted)
                field_style = selected if index == active else curses.A_NORMAL
                screen.addstr(row, 11, " " * (available + 2), field_style)
                screen.addstr(row, 12, visible, field_style)
            screen.addnstr(4, 12, "Comma-separated · empty clears tags", width - 14, muted)
            screen.addnstr(6, 2, error or "↑↓ switch   Enter save   Esc cancel", width - 4,
                           warning if error else muted)
            screen.move(active * 2 + 1, 12 + _width(values[active][offsets[active]:positions[active]]))
        screen.refresh()
        key = screen.get_wch()
        if key in ("\x1b", "\x03"):
            return None
        if height < 7 or width < 24:
            continue
        if key in ("\n", "\r", curses.KEY_ENTER):
            if not values[0].strip():
                error = "Name cannot be empty."
                active = 0
                continue
            return values[0], values[1]
        error = ""
        if key in (curses.KEY_UP, curses.KEY_DOWN, "\t", curses.KEY_BTAB):
            active = 1 - active
            continue
        position = positions[active]
        value = values[active]
        if key in (curses.KEY_LEFT, "\x02"):
            positions[active] = max(0, position - 1)
        elif key in (curses.KEY_RIGHT, "\x06"):
            positions[active] = min(len(value), position + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            positions[active] = 0
        elif key in (curses.KEY_END, "\x05"):
            positions[active] = len(value)
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08") and position:
            values[active] = value[:position - 1] + value[position:]
            positions[active] -= 1
        elif key == curses.KEY_DC:
            values[active] = value[:position] + value[position + 1:]
        elif key == "\x15":
            values[active] = value[position:]
            positions[active] = 0
        elif key == "\x0b":
            values[active] = value[:position]
        elif isinstance(key, str) and key.isprintable():
            values[active] = value[:position] + key + value[position:]
            positions[active] += len(key)
