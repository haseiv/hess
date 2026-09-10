"""
Miami DM Welcome Card Generator
"""

import os
import io
import asyncio
import logging
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("bot.welcome")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_BANNER_PATH = os.path.join(_BASE_DIR, "assets", "welcome_banner.jpg")
_FONT_BOLD = os.path.join(_BASE_DIR, "assets", "fonts", "bold.ttf")
_FONT_REG = os.path.join(_BASE_DIR, "assets", "fonts", "regular.ttf")

# Coordinates of glowing circle center on the 1376x768 banner
_CENTER_X = 698
_CENTER_Y = 387
_AVATAR_SIZE = 340


def _get_fonts():
    """Load fonts with safe fallbacks across platforms."""
    font_candidates_bold = [
        _FONT_BOLD,
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    font_candidates_reg = [
        _FONT_REG,
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]

    font_title = None
    font_name = None
    font_sub = None

    for p in font_candidates_bold:
        if os.path.exists(p):
            try:
                font_title = ImageFont.truetype(p, 44)
                font_name = ImageFont.truetype(p, 32)
                break
            except Exception:
                continue

    for p in font_candidates_reg:
        if os.path.exists(p):
            try:
                font_sub = ImageFont.truetype(p, 24)
                break
            except Exception:
                continue

    if font_title is None:
        font_title = ImageFont.load_default()
    if font_name is None:
        font_name = ImageFont.load_default()
    if font_sub is None:
        font_sub = ImageFont.load_default()

    return font_title, font_name, font_sub


def _draw_glow_text(draw, text, font, pos_x, pos_y, fill, glow_color, glow_radius=3):
    """Render centered text with neon glow aura."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    tx = pos_x - tw // 2

    # Glowing aura
    for dx in range(-glow_radius, glow_radius + 1):
        for dy in range(-glow_radius, glow_radius + 1):
            if dx * dx + dy * dy <= glow_radius * glow_radius and (dx != 0 or dy != 0):
                draw.text((tx + dx, pos_y + dy), text, font=font, fill=glow_color)

    # Foreground text
    draw.text((tx, pos_y), text, font=font, fill=fill)


def generate_welcome_card(
    avatar_bytes: bytes | None,
    member_name: str,
    server_name: str = "MIAMI DM",
    member_count: int = 1,
) -> io.BytesIO:
    """
    Generate welcome banner image with user avatar in center.
    Returns io.BytesIO with JPEG image data.
    """
    if os.path.exists(_BANNER_PATH):
        template = Image.open(_BANNER_PATH).convert("RGBA")
    else:
        template = Image.new("RGBA", (1376, 768), (10, 15, 30, 255))
        d_bg = ImageDraw.Draw(template)
        d_bg.text((_CENTER_X - 100, 80), "MIAMI DM", fill=(0, 220, 255, 255))

    # Process avatar
    if avatar_bytes:
        try:
            avatar_raw = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
        except Exception:
            avatar_raw = None
    else:
        avatar_raw = None

    if avatar_raw is None:
        avatar_raw = Image.new("RGBA", (_AVATAR_SIZE, _AVATAR_SIZE), (20, 35, 65, 255))
        d = ImageDraw.Draw(avatar_raw)
        d.ellipse(
            [_AVATAR_SIZE * 0.32, _AVATAR_SIZE * 0.22, _AVATAR_SIZE * 0.68, _AVATAR_SIZE * 0.58],
            fill=(220, 235, 255, 240)
        )
        d.ellipse(
            [_AVATAR_SIZE * 0.18, _AVATAR_SIZE * 0.65, _AVATAR_SIZE * 0.82, _AVATAR_SIZE * 1.3],
            fill=(220, 235, 255, 240)
        )

    avatar_resized = avatar_raw.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.Resampling.LANCZOS)

    # 4x super-sampled circular mask
    mask = Image.new("L", (_AVATAR_SIZE * 4, _AVATAR_SIZE * 4), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse([0, 0, _AVATAR_SIZE * 4, _AVATAR_SIZE * 4], fill=255)
    mask = mask.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.Resampling.LANCZOS)

    card = template.copy()
    top_left = (_CENTER_X - _AVATAR_SIZE // 2, _CENTER_Y - _AVATAR_SIZE // 2)
    card.paste(avatar_resized, top_left, mask)

    # Neon rim glow outline around avatar
    overlay = Image.new("RGBA", card.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.ellipse(
        [
            _CENTER_X - _AVATAR_SIZE // 2 - 2,
            _CENTER_Y - _AVATAR_SIZE // 2 - 2,
            _CENTER_X + _AVATAR_SIZE // 2 + 2,
            _CENTER_Y + _AVATAR_SIZE // 2 + 2,
        ],
        outline=(0, 240, 255, 220),
        width=3,
    )
    card = Image.alpha_composite(card, overlay)

    draw = ImageDraw.Draw(card)
    font_title, font_name, font_sub = _get_fonts()

    clean_name = member_name.strip()
    if len(clean_name) > 22:
        clean_name = clean_name[:20] + "..."
    if not clean_name.startswith("@"):
        clean_name = "@" + clean_name

    # 1. Header
    _draw_glow_text(
        draw,
        "ДОБРО ПОЖАЛОВАТЬ!",
        font_title,
        _CENTER_X,
        595,
        fill=(255, 255, 255, 255),
        glow_color=(0, 180, 255, 160),
        glow_radius=4,
    )

    # 2. Member Name
    _draw_glow_text(
        draw,
        clean_name,
        font_name,
        _CENTER_X,
        650,
        fill=(0, 240, 255, 255),
        glow_color=(0, 100, 220, 140),
        glow_radius=3,
    )

    # 3. Subtitle with member count
    sub_text = (
        f"Ты {member_count}-й участник сервера {server_name}"
        if member_count
        else f"Рады видеть тебя на сервере {server_name}!"
    )
    _draw_glow_text(
        draw,
        sub_text,
        font_sub,
        _CENTER_X,
        695,
        fill=(200, 225, 255, 220),
        glow_color=(0, 60, 160, 100),
        glow_radius=2,
    )

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


async def create_welcome_card_async(member) -> io.BytesIO:
    """
    Async wrapper that does not block Discord bot event loop.
    """
    avatar_bytes = None
    try:
        avatar_bytes = await member.display_avatar.read()
    except Exception as e:
        log.warning("Could not fetch avatar for %s: %s", member, e)

    server_name = member.guild.name if member.guild else "MIAMI DM"
    member_count = member.guild.member_count if member.guild else 0

    return await asyncio.to_thread(
        generate_welcome_card,
        avatar_bytes,
        member.display_name,
        server_name,
        member_count,
    )
