from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError
import requests

ZILLOW_HOST = "https://api.openwebninja.com"
ZILLOW_SEARCH_PATH = "/realtime-zillow-data/search"
ZILLOW_POLYGON_PATH = "/realtime-zillow-data/search-polygon"
# Note: Zillow API uses same OpenWebNinja key as Waze
ZILLOW_SECRET_NAME = "houseplanner/waze_api_key"


@lru_cache(maxsize=1)
def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        raise RuntimeError(f"Unable to load secret '{secret_name}': {exc}")
    return resp["SecretString"]


def load_zillow_api_key() -> str:
    try:
        return _get_secret(ZILLOW_SECRET_NAME)
    except Exception as exc:
        raise RuntimeError(
            "Missing Zillow/Waze API key. Create the AWS Secrets Manager secret "
            f"'{ZILLOW_SECRET_NAME}' or set it for the app runtime. ({exc})"
        )


def search_zillow_properties(
    *,
    api_key: str,
    params: dict[str, Any],
    timeout: int = 30,
    use_polygon: bool = False,
) -> dict[str, Any]:
    path = ZILLOW_POLYGON_PATH if use_polygon else ZILLOW_SEARCH_PATH
    url = f"{ZILLOW_HOST}{path}"
    headers = {"x-api-key": api_key}
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
