# ---------------------------------------------
# Global state
# ---------------------------------------------
from state import init_state
from profile.storage import ensure_profiles_dir

ensure_profiles_dir()
init_state()

# =============================
# Third-party: Core app & data
# =============================
import streamlit as st

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="House Planner (Prototype)", layout="wide")


# Logout button in sidebar - opens logout in a new tab, then attempts to close this one
if st.sidebar.button("🚪 Logout", width='stretch'):
    st.components.v1.html(
        """
        <script>
          const logoutUrl = '/logout';
          const newTab = window.open(logoutUrl, '_blank', 'noopener');
          if (newTab) {
            newTab.focus();
          }
          // Attempt to close the current window (may be blocked by browser settings)
          window.close();
        </script>
        """,
        height=0,
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
