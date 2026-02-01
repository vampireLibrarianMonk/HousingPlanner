# ---------------------------------------------
# Global state
# ---------------------------------------------
from state import init_state
init_state()

# =============================
# Third-party: Core app & data
# =============================
import streamlit as st

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="House Planner (Prototype)", layout="wide")


# Logout button in sidebar - uses JavaScript to navigate top window (avoids iframe issues)
st.sidebar.markdown(
    '''<a href="/logout" onclick="window.top.location.href='/logout';return false;" 
    style="display:inline-block;padding:0.5rem 1rem;background:#dc3545;color:white;
    text-decoration:none;border-radius:0.25rem;font-weight:500;cursor:pointer;">🚪 Logout</a>''',
    unsafe_allow_html=True
)

st.title("House Planner (Prototype)")

# =============================
# Home Buying Checklist & Notes
# =============================
from assistant.ui import render_checklist_and_notes, render_floating_chatbot

render_checklist_and_notes()

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

render_mortgage()

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

# =============================
# Profile Manager (sidebar)
# =============================
from profile.ui import render_profile_manager

render_profile_manager()

# =============================
# Floating Chatbot Assistant
# =============================
render_floating_chatbot()
