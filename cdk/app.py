#!/usr/bin/env python3
"""
House Planner CDK Application

Stack Architecture (6 Stacks):
    NetworkStack - VPC, Subnets, Security Groups
        ↓
    CognitoStack - User Pool, Client, Domain
        ↓
    ComputeStack - EC2 Launch Template, Instance Role
        ↓
    LoadBalancerStack - ALB, HTTPS Listener, Ensure Lambda, OIDC Auth
        ↓
    CloudFrontStack - CDN Distribution (us-east-1)
        ↓
    CleanupStack - EventBridge cleanup on user deletion

All stacks deploy to us-east-1.
CloudFrontStack requires us-east-1 for ACM certificate with CloudFront.
"""

import os
import aws_cdk as cdk

from house_planner.house_planner_network_stack import HousePlannerNetworkStack
from house_planner.house_planner_cognito_stack import HousePlannerCognitoStack
from house_planner.house_planner_compute_stack import HousePlannerComputeStack
from house_planner.house_planner_load_balancer_stack import HousePlannerLoadBalancerStack
from house_planner.house_planner_cloud_front_stack import HousePlannerCloudFrontStack
from house_planner.house_planner_cleanup_stack import HousePlannerCleanupStack

# --------------------------------------------------
# Configuration
# --------------------------------------------------
HOSTED_ZONE_NAME = "housing-planner.com"
APP_DOMAIN_NAME = "app.housing-planner.com"

app = cdk.App()

# --------------------------------------------------
# Context Variables (optional)
# --------------------------------------------------
# Pass via: cdk deploy -c ssh_cidr="1.2.3.4/32" -c cloudfront_pl_id="pl-xxxxxxxx"
ssh_cidr = app.node.try_get_context("ssh_cidr")
cloudfront_pl_id = app.node.try_get_context("cloudfront_pl_id")

# Default environment for all stacks
default_env = cdk.Environment(
    account=os.environ["CDK_DEFAULT_ACCOUNT"],
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

# --------------------------------------------------
# Stack 1: Network Foundation
# --------------------------------------------------
network_stack = HousePlannerNetworkStack(
    app,
    "HousePlannerNetworkStack",
    env=default_env,
    ssh_cidr=ssh_cidr,
    cloudfront_pl_id=cloudfront_pl_id,
    description="VPC, subnets, and security groups for House Planner",
)

# --------------------------------------------------
# Stack 2: Cognito Identity
# --------------------------------------------------
cognito_stack = HousePlannerCognitoStack(
    app,
    "HousePlannerCognitoStack",
    env=default_env,
    app_domain_name=APP_DOMAIN_NAME,
    description="Cognito user pool and authentication for House Planner",
)

# --------------------------------------------------
# Stack 3: Compute Template
# --------------------------------------------------
compute_stack = HousePlannerComputeStack(
    app,
    "HousePlannerComputeStack",
    env=default_env,
    vpc=network_stack.vpc,
    ec2_security_group=network_stack.ec2_security_group,
    user_pool_id=cognito_stack.user_pool_id,
    user_pool_client_id=cognito_stack.user_pool_client_id,
    user_pool_domain_name=cognito_stack.user_pool_domain_name,
    app_domain_name=APP_DOMAIN_NAME,
    description="EC2 launch template and instance role for House Planner",
)
compute_stack.add_dependency(network_stack)
compute_stack.add_dependency(cognito_stack)

# --------------------------------------------------
# Stack 4: Load Balancer + Provisioning
# --------------------------------------------------
# This stack includes:
# - ALB with HTTPS listener
# - Cognito OIDC authentication
# - Ensure-instance Lambda
# - Lambda target group and /internal/ensure rule
load_balancer_stack = HousePlannerLoadBalancerStack(
    app,
    "HousePlannerLoadBalancerStack",
    env=default_env,
    vpc=network_stack.vpc,
    alb_security_group=network_stack.alb_security_group,
    ec2_security_group=network_stack.ec2_security_group,
    user_pool_id=cognito_stack.user_pool_id,
    user_pool_client_id=cognito_stack.user_pool_client_id,
    user_pool_domain_name=cognito_stack.user_pool_domain_name,
    client_secret_arn=cognito_stack.client_secret_arn,
    launch_template_id=compute_stack.launch_template_id,
    hosted_zone_name=HOSTED_ZONE_NAME,
    app_domain_name=APP_DOMAIN_NAME,
    description="ALB with Cognito OIDC and ensure-instance Lambda for House Planner",
)
load_balancer_stack.add_dependency(network_stack)
load_balancer_stack.add_dependency(cognito_stack)
load_balancer_stack.add_dependency(compute_stack)

# --------------------------------------------------
# Stack 5: CloudFront Edge (us-east-1)
# --------------------------------------------------
cloudfront_stack = HousePlannerCloudFrontStack(
    app,
    "HousePlannerCloudFrontStack",
    alb_dns_name=load_balancer_stack.alb_dns_name,
    hosted_zone_name=HOSTED_ZONE_NAME,
    app_domain_name=APP_DOMAIN_NAME,
    description="CloudFront CDN distribution for House Planner",
)
cloudfront_stack.add_dependency(load_balancer_stack)

# --------------------------------------------------
# Stack 6: Cleanup (User Deletion)
# --------------------------------------------------
cleanup_stack = HousePlannerCleanupStack(
    app,
    "HousePlannerCleanupStack",
    env=default_env,
    alb_arn=load_balancer_stack.alb_arn,
    user_pool_id=cognito_stack.user_pool_id,
    description="EventBridge-triggered cleanup when users are deleted",
)
cleanup_stack.add_dependency(cognito_stack)
cleanup_stack.add_dependency(load_balancer_stack)

# --------------------------------------------------
# Synthesize
# --------------------------------------------------
app.synth()
