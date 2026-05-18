#!/usr/bin/env python3
"""Generate setup/tokyonight-night.itermcolors from the canonical tokyonight-night
hex palette. One-shot generator — re-run if the palette changes.

Run:  python3 setup/_gen-itermcolors.py > setup/tokyonight-night.itermcolors
"""

# Canonical tokyonight-night palette (https://github.com/folke/tokyonight.nvim).
COLORS = {
    "Ansi 0 Color":   "#15161e",  # black
    "Ansi 1 Color":   "#f7768e",  # red
    "Ansi 2 Color":   "#9ece6a",  # green
    "Ansi 3 Color":   "#e0af68",  # yellow
    "Ansi 4 Color":   "#7aa2f7",  # blue
    "Ansi 5 Color":   "#bb9af7",  # magenta
    "Ansi 6 Color":   "#7dcfff",  # cyan
    "Ansi 7 Color":   "#a9b1d6",  # white
    "Ansi 8 Color":   "#414868",  # bright black
    "Ansi 9 Color":   "#f7768e",  # bright red
    "Ansi 10 Color":  "#9ece6a",  # bright green
    "Ansi 11 Color":  "#e0af68",  # bright yellow
    "Ansi 12 Color":  "#7aa2f7",  # bright blue
    "Ansi 13 Color":  "#bb9af7",  # bright magenta
    "Ansi 14 Color":  "#7dcfff",  # bright cyan
    "Ansi 15 Color":  "#c0caf5",  # bright white
    "Background Color": "#1a1b26",
    "Foreground Color": "#c0caf5",
    "Bold Color":       "#c0caf5",
    "Cursor Color":     "#c0caf5",
    "Cursor Text Color":"#1a1b26",
    "Selection Color":  "#283457",
    "Selected Text Color":"#c0caf5",
    "Link Color":       "#7aa2f7",
    "Badge Color":      "#f7768e",
}


def to_components(hex_string):
    hex_value = hex_string.lstrip("#")
    red = int(hex_value[0:2], 16) / 255
    green = int(hex_value[2:4], 16) / 255
    blue = int(hex_value[4:6], 16) / 255
    return red, green, blue


def emit():
    print('<?xml version="1.0" encoding="UTF-8"?>')
    print('<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">')
    print('<plist version="1.0">')
    print('<dict>')
    for name in sorted(COLORS):
        red, green, blue = to_components(COLORS[name])
        print(f'\t<key>{name}</key>')
        print('\t<dict>')
        print('\t\t<key>Alpha Component</key>')
        print('\t\t<real>1</real>')
        print('\t\t<key>Blue Component</key>')
        print(f'\t\t<real>{blue:.16f}</real>')
        print('\t\t<key>Color Space</key>')
        print('\t\t<string>sRGB</string>')
        print('\t\t<key>Green Component</key>')
        print(f'\t\t<real>{green:.16f}</real>')
        print('\t\t<key>Red Component</key>')
        print(f'\t\t<real>{red:.16f}</real>')
        print('\t</dict>')
    print('</dict>')
    print('</plist>')


if __name__ == "__main__":
    emit()
