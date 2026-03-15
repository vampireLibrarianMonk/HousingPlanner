import streamlit as st
from fpdf import FPDF
import tempfile
import os
from pathlib import Path

from locations.logic import _get_loc_by_label
from sun.astronomy import compute_season_azimuths
from sun.imagery import get_static_osm_image
from sun.rendering import draw_solar_overlay


SUN_HELP_TEXT = """
### What this chart shows

This diagram summarizes **the directions from which the sun reaches this property over the course of a year**.

Each colored wedge represents a **compass direction** (north, east, south, west, etc.) where sunlight is present at some point during the day.

The sun generally rises in the east, moves across the southern sky, and sets in the west, with the exact angle shifting slightly between winter and summer.

---

### How the sun paths are calculated

- The house location is used to determine **local sunrise and sunset times**.
- For three representative dates:
  - **Winter** (December 21)
  - **Equinox** (March 20)
  - **Summer** (June 21)
- The sun’s position is sampled **every 10 minutes** while it is above the horizon.
- Each sun position contributes to a **5° compass direction bin**.
- For each direction, the season with the **most sun presence** becomes the dominant color.

This ensures each direction is assigned to **one clear season**, avoiding confusing overlaps.

---

### What the colors mean

- **Blue (Winter)** → Sun most often comes from this direction during Dec–Feb
- **Green (Equinox)** → Sun most often comes from this direction during spring & fall
- **Orange (Summer)** → Sun most often comes from this direction during Jun–Aug

The legend below the chart maps **months to seasons**.

---

### How to interpret gaps

If part of the circle has **no color**, it means:
- The sun **never rises above the horizon** in that direction at this location,
- At any time of year.

This is common for **north-facing directions** in the Northern Hemisphere.

---

### What this chart is (and is not)

✅ Shows **directional sun exposure trends**
✅ Helps compare **front vs back vs side exposure**
✅ Useful for understanding **seasonal lighting patterns**

❌ Does not show exact sunlight hours
❌ Does not account for trees or buildings
❌ Does not simulate shadows

---

### About imagery dates

The aerial image shown is provided by a satellite imagery service and is part of a **composite map mosaic**.

- The image is **not captured at a single moment in time**
- Different parts of the image may come from **different capture dates**
- Imagery is processed, corrected, and blended before being published

Because of this, a single, exact “image date” does **not exist** and would be misleading to display.

The imagery is used only for **visual context**.
All sun angle and seasonal calculations are based on **precise astronomical models** and the property’s geographic location.

---
"""


def _pdf_text(value: str) -> str:
    return str(value)


def _address_to_filename_slug(address: str) -> str:
    cleaned = (address or "unknown_address").strip().lower()
    cleaned = cleaned.replace(",", " ")
    slug = "".join(ch if ch.isalnum() else "_" for ch in cleaned)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "unknown_address"


def _configure_pdf_fonts(pdf: FPDF) -> str:
    """Configure a unicode-capable font when available (for symbols/emojis)."""
    candidates = [
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        (
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ),
    ]

    for regular_path, bold_path in candidates:
        if not os.path.exists(regular_path):
            continue
        try:
            pdf.add_font("DejaVu", "", regular_path, uni=True)
            if os.path.exists(bold_path):
                pdf.add_font("DejaVu", "B", bold_path, uni=True)
            else:
                pdf.add_font("DejaVu", "B", regular_path, uni=True)
            return "DejaVu"
        except Exception:
            continue

    return "Helvetica"


def _build_sun_pdf(
    *,
    house_address: str,
    house_lat: float,
    house_lon: float,
    tz_name: str,
    overlay_alpha: float,
    azimuths: dict[str, list[float]],
    overlay_image,
) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    font_family = _configure_pdf_fonts(pdf)

    pdf.set_font(font_family, "B", 16)
    pdf.cell(0, 10, "Sun & Light Analysis Plan", ln=True)

    pdf.set_font(font_family, "", 10)
    pdf.multi_cell(0, 6, _pdf_text(f"Address: {house_address}"))
    pdf.multi_cell(0, 6, _pdf_text(f"Coordinates: {house_lat:.6f}, {house_lon:.6f}"))
    pdf.multi_cell(0, 6, _pdf_text(f"Timezone assumption: {tz_name}"))
    pdf.multi_cell(0, 6, _pdf_text(f"Overlay transparency selected: {overlay_alpha:.2f}"))
    pdf.ln(2)

    tmp_img_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp_img_path = tmp.name
        overlay_image.save(tmp_img_path, format="PNG")
        pdf.set_font(font_family, "B", 12)
        pdf.cell(0, 8, "Sun Angle Overlay", ln=True)
        pdf.image(tmp_img_path, w=185)
        pdf.ln(2)
    except Exception:
        # Keep PDF generation resilient even if image serialization fails.
        pass
    finally:
        if tmp_img_path and Path(tmp_img_path).exists():
            os.remove(tmp_img_path)

    pdf.set_font(font_family, "B", 12)
    pdf.cell(0, 8, "Legend", ln=True)
    pdf.set_font(font_family, "", 10)

    legend_rows = [
        ((251, 140, 0), "Summer (Jun–Aug)"),
        ((67, 160, 71), "Spring & Fall (Mar–May, Sep–Nov)"),
        ((30, 136, 229), "Winter (Dec–Feb)"),
    ]

    for (r, g, b), label in legend_rows:
        current_x = pdf.get_x()
        current_y = pdf.get_y()
        pdf.set_fill_color(r, g, b)
        pdf.cell(6, 6, "", border=0, ln=0, fill=True)
        pdf.set_xy(current_x + 8, current_y)
        pdf.cell(0, 6, _pdf_text(label), ln=True)

    pdf.add_page()
    pdf.set_font(font_family, "B", 12)
    pdf.cell(0, 8, "Help Menu Content", ln=True)
    pdf.set_font(font_family, "", 10)

    for line in SUN_HELP_TEXT.strip().splitlines():
        cleaned = line.replace("**", "").replace("###", "").replace("---", "").strip()
        if cleaned:
            pdf.multi_cell(0, 6, _pdf_text(cleaned))
        else:
            pdf.ln(1)

    output = pdf.output(dest="S")
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    return output.encode("latin-1")


def render_sun():
    with st.expander(
        "☀️ Sun & Light Analysis",
        expanded=st.session_state["sun_expanded"],
    ):
        st.subheader("Annual Sun Exposure")

        with st.expander("ℹ️ How to read this chart"):
            st.markdown(SUN_HELP_TEXT)

        overlay_alpha = st.slider(
            "Overlay transparency",
            min_value=0.05,
            max_value=0.80,
            value=0.60,
            step=0.05,
            help="Controls visibility of seasonal sun coverage overlay.",
        )

        house = _get_loc_by_label(
            st.session_state["map_data"]["locations"],
            "House",
        )

        if not house:
            st.info("Add a location labeled **House** to enable sun analysis.")
            return

        tz_name = "America/New_York"  # prototype assumption

        base_img = get_static_osm_image(
            house["lat"],
            house["lon"],
            zoom=19,
            size=800,
            cache_buster=f"{house['lat']:.6f},{house['lon']:.6f}",
        )

        azimuths = compute_season_azimuths(
            house["lat"],
            house["lon"],
            tz_name,
        )

        overlay = draw_solar_overlay(
            base_img,
            azimuths,
            base_alpha=overlay_alpha,
        )

        st.image(overlay, width="stretch")

        if st.button("Generate Sun PDF", key="generate_sun_pdf"):
            try:
                st.session_state["sun_pdf_bytes"] = _build_sun_pdf(
                    house_address=house.get("address", "Unknown address"),
                    house_lat=float(house["lat"]),
                    house_lon=float(house["lon"]),
                    tz_name=tz_name,
                    overlay_alpha=float(overlay_alpha),
                    azimuths=azimuths,
                    overlay_image=overlay,
                )
                st.success("Sun analysis PDF generated. Click Download Sun PDF to save it.")
            except Exception as exc:
                st.error(f"Generate Sun PDF failed: {exc}")

        sun_pdf_bytes = st.session_state.get("sun_pdf_bytes")
        if sun_pdf_bytes:
            house_slug = _address_to_filename_slug(str(house.get("address", "unknown_address")))
            st.download_button(
                "Download Sun PDF",
                data=sun_pdf_bytes,
                file_name=f"sun_light_analysis_plan_{house_slug}.pdf",
                mime="application/pdf",
                key="download_sun_pdf",
            )

        st.markdown(
            """
        <div style="
            display: flex;
            gap: 24px;
            align-items: center;
            margin-top: 10px;
            font-size: 14px;
        ">
          <div>
            <span style="
                display:inline-block;
                width:14px;
                height:14px;
                background:#FB8C00;
                margin-right:6px;
            "></span>
            <strong>Summer</strong> (Jun–Aug)
          </div>
        
          <div>
            <span style="
                display:inline-block;
                width:14px;
                height:14px;
                background:#43A047;
                margin-right:6px;
            "></span>
            <strong>Spring & Fall</strong> (Mar–May, Sep–Nov)
          </div>
        
          <div>
            <span style="
                display:inline-block;
                width:14px;
                height:14px;
                background:#1E88E5;
                margin-right:6px;
            "></span>
            <strong>Winter</strong> (Dec–Feb)
          </div>
        </div>
        """,
            unsafe_allow_html=True,
        )

