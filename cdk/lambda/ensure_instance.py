import boto3
import os
import hashlib
import logging
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
elbv2 = boto3.client("elbv2")
secretsmanager = boto3.client("secretsmanager")

# Cache values to avoid repeated API calls
_cached_listener_arn = None
_cached_client_secret = None


def _get_client_secret() -> str:
    """
    Get the OIDC client secret from Secrets Manager.
    Caches the result for subsequent calls.
    """
    global _cached_client_secret
    if _cached_client_secret:
        return _cached_client_secret

    secret_arn = os.environ["OIDC_CLIENT_SECRET_ARN"]
    resp = secretsmanager.get_secret_value(SecretId=secret_arn)
    _cached_client_secret = resp["SecretString"]
    return _cached_client_secret


def _get_https_listener_arn(alb_arn: str) -> str:
    """
    Discover the HTTPS (port 443) listener ARN from the ALB.
    Caches the result for subsequent calls.
    """
    global _cached_listener_arn
    if _cached_listener_arn:
        return _cached_listener_arn

    resp = elbv2.describe_listeners(LoadBalancerArn=alb_arn)
    for listener in resp.get("Listeners", []):
        if listener.get("Port") == 443:
            _cached_listener_arn = listener["ListenerArn"]
            logger.info("Discovered HTTPS listener: %s", _cached_listener_arn)
            return _cached_listener_arn

    raise RuntimeError(f"No HTTPS listener found on ALB {alb_arn}")


def _get_header(event: dict, name: str) -> str | None:
    """
    ALB Lambda events may provide headers in:
      - event["headers"] (single-value)
      - event["multiValueHeaders"] (list per header)
    Header keys may vary in case. We normalize to lowercase.
    """
    lname = name.lower()

    headers = event.get("headers") or {}
    for k, v in headers.items():
        if k.lower() == lname and v:
            return v

    mvh = event.get("multiValueHeaders") or {}
    for k, vlist in mvh.items():
        if k.lower() == lname and vlist:
            return vlist[0]

    return None


def _alb_response(status_code: int, body: str = "", extra_headers: dict = None) -> dict:
    # ALB Lambda target supports this minimal shape.
    headers = {"content-type": "text/plain"}
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": body,
        "isBase64Encoded": False,
    }


def _routing_cookie_name(user_sub: str) -> str:
    """
    Generate a deterministic routing cookie name for the user.
    This cookie is used to match ALB listener rules.
    """
    suffix = hashlib.sha1(user_sub.encode()).hexdigest()[:12]
    return f"hp_route_{suffix}"


def _tg_name(user_sub: str, listener_arn: str) -> str:
    # TG name limit is 32 chars; keep it short and deterministic.
    suffix = hashlib.sha1((user_sub + listener_arn).encode()).hexdigest()[:16]
    return f"u-{suffix}"  # 18 chars total


def _get_or_create_target_group(tg_name: str, vpc_id: str) -> dict:
    # Try to find existing TG by name first.
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        tgs = resp.get("TargetGroups", [])
        if tgs:
            return tgs[0]
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        # If not found, we create it. Otherwise re-raise.
        if code not in ("TargetGroupNotFound",):
            raise

    # Create it if missing with fast health check settings.
    # Fast health checks: 5s interval, 2 healthy/unhealthy thresholds
    # This means ~10 seconds to become healthy (vs default ~2.5 minutes)
    resp = elbv2.create_target_group(
        Name=tg_name,
        Protocol="HTTP",
        Port=80,
        VpcId=vpc_id,
        TargetType="instance",
        HealthCheckProtocol="HTTP",
        HealthCheckPath="/",
        HealthCheckIntervalSeconds=5,
        HealthCheckTimeoutSeconds=3,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=2,
    )
    
    tg = resp["TargetGroups"][0]
    
    # Also enable stickiness to keep user on same instance
    elbv2.modify_target_group_attributes(
        TargetGroupArn=tg["TargetGroupArn"],
        Attributes=[
            {"Key": "stickiness.enabled", "Value": "true"},
            {"Key": "stickiness.type", "Value": "lb_cookie"},
            {"Key": "stickiness.lb_cookie.duration_seconds", "Value": "86400"},
        ],
    )
    
    return tg


def _find_existing_rule_for_user(listener_arn: str, user_sub: str) -> dict | None:
    """
    Looks for a listener rule that matches the user's routing cookie.
    The cookie name is deterministically generated from user_sub.
    """
    cookie_name = _routing_cookie_name(user_sub)
    rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
    for r in rules:
        for cond in r.get("Conditions", []):
            if cond.get("Field") == "http-header":
                cfg = cond.get("HttpHeaderConfig") or {}
                # Cookie header contains our routing cookie
                if cfg.get("HttpHeaderName", "").lower() == "cookie":
                    values = cfg.get("Values") or []
                    # Check if any value contains our cookie name
                    for v in values:
                        if cookie_name in v:
                            return r
    return None


def _next_available_priority(listener_arn: str, preferred: int) -> int:
    """
    Ensure we never fail due to priority collisions.
    Try preferred first; if taken, scan for an open priority.
    User rules should be in range 2-9999 (after /internal/ensure at 1, before default).
    """
    rules = elbv2.describe_rules(ListenerArn=listener_arn)["Rules"]
    used = {int(r["Priority"]) for r in rules if r.get("Priority", "").isdigit()}

    if preferred not in used:
        return preferred

    # Scan for an available priority in range 2-9999
    for p in range(2, 10000):
        if p not in used:
            return p

    raise RuntimeError("No available listener rule priorities in range 2-9999")


def _ensure_rule(listener_arn: str, user_sub: str, target_group_arn: str) -> None:
    """
    Create a listener rule that:
    1. Matches requests with the user's routing cookie
    2. Authenticates with OIDC (to ensure session is valid)
    3. Forwards to the user's target group
    
    The routing cookie is set by this Lambda after provisioning.
    This approach works because cookies ARE available during rule condition evaluation,
    unlike the x-amzn-oidc-identity header which is only set after OIDC auth runs.
    """
    # If the rule already exists for this user, do nothing.
    existing = _find_existing_rule_for_user(listener_arn, user_sub)
    if existing:
        logger.info("Existing rule found for user_sub; skipping create_rule.")
        return

    # Compute deterministic preferred priority, then find an open one if it collides.
    # Use priority range 2-9999 so user rules are checked before default action
    # but after /internal/ensure (priority 1)
    preferred = 2 + (int(hashlib.sha1(user_sub.encode()).hexdigest()[:6], 16) % 9997)
    priority = _next_available_priority(listener_arn, preferred)

    # Get OIDC config from environment
    client_secret = _get_client_secret()
    
    # Get the routing cookie name for this user
    cookie_name = _routing_cookie_name(user_sub)
    
    # Create rule that matches on the routing cookie
    # The Cookie header contains all cookies, so we use a wildcard pattern
    elbv2.create_rule(
        ListenerArn=listener_arn,
        Priority=priority,
        Conditions=[
            {
                "Field": "http-header",
                "HttpHeaderConfig": {
                    "HttpHeaderName": "Cookie",
                    # Match cookie name followed by =1 anywhere in Cookie header
                    "Values": [f"*{cookie_name}=1*"],
                },
            }
        ],
        Actions=[
            # Action 1: Authenticate with OIDC (order=1)
            {
                "Type": "authenticate-oidc",
                "Order": 1,
                "AuthenticateOidcConfig": {
                    "Issuer": os.environ["OIDC_ISSUER"],
                    "AuthorizationEndpoint": os.environ["OIDC_AUTH_ENDPOINT"],
                    "TokenEndpoint": os.environ["OIDC_TOKEN_ENDPOINT"],
                    "UserInfoEndpoint": os.environ["OIDC_USER_INFO_ENDPOINT"],
                    "ClientId": os.environ["OIDC_CLIENT_ID"],
                    "ClientSecret": client_secret,
                    "Scope": "openid email",
                    "OnUnauthenticatedRequest": "authenticate",
                },
            },
            # Action 2: Forward to user's target group (order=2)
            {
                "Type": "forward",
                "Order": 2,
                "TargetGroupArn": target_group_arn,
            },
        ],
    )
    logger.info("Created rule priority=%s for cookie=%s with OIDC auth", priority, cookie_name)


def lambda_handler(event, context):
    # 1) Confirm identity (provided by ALB authenticate_oidc)
    user_sub = _get_header(event, "x-amzn-oidc-identity")
    if not user_sub:
        logger.warning("Missing x-amzn-oidc-identity header")
        return _alb_response(401, "Missing authenticated user identity")

    # Discover listener ARN from ALB to avoid circular dependency at deploy time
    alb_arn = os.environ["ALB_ARN"]
    listener_arn = _get_https_listener_arn(alb_arn)
    vpc_id = os.environ["VPC_ID"]

    # Generate the routing cookie for this user
    # This cookie will be sent back to the browser and used for subsequent routing
    cookie_name = _routing_cookie_name(user_sub)
    # Set cookie with long expiry, secure, httponly, and SameSite=Lax for redirect compatibility
    set_cookie = f"{cookie_name}=1; Path=/; Max-Age=31536000; Secure; HttpOnly; SameSite=Lax"

    # 2) Find an existing instance (prefer running/pending)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:OwnerSub", "Values": [user_sub]},
            {"Name": "tag:Purpose", "Values": ["HousePlannerUser"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopped"]},
        ]
    )
    instances = [
        i for r in resp.get("Reservations", []) for i in r.get("Instances", [])
    ]

    def _state_rank(inst):
        s = inst["State"]["Name"]
        return {"running": 0, "pending": 1, "stopped": 2}.get(s, 99)

    instances.sort(key=_state_rank)

    # 3) Ensure TG + rule always exist (idempotent)
    tg_name = _tg_name(user_sub, listener_arn)
    tg = _get_or_create_target_group(tg_name, vpc_id)

    # CASE 1 — Instance exists
    if instances:
        inst = instances[0]
        instance_id = inst["InstanceId"]
        state = inst["State"]["Name"]
        logger.info("Found instance %s state=%s for user_sub", instance_id, state)

        # Register target (safe to call repeatedly)
        elbv2.register_targets(
            TargetGroupArn=tg["TargetGroupArn"],
            Targets=[{"Id": instance_id, "Port": 80}],
        )

        _ensure_rule(listener_arn, user_sub, tg["TargetGroupArn"])

        if state == "stopped":
            ec2.start_instances(InstanceIds=[instance_id])
            logger.info("Starting stopped instance %s", instance_id)

        # Return with Set-Cookie header so browser stores the routing cookie
        return _alb_response(200, "OK", {"set-cookie": set_cookie})

    # CASE 2 — No instance → create one
    run = ec2.run_instances(
        LaunchTemplate={
            "LaunchTemplateId": os.environ["LAUNCH_TEMPLATE_ID"],
            "Version": "$Latest",
        },
        MinCount=1,
        MaxCount=1,
        SubnetId=os.environ["SUBNET_ID"],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "OwnerSub", "Value": user_sub},
                    {"Key": "Purpose", "Value": "HousePlannerUser"},
                    {"Key": "App", "Value": "HousePlanner"},
                ],
            }
        ],
    )

    instance_id = run["Instances"][0]["InstanceId"]
    logger.info("Created new instance %s for user_sub", instance_id)

    # Register target + ensure rule
    elbv2.register_targets(
        TargetGroupArn=tg["TargetGroupArn"],
        Targets=[{"Id": instance_id, "Port": 80}],
    )
    _ensure_rule(listener_arn, user_sub, tg["TargetGroupArn"])

    # Return with Set-Cookie header so browser stores the routing cookie
    return _alb_response(200, "Provisioning started", {"set-cookie": set_cookie})
