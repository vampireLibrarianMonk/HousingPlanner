"""
delete_instance.py

Lambda handler for cleaning up user resources when a Cognito user is deleted.

Triggered by:
- EventBridge rule on Cognito AdminDeleteUser API call

Responsibilities:
- Terminate the user's EC2 instance
- Delete the user's ALB listener rule
- Delete the user's target group
"""

import hashlib
import logging
import boto3
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ec2 = boto3.client("ec2")
elbv2 = boto3.client("elbv2")
cognito = boto3.client("cognito-idp")

# Cache the listener ARN to avoid repeated API calls
_cached_listener_arn = None


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


def _resolve_to_sub(event: dict) -> str:
    """
    Extract the user's 'sub' (unique identifier) from the event.
    
    Handles:
    - EventBridge CloudTrail AdminDeleteUser events (username in requestParameters)
    - Cognito PRE_DELETE trigger events (sub in userAttributes)
    """
    # 1) EventBridge CloudTrail AdminDeleteUser (username/email)
    detail = event.get("detail", {})
    rp = detail.get("requestParameters", {}) or {}
    username = rp.get("username")
    
    if username:
        try:
            resp = cognito.admin_get_user(
                UserPoolId=os.environ["USER_POOL_ID"],
                Username=username,
            )
            attrs = {a["Name"]: a["Value"] for a in resp.get("UserAttributes", [])}
            user_sub = attrs.get("sub")
            if user_sub:
                logger.info("Resolved username %s to sub %s", username, user_sub)
                return user_sub
            return username
        except cognito.exceptions.UserNotFoundException:
            # User already deleted, try to use username as-is
            logger.warning("User %s not found in Cognito, using as identifier", username)
            return username

    # 2) Cognito PRE_DELETE trigger
    attrs = event.get("request", {}).get("userAttributes", {}) or {}
    user_sub = attrs.get("sub") or event.get("userName")
    logger.info("Resolved user_sub from trigger: %s", user_sub)
    return user_sub


def _tg_name(user_sub: str, listener_arn: str) -> str:
    """
    Generate the target group name for a user.
    Must match the naming scheme used in ensure_instance.py.
    """
    suffix = hashlib.sha1((user_sub + listener_arn).encode()).hexdigest()[:16]
    return f"u-{suffix}"


def _routing_cookie_name(user_sub: str) -> str:
    """
    Generate the routing cookie name for a user.
    Must match the naming scheme used in ensure_instance.py.
    """
    suffix = hashlib.sha1(user_sub.encode()).hexdigest()[:12]
    return f"hp_route_{suffix}"


def lambda_handler(event, context):
    """
    Main handler for user deletion cleanup.
    """
    logger.info("Delete user event: %s", event)
    
    user_sub = _resolve_to_sub(event)
    if not user_sub:
        logger.error("Could not resolve user identifier from event")
        return {"statusCode": 400, "body": "Could not resolve user identifier"}
    
    logger.info("Cleaning up resources for user_sub: %s", user_sub)
    
    # Get the ALB ARN and discover the listener
    alb_arn = os.environ["ALB_ARN"]
    listener_arn = _get_https_listener_arn(alb_arn)

    # ---------- Find & terminate EC2 instances ----------
    instances = ec2.describe_instances(
        Filters=[
            {"Name": "tag:OwnerSub", "Values": [user_sub]},
            {"Name": "tag:Purpose", "Values": ["HousePlannerUser"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopped", "stopping"]},
        ]
    )

    instance_ids = [
        i["InstanceId"]
        for r in instances.get("Reservations", [])
        for i in r.get("Instances", [])
    ]

    if instance_ids:
        logger.info("Terminating instances: %s", instance_ids)
        ec2.terminate_instances(InstanceIds=instance_ids)

        # Wait for instances to fully terminate
        try:
            waiter = ec2.get_waiter("instance_terminated")
            waiter.wait(
                InstanceIds=instance_ids,
                WaiterConfig={"Delay": 5, "MaxAttempts": 40}  # Up to ~3 minutes
            )
            logger.info("Instances terminated successfully")
        except Exception as e:
            logger.warning("Waiter error (continuing anyway): %s", e)
    else:
        logger.info("No EC2 instances found for user_sub: %s", user_sub)

    # ---------- Find & delete ALB listener rules ----------
    # Rules are identified by the routing cookie pattern
    cookie_name = _routing_cookie_name(user_sub)
    rules_deleted = 0
    try:
        paginator = elbv2.get_paginator("describe_rules")
        for page in paginator.paginate(ListenerArn=listener_arn):
            for rule in page.get("Rules", []):
                # Skip the default rule
                if rule.get("IsDefault"):
                    continue
                    
                for cond in rule.get("Conditions", []):
                    if cond.get("Field") == "http-header":
                        http_config = cond.get("HttpHeaderConfig", {})
                        header_name = http_config.get("HttpHeaderName", "").lower()
                        values = http_config.get("Values", [])
                        
                        # Match cookie-based rules (new format)
                        if header_name == "cookie":
                            for v in values:
                                if cookie_name in v:
                                    logger.info("Deleting cookie-based rule: %s", rule["RuleArn"])
                                    elbv2.delete_rule(RuleArn=rule["RuleArn"])
                                    rules_deleted += 1
                                    break
                        
                        # Also match old x-amzn-oidc-identity rules (for backwards compatibility)
                        elif header_name == "x-amzn-oidc-identity":
                            if user_sub in values:
                                logger.info("Deleting legacy rule: %s", rule["RuleArn"])
                                elbv2.delete_rule(RuleArn=rule["RuleArn"])
                                rules_deleted += 1
    except Exception as e:
        logger.error("Error deleting listener rules: %s", e)
    
    logger.info("Deleted %d listener rules", rules_deleted)

    # ---------- Find & delete target group ----------
    tg_name = _tg_name(user_sub, listener_arn)
    logger.info("Looking for target group: %s", tg_name)
    
    try:
        resp = elbv2.describe_target_groups(Names=[tg_name])
        for tg in resp.get("TargetGroups", []):
            logger.info("Deleting target group: %s", tg["TargetGroupArn"])
            elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
    except elbv2.exceptions.TargetGroupNotFoundException:
        logger.info("Target group %s not found (already deleted or never created)", tg_name)
    except Exception as e:
        logger.error("Error deleting target group: %s", e)

    logger.info("Cleanup complete for user_sub: %s", user_sub)
    
    # Return the original event for Cognito triggers
    return event
