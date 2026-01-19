"""
house_planner_cloud_front_stack.py

CloudFront Edge stack for House Planner.

Responsibilities:
- Own the public edge (CloudFront distribution)
- Terminate TLS at the edge (ACM certificate in us-east-1)
- Route traffic to the ALB origin
- Manage DNS (Route53 alias)
- Remain STATIC (no user lifecycle coupling)

This stack MUST NOT:
- Know about Cognito users
- Know about EC2 instances
- Be modified by Lambdas

IMPORTANT: This stack MUST be deployed in us-east-1 for CloudFront.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Duration,
    Environment,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_route53 as route53,
    aws_route53_targets as route53_targets,
)
from constructs import Construct
import os


class HousePlannerCloudFrontStack(Stack):
    """
    CloudFront edge stack.
    Creates the CDN distribution that fronts the ALB.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alb_dns_name: str,
        hosted_zone_name: str,
        app_domain_name: str,
        **kwargs,
    ) -> None:
        """
        :param alb_dns_name: DNS name of the ALB
        :param hosted_zone_name: Base hosted zone (e.g. housing-planner.com)
        :param app_domain_name: Full app domain (e.g. app.housing-planner.com)
        """
        # Force us-east-1 for CloudFront + ACM
        super().__init__(
            scope,
            construct_id,
            env=Environment(
                account=os.environ["CDK_DEFAULT_ACCOUNT"],
                region="us-east-1",
            ),
            **kwargs,
        )

        # --------------------------------------------------
        # Route53 hosted zone lookup
        # --------------------------------------------------
        hosted_zone = route53.HostedZone.from_lookup(
            self,
            "HousePlannerHostedZone",
            domain_name=hosted_zone_name,
        )

        # --------------------------------------------------
        # ACM certificate (MUST be us-east-1 for CloudFront)
        # --------------------------------------------------
        certificate = acm.Certificate(
            self,
            "HousePlannerCloudFrontCert",
            domain_name=app_domain_name,
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        # --------------------------------------------------
        # CloudFront origin (ALB)
        # --------------------------------------------------
        # Configure for WebSocket support with longer timeouts
        alb_origin = origins.HttpOrigin(
            domain_name=alb_dns_name,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            # Longer timeouts for WebSocket connections (Streamlit uses WebSockets)
            keepalive_timeout=Duration.seconds(60),
            read_timeout=Duration.seconds(60),
        )

        # --------------------------------------------------
        # CloudFront Distribution
        # --------------------------------------------------
        # Use managed policies for WebSocket support:
        # - CACHING_DISABLED: No caching, all requests go to origin
        # - ALL_VIEWER: Forward ALL headers (including WebSocket: Upgrade, Connection, Sec-WebSocket-*)
        distribution = cloudfront.Distribution(
            self,
            "HousePlannerDistribution",
            comment="House Planner CDN with WebSocket support",
            domain_names=[app_domain_name],
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=alb_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                # Use managed policies for proper WebSocket support
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            ),
        )

        # --------------------------------------------------
        # Route53 alias: app.<domain> → CloudFront
        # --------------------------------------------------
        record_name = app_domain_name.replace(f".{hosted_zone_name}", "")
        
        route53.ARecord(
            self,
            "HousePlannerCloudFrontAlias",
            zone=hosted_zone,
            record_name=record_name,
            target=route53.RecordTarget.from_alias(
                route53_targets.CloudFrontTarget(distribution)
            ),
        )

        # --------------------------------------------------
        # Outputs
        # --------------------------------------------------
        CfnOutput(
            self,
            "CloudFrontDistributionId",
            value=distribution.distribution_id,
        )

        CfnOutput(
            self,
            "CloudFrontDomainName",
            value=distribution.domain_name,
        )

        CfnOutput(
            self,
            "HousePlannerAppUrl",
            value=f"https://{app_domain_name}",
        )
