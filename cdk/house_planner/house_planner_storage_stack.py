"""
house_planner_storage_stack.py

Storage stack for House Planner.

Responsibilities:
- Define naming convention for per-user S3 buckets
- Store naming prefix in SSM for cross-stack reference
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    aws_ssm as ssm,
)
from constructs import Construct


class HousePlannerStorageStack(Stack):
    """
    Storage stack for per-user S3 buckets.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_tag: str = "HousePlanner",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Prefix is shared across per-user buckets: <prefix>-<user_sub>
        self.bucket_prefix = f"{app_tag.lower()}-{self.account}"

        self.prefix_param = ssm.StringParameter(
            self,
            "StorageBucketPrefix",
            parameter_name="/houseplanner/storage/bucket_prefix",
            string_value=self.bucket_prefix,
        )

        CfnOutput(self, "StorageBucketPrefixOutput", value=self.bucket_prefix)