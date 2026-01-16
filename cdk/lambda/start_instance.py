import boto3
import os

ec2 = boto3.client("ec2")
INSTANCE_ID = os.environ["INSTANCE_ID"]

# Cognito group required to start the instance
ADMIN_GROUP = os.environ.get("ADMIN_GROUP", "admin")


def lambda_handler(event, context):
    """
    Requires Cognito auth via API Gateway Cognito User Pools Authorizer.
    Enforces that the caller is in the ADMIN_GROUP (default: 'admin').
    """
    claims = (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )

    groups_raw = claims.get("cognito:groups", "") or ""
    # API Gateway commonly provides this as a comma-separated string
    groups = {g.strip() for g in groups_raw.split(",") if g.strip()}

    if ADMIN_GROUP not in groups:
        return {
            "statusCode": 403,
            "body": f"Forbidden: requires Cognito group '{ADMIN_GROUP}'",
        }

    ec2.start_instances(InstanceIds=[INSTANCE_ID])
    return {
        "statusCode": 200,
        "body": "Instance starting",
    }
