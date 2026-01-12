# ---------------------------------------------
# Global state
# ---------------------------------------------
from state import init_state
init_state()

# ---------------------------------------------
# Load environment variables (.env)
# ---------------------------------------------
from dotenv import load_dotenv
load_dotenv()

# =============================
# Third-party: Core app & data
# =============================
import streamlit as st

# -----------------------------
# Session State
# -----------------------------
if "map_badge" not in st.session_state:
    st.session_state["map_badge"] = "3 locations"

if "map_expanded" not in st.session_state:
    st.session_state["map_expanded"] = False

if "mortgage_expanded" not in st.session_state:
    st.session_state["mortgage_expanded"] = False

if "commute_results" not in st.session_state:
    # Holds results per provider: {"ORS": {...}, "Google": {...}}
    st.session_state["commute_results"] = {}

if "commute_expanded" not in st.session_state:
    st.session_state["commute_expanded"] = False

if "sun_expanded" not in st.session_state:
    st.session_state["sun_expanded"] = False

if "disaster_expanded" not in st.session_state:
    st.session_state["disaster_expanded"] = False

if "disaster_radius_miles" not in st.session_state:
    st.session_state["disaster_radius_miles"] = 5

if "show_ors" not in st.session_state:
    st.session_state["show_ors"] = False

if "show_google" not in st.session_state:
    st.session_state["show_google"] = False

if "show_markers" not in st.session_state:
    st.session_state["show_markers"] = False

if "hz_flood" not in st.session_state:
    st.session_state["hz_flood"] = False

if "hz_wildfire" not in st.session_state:
    st.session_state["hz_wildfire"] = False

if "hz_earthquake" not in st.session_state:
    st.session_state["hz_earthquake"] = False

if "hz_wind" not in st.session_state:
    st.session_state["hz_wind"] = False

if "hz_heat" not in st.session_state:
    st.session_state["hz_heat"] = False

if "hz_disaster_history" not in st.session_state:
    st.session_state["hz_disaster_history"] = False

if "hz_land_use" not in st.session_state:
    st.session_state["hz_land_use"] = False


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="House Planner (Prototype)", layout="wide")

st.title("House Planner (Prototype)")

# -----------------------------
# Safe defaults for section badges
# -----------------------------
map_badge = "0 locations"
commute_badge = "—"

# =============================
# Mortgage Section
# =============================
from mortgage.ui import render_mortgage

if "mortgage_badge" not in st.session_state:
    st.session_state["mortgage_badge"] = "Monthly: —"

method = st.selectbox(
    "Calculation method",
    ["Bankrate-style", "NerdWallet-style"],
    help="Affects input conventions and displayed assumptions."
)

render_mortgage(method)

# =============================
# Location Management Section
# =============================
from locations.ui import render_locations

render_locations()

# =============================
# Commute Section
# =============================
from commute.ui import render_commute

render_commute()

# =============================
# Sun & Light Analysis
# =============================
from sun.ui import render_sun

render_sun()

# =============================
# Disaster Risk & Hazard Mapping
# =============================
from disaster.ui import render_disaster

render_disaster()
