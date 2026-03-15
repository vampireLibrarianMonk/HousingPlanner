"""Streamlit UI for profile save/load."""

from __future__ import annotations

import streamlit as st

from .costs import render_api_usage_costs, render_document_operations_costs, render_usage_costs
from .identity import (
    ProfileIdentityError,
    get_profile_identity,
    profile_key,
)
from .state_io import apply_profile, extract_profile
from .storage import delete_profile, list_profiles, load_profile, save_profile


def render_profile_manager() -> None:
    with st.sidebar.expander("Profile Manager", expanded=False):
        st.caption("Profiles are keyed by OwnerSub + House address.")

        try:
            owner_sub, house_slug = get_profile_identity()
            current_key = profile_key(owner_sub, house_slug)
            st.session_state["profile_owner_sub"] = owner_sub
            st.session_state["profile_house_slug"] = house_slug
        except ProfileIdentityError as exc:
            st.warning(str(exc))
            st.caption(
                "Running locally? Set HOUSE_PLANNER_OWNER_SUB in your environment "
                "(e.g., `export HOUSE_PLANNER_OWNER_SUB=your-id`)."
            )
            return

        st.text_input("Current Profile Key", value=current_key, disabled=True)
        st.caption(f"House address: {house_slug.replace('_', ' ')}")

        available_profiles = list_profiles(owner_sub)
        if available_profiles:
            selected_slug = st.selectbox(
                "Available Properties",
                options=available_profiles,
                index=available_profiles.index(house_slug)
                if house_slug in available_profiles
                else 0,
            )
        else:
            st.selectbox(
                "Available Properties",
                options=[],
                index=None,
                placeholder="No saved profiles",
                disabled=True,
            )
            selected_slug = None

        save_cols = st.columns([1, 1, 1])
        if save_cols[0].button("Save Profile", width='stretch'):
            save_path = save_current_profile()
            st.success(f"Saved to {save_path}")
            st.rerun()

        load_disabled = selected_slug is None
        if save_cols[1].button("Load Profile", width='stretch', disabled=load_disabled):
            profile = load_profile(owner_sub, selected_slug)
            if not profile:
                st.error("Profile not found.")
            else:
                st.session_state["profile_owner_sub"] = owner_sub
                st.session_state["profile_house_slug"] = selected_slug
                apply_profile(profile)
                st.success("Profile loaded. Scroll to see updates.")
                st.rerun()

        delete_disabled = selected_slug is None
        confirm_key = "profile_delete_confirm"
        if confirm_key not in st.session_state:
            st.session_state[confirm_key] = False

        if not st.session_state[confirm_key]:
            if save_cols[2].button(
                "Delete",
                width='stretch',
                disabled=delete_disabled,
                type="secondary",
            ):
                st.session_state[confirm_key] = True
                st.rerun()
        else:
            if save_cols[2].button(
                "Confirm",
                width='stretch',
                disabled=delete_disabled,
                type="primary",
            ):
                if selected_slug == house_slug:
                    st.warning("You cannot delete the active profile. Load another profile first.")
                else:
                    deleted = delete_profile(owner_sub, selected_slug)
                    if deleted:
                        st.success("Profile deleted.")
                    else:
                        st.error("Profile not found.")
                st.session_state[confirm_key] = False
                st.rerun()

    # Render consolidated AI usage costs below profile manager
    render_usage_costs()
    render_document_operations_costs()
    render_api_usage_costs()


def save_current_profile() -> str:
    owner_sub, house_slug = get_profile_identity()
    st.session_state["profile_owner_sub"] = owner_sub
    st.session_state["profile_house_slug"] = house_slug
    profile = extract_profile()
    save_path = save_profile(owner_sub, house_slug, profile)
    return str(save_path)