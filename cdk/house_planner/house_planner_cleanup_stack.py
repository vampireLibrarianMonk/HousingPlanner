"""
house_planner_cleanup_stack.py

Cleanup stack for House Planner.

Responsibilities:
- React to Cognito AdminDeleteUser events via EventBridge
- Terminate the user's EC2 instance
- Remove per-user ALB listener rules and target groups

This stack intentionally does NOT attach Cognito triggers
to avoid circular dependencies with other stacks.
"""

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_events as events,
    aws_events_targets as targets,
)
from constructs import Construct


class HousePlannerCleanupStack(Stack):
    """
    Cleanup stack.
    Handles resource cleanup when users are deleted from Cognito.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alb_arn: str,
        user_pool_id: str,
        storage_bucket_prefix_param: str,
        **kwargs,
    ) -> None:
        """
        :param alb_arn: ARN of the Application Load Balancer
        :param user_pool_id: Cognito user pool ID
        """
        super().__init__(scope, construct_id, **kwargs)

        bucket_prefix = f"houseplanner-{self.account}"
        bucket_arn_pattern = f"arn:aws:s3:::{bucket_prefix}-*"

        # --------------------------------------------------
        # Delete-user cleanup Lambda
        # --------------------------------------------------
        self.delete_lambda = _lambda.Function(
            self,
            "DeleteUserResourcesLambda",
            function_name="HousePlannerDeleteUserResources",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="delete_instance.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(120),
            memory_size=256,
            description="Cleans up EC2 instance and ALB resources when user is deleted",
            environment={
                "ALB_ARN": alb_arn,
                "USER_POOL_ID": user_pool_id,
                "STORAGE_BUCKET_PREFIX_PARAM": storage_bucket_prefix_param,
            },
        )

        # --------------------------------------------------
        # Lambda IAM Permissions
        # --------------------------------------------------
        self.delete_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="EC2Cleanup",
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:TerminateInstances",
                ],
                resources=["*"],
            )
        )

        self.delete_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="ALBCleanup",
                actions=[
                    "elasticloadbalancing:DescribeListeners",
                    "elasticloadbalancing:DescribeRules",
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DeleteRule",
                    "elasticloadbalancing:DeleteTargetGroup",
                    "elasticloadbalancing:DeregisterTargets",
                ],
                resources=["*"],
            )
        )

        # Permission to look up user details from Cognito
        self.delete_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="CognitoUserLookup",
                actions=["cognito-idp:AdminGetUser"],
                resources=[f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/{user_pool_id}"],
            )
        )

        self.delete_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="StorageBucketPrefixLookup",
                actions=["ssm:GetParameter"],
                resources=[storage_bucket_prefix_param],
            )
        )

        self.delete_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="S3UserBucketCleanup",
                actions=[
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                    "s3:DeleteBucket",
                ],
                resources=[bucket_arn_pattern, f"{bucket_arn_pattern}/*"],
            )
        )

        # --------------------------------------------------
        # EventBridge rule: Cognito AdminDeleteUser
        # --------------------------------------------------
        # This rule triggers when a user is deleted via AdminDeleteUser API
        delete_rule = events.Rule(
            self,
            "CognitoAdminDeleteUserRule",
            rule_name="HousePlannerUserDeletedRule",
            description="Triggers cleanup when a Cognito user is deleted",
            event_pattern=events.EventPattern(
                source=["aws.cognito-idp"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventName": ["AdminDeleteUser"],
                    "requestParameters": {
                        "userPoolId": [user_pool_id],
                    },
                },
            ),
        )

        delete_rule.add_target(targets.LambdaFunction(self.delete_lambda))

        # --------------------------------------------------
        # Outputs
        # --------------------------------------------------
        CfnOutput(self, "DeleteLambdaArn", value=self.delete_lambda.function_arn)
        CfnOutput(self, "DeleteEventRuleArn", value=delete_rule.rule_arn)
