"""
house_planner_provisioning_stack.py

Provisioning stack for House Planner.

Responsibilities:
- Ensure-instance Lambda function
- Lambda target group for ALB
- Listener rule for /internal/ensure path (using L1 CfnListenerRule to avoid cross-stack issues)
- IAM permissions for Lambda to manage EC2 and ALB

This stack handles the on-demand EC2 provisioning triggered by the warm-up page.
"""

from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
)
from constructs import Construct


class HousePlannerProvisioningStack(Stack):
    """
    Provisioning stack.
    Creates the Lambda that provisions EC2 instances on-demand.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        alb_arn: str,
        listener_arn: str,
        oidc_config: dict,
        launch_template_id: str,
        ec2_security_group: ec2.ISecurityGroup,
        **kwargs,
    ) -> None:
        """
        :param vpc: VPC for Lambda and EC2
        :param alb_arn: ARN of the Application Load Balancer (string to avoid dependency)
        :param listener_arn: ARN of the HTTPS listener (string to avoid dependency)
        :param oidc_config: OIDC configuration dict from LoadBalancerStack
        :param launch_template_id: EC2 launch template ID
        :param ec2_security_group: Security group for EC2 instances
        """
        super().__init__(scope, construct_id, **kwargs)

        # --------------------------------------------------
        # Ensure-Instance Lambda Function
        # --------------------------------------------------
        self.ensure_lambda = _lambda.Function(
            self,
            "EnsureInstanceLambda",
            function_name="HousePlannerEnsureInstance",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="ensure_instance.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(60),
            memory_size=256,
            description="Provisions or starts EC2 instance for authenticated user",
            environment={
                "LAUNCH_TEMPLATE_ID": launch_template_id,
                "SUBNET_ID": vpc.public_subnets[0].subnet_id,
                "VPC_ID": vpc.vpc_id,
                "ALB_ARN": alb_arn,
                "EC2_SECURITY_GROUP_ID": ec2_security_group.security_group_id,
            },
        )

        # --------------------------------------------------
        # Lambda IAM Permissions
        # --------------------------------------------------
        self.ensure_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="EC2Management",
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:RunInstances",
                    "ec2:StartInstances",
                    "ec2:CreateTags",
                ],
                resources=["*"],
            )
        )

        self.ensure_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="ALBTargetGroupManagement",
                actions=[
                    "elasticloadbalancing:DescribeListeners",
                    "elasticloadbalancing:DescribeRules",
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DescribeTargetHealth",
                    "elasticloadbalancing:CreateTargetGroup",
                    "elasticloadbalancing:DeleteTargetGroup",
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:DeregisterTargets",
                    "elasticloadbalancing:CreateRule",
                    "elasticloadbalancing:DeleteRule",
                    "elasticloadbalancing:ModifyRule",
                    "elasticloadbalancing:AddTags",
                ],
                resources=["*"],
            )
        )

        # Allow IAM pass role for EC2 instance profile
        self.ensure_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassRoleForEC2",
                actions=["iam:PassRole"],
                resources=[f"arn:aws:iam::{self.account}:role/HousePlannerEC2Role"],
            )
        )

        # --------------------------------------------------
        # Lambda Target Group
        # --------------------------------------------------
        self.lambda_target_group = elbv2.ApplicationTargetGroup(
            self,
            "EnsureLambdaTargetGroup",
            target_group_name="HousePlannerEnsureTG",
            target_type=elbv2.TargetType.LAMBDA,
            targets=[elbv2_targets.LambdaTarget(self.ensure_lambda)],
        )

        # Lambda targets don't need health checks
        self.lambda_target_group.configure_health_check(enabled=False)

        # --------------------------------------------------
        # Listener Rule for /internal/ensure (using L1 CfnListenerRule)
        # --------------------------------------------------
        # Using L1 construct to avoid cross-stack dependency issues
        # This rule:
        # 1. Matches path /internal/ensure
        # 2. Authenticates via Cognito OIDC
        # 3. Forwards to the Lambda target group
        elbv2.CfnListenerRule(
            self,
            "EnsureListenerRule",
            listener_arn=listener_arn,
            priority=1,
            conditions=[
                elbv2.CfnListenerRule.RuleConditionProperty(
                    field="path-pattern",
                    path_pattern_config=elbv2.CfnListenerRule.PathPatternConfigProperty(
                        values=["/internal/ensure"]
                    ),
                )
            ],
            actions=[
                # Action 1: Authenticate with OIDC
                elbv2.CfnListenerRule.ActionProperty(
                    type="authenticate-oidc",
                    order=1,
                    authenticate_oidc_config=elbv2.CfnListenerRule.AuthenticateOidcConfigProperty(
                        issuer=oidc_config["issuer"],
                        authorization_endpoint=oidc_config["authorization_endpoint"],
                        token_endpoint=oidc_config["token_endpoint"],
                        user_info_endpoint=oidc_config["user_info_endpoint"],
                        client_id=oidc_config["client_id"],
                        client_secret=oidc_config["client_secret"],
                        scope="openid email",
                    ),
                ),
                # Action 2: Forward to Lambda target group
                elbv2.CfnListenerRule.ActionProperty(
                    type="forward",
                    order=2,
                    target_group_arn=self.lambda_target_group.target_group_arn,
                ),
            ],
        )

        # --------------------------------------------------
        # Outputs
        # --------------------------------------------------
        CfnOutput(self, "EnsureLambdaArn", value=self.ensure_lambda.function_arn)
        CfnOutput(self, "LambdaTargetGroupArn", value=self.lambda_target_group.target_group_arn)
