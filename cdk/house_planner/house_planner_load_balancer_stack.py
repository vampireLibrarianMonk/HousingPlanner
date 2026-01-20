"""
house_planner_load_balancer_stack.py

Load Balancer stack for House Planner.

Responsibilities:
- Application Load Balancer
- HTTPS Listener with Cognito OIDC authentication
- ACM Certificate for ALB
- Default action: warm-up page (after auth)
- Ensure-instance Lambda for /internal/ensure path
- Lambda target group and listener rule

This stack handles traffic routing, authentication enforcement, and on-demand EC2 provisioning.
"""

from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    SecretValue,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_elasticloadbalancingv2_targets as elbv2_targets,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_iam as iam,
    aws_lambda as _lambda,
)
from constructs import Construct

from common import get_warmup_page_html


class HousePlannerLoadBalancerStack(Stack):
    """
    Load Balancer stack.
    Creates the ALB with OIDC-authenticated HTTPS listener and ensure-instance Lambda.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        alb_security_group: ec2.ISecurityGroup,
        ec2_security_group: ec2.ISecurityGroup,
        user_pool_id: str,
        user_pool_client_id: str,
        user_pool_domain_name: str,
        client_secret_arn: str,
        launch_template_id: str,
        hosted_zone_name: str,
        app_domain_name: str,
        **kwargs,
    ) -> None:
        """
        :param vpc: VPC for ALB placement
        :param alb_security_group: Security group for ALB
        :param ec2_security_group: Security group for EC2 instances
        :param user_pool_id: Cognito user pool ID
        :param user_pool_client_id: Cognito client ID
        :param user_pool_domain_name: Cognito domain prefix
        :param client_secret_arn: ARN of the client secret in Secrets Manager
        :param launch_template_id: EC2 launch template ID
        :param hosted_zone_name: Base hosted zone (e.g. housing-planner.com)
        :param app_domain_name: Full app domain (e.g. app.housing-planner.com)
        """
        super().__init__(scope, construct_id, **kwargs)

        # --------------------------------------------------
        # Application Load Balancer
        # --------------------------------------------------
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "HousePlannerALB",
            load_balancer_name="HousePlannerALB",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_security_group,
        )

        # --------------------------------------------------
        # ACM Certificate for ALB
        # --------------------------------------------------
        hosted_zone = route53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=hosted_zone_name,
        )

        certificate = acm.Certificate(
            self,
            "AlbCertificate",
            domain_name=app_domain_name,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        # --------------------------------------------------
        # Ensure-Instance Lambda Function
        # --------------------------------------------------
        # OIDC config needed for Lambda to create user-specific rules with auth
        oidc_issuer = f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool_id}"
        oidc_auth_endpoint = f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/authorize"
        oidc_token_endpoint = f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/token"
        oidc_user_info_endpoint = f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/userInfo"

        self.ensure_lambda = _lambda.Function(
            self,
            "EnsureInstanceLambda",
            function_name="HousePlannerEnsureInstance",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="ensure_instance.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(120),
            memory_size=256,
            description="Provisions or starts EC2 instance for authenticated user",
            environment={
                "LAUNCH_TEMPLATE_ID": launch_template_id,
                "SUBNET_ID": vpc.public_subnets[0].subnet_id,
                "VPC_ID": vpc.vpc_id,
                "ALB_ARN": self.alb.load_balancer_arn,
                "EC2_SECURITY_GROUP_ID": ec2_security_group.security_group_id,
                # OIDC config for creating authenticated user rules
                "OIDC_ISSUER": oidc_issuer,
                "OIDC_AUTH_ENDPOINT": oidc_auth_endpoint,
                "OIDC_TOKEN_ENDPOINT": oidc_token_endpoint,
                "OIDC_USER_INFO_ENDPOINT": oidc_user_info_endpoint,
                "OIDC_CLIENT_ID": user_pool_client_id,
                "OIDC_CLIENT_SECRET_ARN": client_secret_arn,
            },
        )

        # Lambda IAM Permissions
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
                    "elasticloadbalancing:ModifyTargetGroupAttributes",
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

        # Allow Lambda to read the client secret for OIDC auth rules
        self.ensure_lambda.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadClientSecret",
                actions=["secretsmanager:GetSecretValue"],
                resources=[client_secret_arn],
            )
        )

        # --------------------------------------------------
        # Lambda Target Group
        # --------------------------------------------------
        ensure_target_group = elbv2.ApplicationTargetGroup(
            self,
            "EnsureLambdaTargetGroup",
            target_group_name="HousePlannerEnsureTG",
            target_type=elbv2.TargetType.LAMBDA,
            targets=[elbv2_targets.LambdaTarget(self.ensure_lambda)],
        )
        ensure_target_group.configure_health_check(enabled=False)

        # --------------------------------------------------
        # HTTPS Listener with Cognito OIDC Authentication
        # --------------------------------------------------
        self.https_listener = self.alb.add_listener(
            "HttpsListener",
            port=443,
            certificates=[elbv2.ListenerCertificate(certificate.certificate_arn)],
            default_action=elbv2.ListenerAction.authenticate_oidc(
                issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool_id}",
                authorization_endpoint=f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/authorize",
                token_endpoint=f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/token",
                user_info_endpoint=f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/userInfo",
                client_id=user_pool_client_id,
                client_secret=SecretValue.secrets_manager(client_secret_arn),
                scope="openid email",
                next=elbv2.ListenerAction.fixed_response(
                    status_code=200,
                    content_type="text/html",
                    message_body=self._warm_up_page(),
                ),
            ),
        )

        # --------------------------------------------------
        # Listener Rule for /internal/ensure
        # --------------------------------------------------
        self.https_listener.add_action(
            "EnsureRoute",
            priority=1,
            conditions=[
                elbv2.ListenerCondition.path_patterns(["/internal/ensure"])
            ],
            action=elbv2.ListenerAction.authenticate_oidc(
                issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool_id}",
                authorization_endpoint=f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/authorize",
                token_endpoint=f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/token",
                user_info_endpoint=f"https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/oauth2/userInfo",
                client_id=user_pool_client_id,
                client_secret=SecretValue.secrets_manager(client_secret_arn),
                scope="openid email",
                next=elbv2.ListenerAction.forward([ensure_target_group]),
            ),
        )

        # --------------------------------------------------
        # Expose values for other stacks
        # --------------------------------------------------
        self.alb_dns_name = self.alb.load_balancer_dns_name
        self.alb_arn = self.alb.load_balancer_arn
        self.listener_arn = self.https_listener.listener_arn

        # --------------------------------------------------
        # Outputs
        # --------------------------------------------------
        CfnOutput(self, "AlbDnsName", value=self.alb_dns_name)
        CfnOutput(self, "AlbArn", value=self.alb_arn)
        CfnOutput(self, "ListenerArn", value=self.listener_arn)
        CfnOutput(self, "EnsureLambdaArn", value=self.ensure_lambda.function_arn)

    def _warm_up_page(self) -> str:
        """
        Returns the HTML for the warm-up page.
        Uses the shared function from common.py for consistency.
        """
        return get_warmup_page_html()
