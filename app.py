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


# Logout button in sidebar - clears ALB cookies and redirects to Cognito logout
st.session_state.setdefault("logout_requested", False)

# Get Cognito configuration from environment for proper logout URL
import os
_cognito_domain = os.getenv("COGNITO_DOMAIN", "")
_cognito_client_id = os.getenv("COGNITO_CLIENT_ID", "")
_app_domain = os.getenv("APP_DOMAIN", "app.housing-planner.com")

if st.sidebar.button("🚪 Logout", width='stretch'):
    st.session_state["logout_requested"] = True

if st.session_state["logout_requested"]:
    # Build the Cognito logout URL
    # This clears both Cognito session AND triggers proper redirect
    if _cognito_domain and _cognito_client_id:
        cognito_logout_url = (
            f"https://{_cognito_domain}/logout?"
            f"client_id={_cognito_client_id}&"
            f"logout_uri=https://{_app_domain}"
        )
    else:
        # Fallback - just redirect to home (will require re-auth via ALB)
        cognito_logout_url = f"https://{_app_domain}"
    
    # JavaScript to clear ALL ALB session cookies and redirect to Cognito logout
    st.components.v1.html(
        f"""
        <script>
          // Clear all ALB OIDC session cookies
          document.cookie.split(';').forEach(function(c) {{
            var name = c.trim().split('=')[0];
            if (name.startsWith('AWSELBAuthSessionCookie')) {{
              document.cookie = name + '=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;';
            }}
          }});
          
          // Also try to clear with domain variations
          var domain = window.location.hostname;
          document.cookie.split(';').forEach(function(c) {{
            var name = c.trim().split('=')[0];
            if (name.startsWith('AWSELBAuthSessionCookie')) {{
              document.cookie = name + '=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; domain=' + domain;
              document.cookie = name + '=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/; domain=.' + domain;
            }}
          }});
          
          // Redirect to Cognito logout (or home) after clearing cookies
          setTimeout(function() {{
            window.location.href = '{cognito_logout_url}';
          }}, 100);
        </script>
        """,
        height=0,
    )

    st.markdown(
        """
        <style>
          section[data-testid="stSidebar"],
          div[data-testid="stSidebar"] {
            display: none !important;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("""
    # Logging out...
    You will be redirected to complete the logout process.
    """)
    st.stop()

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
