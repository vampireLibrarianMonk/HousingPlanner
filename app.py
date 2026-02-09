# ---------------------------------------------
# Global state
# ---------------------------------------------
from dotenv import load_dotenv
from pathlib import Path

from state import init_state
from profile.storage import ensure_profiles_dir

# Load local environment variables for development (does not override existing env)
#
# Production deployments typically inject env vars via systemd or another mechanism.
load_dotenv(override=False)
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

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


# Logout link in sidebar - force same-tab navigation to /logout
# The /logout endpoint is handled by nginx which clears ALB cookies and redirects to Cognito logout
st.sidebar.markdown(
    """
    <a href="/logout" target="_self" style="text-decoration:none;">
      <button style="width:100%;padding:0.5rem 0.75rem;border:none;border-radius:0.5rem;"
              aria-label="Logout">
        🚪 Logout
      </button>
    </a>
    """,
    unsafe_allow_html=True,
)

st.title("House Planner (Prototype)")

# =============================
# Home Buying Checklist & Notes
# =============================
from assistant.ui import render_checklist_and_notes, render_floating_chatbot
from hoa.ui import render_document_vetting

render_checklist_and_notes()

# =============================
# Document Vetting
# =============================
render_document_vetting()

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
# Neighborhood Analysis
# =============================
from neighborhood.ui import render_neighborhood

render_neighborhood()

# =============================
# Sun & Light Analysis
# =============================
from sun.ui import render_sun

render_sun()

# =============================
# Schools & Districts
# =============================
from schools.ui import render_schools

render_schools()

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
