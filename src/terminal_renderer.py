"""
Render text output as a terminal-style screenshot image using Pillow.
"""
import os
import logging
from PIL import Image, ImageDraw, ImageFont
from typing import List, Tuple, Optional

from config_loader import AppConfig, TerminalStyle

logger = logging.getLogger(__name__)

# ANSI escape code regex for stripping
import re
import ipaddress
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[@-~]")

HIGHLIGHT_TOKENS = re.compile(
    r'\b(?:DISABLED|ENABLED)\b|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')


def terminal_text_runs(line, default_color):
    """Preserve every character; color only exact states and valid IPv4s."""
    runs, start = [], 0
    for match in HIGHLIGHT_TOKENS.finditer(line):
        token = match.group()
        color = {'DISABLED': (255, 235, 59), 'ENABLED': (80, 250, 123)}.get(token)
        if color is None:
            try:
                ipaddress.IPv4Address(token)
                color = (230, 100, 255)
            except ValueError:
                continue
        if match.start() > start:
            runs.append((line[start:match.start()], default_color))
        runs.append((token, color))
        start = match.end()
    if start < len(line):
        runs.append((line[start:], default_color))
    return runs


def ansi_text_runs(line, default_color):
    """Use explicit summary colors, with normal token highlighting elsewhere."""
    colors = {31: (255, 90, 90), 32: (80, 250, 123),
              33: (255, 235, 59), 36: (100, 220, 255)}
    current = default_color
    start = 0
    runs = []
    for match in re.finditer(r'\x1b\[([0-9;]*)m', line):
        fragment = strip_ansi(line[start:match.start()])
        runs.extend(terminal_text_runs(fragment, current) if current == default_color
                    else [(fragment, current)])
        for code in match[1].split(';'):
            number = int(code or 0)
            if number in (0, 39):
                current = default_color
            elif number in colors:
                current = colors[number]
        start = match.end()
    fragment = strip_ansi(line[start:])
    runs.extend(terminal_text_runs(fragment, current) if current == default_color
                else [(fragment, current)])
    return runs


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from text."""
    return ANSI_ESCAPE.sub("", text)


def _get_font(font_name: str, font_size: int) -> ImageFont.FreeTypeFont:
    """Try to load a monospace font, fall back to default."""
    # Common monospace font paths on Windows
    font_paths = [
        f"C:/Windows/Fonts/{font_name}.ttf",
        f"C:/Windows/Fonts/{font_name.lower()}.ttf",
        f"C:/Windows/Fonts/consola.ttf",
        f"C:/Windows/Fonts/cour.ttf",
        f"C:/Windows/Fonts/lucon.ttf",
    ]

    for path in font_paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, font_size)
            except Exception:
                continue

    # Try by font name directly (Pillow may resolve it)
    try:
        return ImageFont.truetype(font_name, font_size)
    except Exception:
        pass

    logger.warning(f"Could not load font '{font_name}', using default")
    return ImageFont.load_default()


def render_terminal_screenshot(
    command: str,
    output: str,
    style: TerminalStyle,
    save_path: str,
    title: Optional[str] = None,
    max_width: int = 1400,
    prompts: Optional[set] = None,
    preserve_content: bool = False,
) -> str:
    """
    Render command output as a terminal-style screenshot image.

    Args:
        command: The command that was executed (shown as header)
        output: The text output to render
        style: Terminal style configuration
        save_path: Path to save the PNG file
        title: Optional title bar text
        max_width: Maximum image width in pixels

    Returns:
        Path to the saved image file
    """
    # Clean up output
    output = output.replace("\r\n", "\n").replace("\r", "\n")

    # Remove trailing empty lines
    lines = output.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()

    # Cap very long outputs to prevent oversized images
    MAX_LINES = 3000
    if not preserve_content and len(lines) > MAX_LINES:
        truncated_count = len(lines) - MAX_LINES
        lines = lines[:MAX_LINES]
        lines.append(f"... ({truncated_count} more lines truncated) ...")

    # Load font
    font = _get_font(style.font, style.font_size)
    bold_font = font  # Use same font for header (bold not always available)

    # Calculate character dimensions
    char_bbox = font.getbbox("M")
    char_width = char_bbox[2] - char_bbox[0]
    char_height = char_bbox[3] - char_bbox[1]
    line_height = char_height + style.line_spacing

    # Build display lines: just the raw output (includes prompt from AMOS)
    display_lines = []
    line_styles = []  # Track which lines get special styling

    if title:
        display_lines.append(f"  {title}")
        line_styles.append("title")

    # Output lines as-is (prompt line is already included from AMOS output)
    for line in lines:
        # Highlight prompt lines with header color
        if prompts and line.strip() in prompts:
            line_styles.append("header")   # explicit prompt lines (evidence)
        elif ">" in line and command.split()[0] in line:
            line_styles.append("header")
        elif line.strip().endswith(">"):
            line_styles.append("header")
        else:
            line_styles.append("normal")
        display_lines.append(line)

    # Calculate image dimensions
    max_line_len = max(len(line) for line in display_lines) if display_lines else 40
    natural_width = max(600, style.padding * 2 +
                        int(max((font.getlength(strip_ansi(line).expandtabs(8)) for line in lines), default=0)) + 10)
    if title:
        natural_width = max(natural_width, 90 + int(font.getlength('  ' + title)))
    img_width = natural_width if preserve_content else min(max_width, natural_width)
    img_height = style.padding * 2 + len(display_lines) * line_height + 10

    # Title bar height
    title_bar_height = 0
    if title:
        title_bar_height = line_height + 8

    # Create image
    bg = tuple(style.bg_color)
    img = Image.new("RGB", (img_width, img_height + title_bar_height), bg)
    draw = ImageDraw.Draw(img)

    # Draw title bar background (slightly lighter)
    if title:
        title_bg = tuple(min(c + 30, 255) for c in style.bg_color)
        draw.rectangle([0, 0, img_width, title_bar_height], fill=title_bg)

        # Draw window control dots
        dot_y = title_bar_height // 2
        for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            draw.ellipse([12 + i * 22, dot_y - 6, 24 + i * 22, dot_y + 6], fill=color)

    # Draw text lines
    y_offset = title_bar_height + style.padding
    text_color = tuple(style.text_color)
    header_color = tuple(style.header_color)

    def draw_colored(x, y, text, base_color):
        for fragment, color in ansi_text_runs(text.expandtabs(8), base_color):
            draw.text((x, y), fragment, fill=color, font=font)
            x += font.getlength(fragment)

    for i, (line, line_style) in enumerate(zip(display_lines, line_styles)):
        y = y_offset + i * line_height

        if line_style == "title":
            # Title text (centered-ish, in title bar)
            title_color = tuple(min(c + 80, 255) for c in style.text_color)
            draw.text((80, title_bar_height // 2 - char_height // 2), line, fill=title_color, font=font)
        elif line_style == "header":
            # Prompt line: node name in green, "> command" in white
            # e.g. "MIN3117_P3ACANOCOTAGUMDDNB01> st cellfdd=.*Y-"
            prompt_end = line.find(">")
            if prompt_end != -1:
                node_part = line[:prompt_end]  # node name (green)
                rest_part = line[prompt_end:]   # "> command" (white)
                draw.text((style.padding, y), node_part, fill=header_color, font=bold_font)
                # Calculate x offset for the rest
                node_bbox = font.getbbox(node_part)
                x_offset = style.padding + (node_bbox[2] - node_bbox[0])
                draw_colored(x_offset, y, rest_part, text_color)
            else:
                draw_colored(style.padding, y, line, header_color)
        else:
            draw_colored(style.padding, y, line, text_color)

    # Save
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    img.save(save_path, "PNG")
    logger.info(f"Screenshot saved: {save_path}")

    return save_path


def render_multi_command_screenshot(
    commands_outputs: List[Tuple[str, str]],
    style: TerminalStyle,
    save_path: str,
    title: Optional[str] = None,
    max_width: int = 1400,
) -> str:
    """
    Render multiple commands and their outputs into a single terminal screenshot.

    Args:
        commands_outputs: List of (command, output) tuples
        style: Terminal style configuration
        save_path: Path to save the PNG file
        title: Optional title bar text
        max_width: Maximum image width

    Returns:
        Path to the saved image file
    """
    # Combine all commands and outputs
    combined_parts = []
    for cmd, out in commands_outputs:
        combined_parts.append(f"$ {cmd}")
        combined_parts.append(strip_ansi(out).strip())
        combined_parts.append("")  # Blank line between commands

    combined_output = "\n".join(combined_parts)

    # Use the first command as the main command shown
    main_cmd = " && ".join(cmd for cmd, _ in commands_outputs)

    return render_terminal_screenshot(
        command=main_cmd,
        output=combined_output,
        style=style,
        save_path=save_path,
        title=title,
        max_width=max_width,
    )
