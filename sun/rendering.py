from PIL import ImageDraw, ImageFont, Image


def draw_solar_overlay(base_img, azimuths_by_season, base_alpha=0.60):
    img = base_img.copy().convert("RGBA")
    draw = ImageDraw.Draw(img)

    size = img.size[0]
    cx = cy = size // 2
    r_inner = int(0.15 * size)
    r_outer = int(0.45 * size)

    # Fixed, legend-safe colors
    season_colors = {
        "Winter": (79, 195, 247),
        "Equinox": (129, 199, 132),
        "Summer": (255, 183, 77),
    }

    # ----------------------------------------
    # 1) Build dominant-season ownership per 5° bin
    # ----------------------------------------
    dominant_bins = {}

    for season, azimuths in azimuths_by_season.items():
        for a in azimuths:
            bin_angle = int(a // 5) * 5
            dominant_bins.setdefault(bin_angle, {})
            dominant_bins[bin_angle][season] = (
                dominant_bins[bin_angle].get(season, 0) + 1
            )

    # ----------------------------------------
    # 2) Render each bin ONCE, using dominant season
    # ----------------------------------------
    alpha = int(255 * base_alpha)

    for angle, season_counts in dominant_bins.items():
        dominant_season = max(season_counts, key=season_counts.get)
        color = (*season_colors[dominant_season], alpha)

        start_deg = angle - 90
        end_deg = angle + 5 - 90

        # Per-bin overlay
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)

        # Outer wedge
        odraw.pieslice(
            [
                cx - r_outer,
                cy - r_outer,
                cx + r_outer,
                cy + r_outer,
            ],
            start_deg,
            end_deg,
            fill=color,
        )

        # Inner cutout mask
        mask = Image.new("L", img.size, 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.pieslice(
            [
                cx - r_inner,
                cy - r_inner,
                cx + r_inner,
                cy + r_inner,
            ],
            start_deg,
            end_deg,
            fill=255,
        )

        overlay.paste((0, 0, 0, 0), mask=mask)
        img.alpha_composite(overlay)

    # ----------------------------------------
    # 3) Reference ring (context, not data)
    # ----------------------------------------
    draw.ellipse(
        [
            cx - r_outer,
            cy - r_outer,
            cx + r_outer,
            cy + r_outer,
        ],
        outline=(255, 255, 255, int(255 * base_alpha * 0.15)),
        width=2,
    )

    # ----------------------------------------
    # 4) Compass (cardinal directions)
    # ----------------------------------------
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    except IOError:
        font = ImageFont.load_default()
    compass_radius = r_inner - 18

    draw.text((cx - 6, cy - compass_radius), "N", fill="white", font=font)
    draw.text((cx - 6, cy + compass_radius - 14), "S", fill="white", font=font)
    draw.text((cx + compass_radius - 12, cy - 8), "E", fill="white", font=font)
    draw.text((cx - compass_radius + 4, cy - 8), "W", fill="white", font=font)

    return img

