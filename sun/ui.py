import streamlit as st

from locations.logic import _get_loc_by_label
from sun.astronomy import compute_season_azimuths
from sun.imagery import get_static_osm_image
from sun.rendering import draw_solar_overlay


def render_sun():
    with st.expander(
        "☀️ Sun & Light Analysis",
        expanded=st.session_state["sun_expanded"],
    ):
        st.subheader("Annual Sun Exposure")

        with st.expander("ℹ️ How to read this chart"):
            st.markdown(
                """
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
            )

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

