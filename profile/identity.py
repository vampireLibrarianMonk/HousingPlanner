"""Helpers for deriving profile identity from EC2 tags and session state."""

from __future__ import annotations
import os
import re
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import boto3
import streamlit as st

IMDS_TOKEN_URL = "http://169.254.169.254/latest/api/token"
IMDS_BASE_URL = "http://169.254.169.254/latest"


class ProfileIdentityError(RuntimeError):
    """Raised when profile identity cannot be derived."""


@dataclass(frozen=True)
class HouseAddress:
    street: str
    city: str
    state: str


def _fetch_imds_token(timeout: float = 1.0) -> Optional[str]:
    request = urllib.request.Request(
        IMDS_TOKEN_URL,
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except Exception:
        return None


def _imds_get(path: str, timeout: float = 1.0) -> Optional[str]:
    token = _fetch_imds_token(timeout=timeout)
    headers = {"X-aws-ec2-metadata-token": token} if token else {}
    request = urllib.request.Request(
        f"{IMDS_BASE_URL}/{path.lstrip('/')}",
        method="GET",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except Exception:
        return None


def get_instance_id() -> Optional[str]:
    return _imds_get("meta-data/instance-id")


def get_owner_sub(tag_key: str = "OwnerSub") -> Optional[str]:
    env_owner = os.getenv("HOUSE_PLANNER_OWNER_SUB") or os.getenv("PROFILE_OWNER_SUB")
    if env_owner:
        return env_owner
    instance_id = get_instance_id()
    if not instance_id:
        return None

    ec2 = boto3.client("ec2")
    response = ec2.describe_instances(InstanceIds=[instance_id])
    reservations = response.get("Reservations", [])
    for reservation in reservations:
        for instance in reservation.get("Instances", []):
            for tag in instance.get("Tags", []) or []:
                if tag.get("Key") == tag_key:
                    return tag.get("Value")
    return None


def _parse_house_location(location: Dict[str, Any]) -> Optional[HouseAddress]:
    address = location.get("address") or ""
    parts = [part.strip() for part in address.split(",") if part.strip()]
    if len(parts) < 3:
        return None

    street = parts[0]
    city = parts[1]
    state = parts[2].split()[0]
    if not (street and city and state):
        return None
    return HouseAddress(street=street, city=city, state=state)


def get_house_address() -> HouseAddress:
    locations = st.session_state.get("map_data", {}).get("locations", [])
    for location in locations:
        if location.get("label", "").strip().lower() == "house":
            parsed = _parse_house_location(location)
            if parsed:
                return parsed
            raise ProfileIdentityError(
                "House location address must include street, city, and state separated by commas."
            )
    raise ProfileIdentityError("House location not found. Add a location labeled 'House'.")


def slugify(value: str) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"[^a-z0-9\s-]", "", normalized)
    normalized = re.sub(r"\s+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized)
    return normalized


def build_house_slug(address: HouseAddress) -> str:
    return f"{slugify(address.street)}_{slugify(address.city)}_{slugify(address.state)}"


def get_profile_identity(tag_key: str = "OwnerSub") -> Tuple[str, str]:
    owner_sub = get_owner_sub(tag_key=tag_key)
    if not owner_sub:
        raise ProfileIdentityError(
            "Could not resolve OwnerSub tag (set HOUSE_PLANNER_OWNER_SUB when running locally)."
        )
    house = get_house_address()
    return owner_sub, build_house_slug(house)


def profile_key(owner_sub: str, house_slug: str) -> str:
    return f"{owner_sub}/{house_slug}"


def bucket_name_for_owner(owner_sub: str, prefix: str) -> str:
    return f"{prefix}-{owner_sub}".lower()


def get_storage_bucket_prefix(
    env_var: str = "STORAGE_BUCKET_PREFIX",
    ssm_param_arn: str = "STORAGE_BUCKET_PREFIX_PARAM",
    fallback_param_name: str = "/houseplanner/storage/bucket_prefix",
) -> Optional[str]:
    prefix = os.getenv(env_var)
    if prefix:
        normalized = prefix.strip()
        if normalized and "${" not in normalized and "__" not in normalized:
            return normalized

    param_name = os.getenv(ssm_param_arn) or fallback_param_name
    if not param_name:
        return None

    ssm = boto3.client("ssm")
    response = ssm.get_parameter(Name=param_name)
    return response["Parameter"]["Value"]