"""
house_planner_network_stack.py

Network foundation stack for House Planner.

Responsibilities:
- VPC with public subnets only (no NAT to save costs)
- VPC Gateway Endpoint for S3
- VPC Interface Endpoint for Secrets Manager
- Security groups for ALB and EC2 instances

This stack is the foundation - rarely changes after initial deployment.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    aws_ec2 as ec2,
)
from constructs import Construct


class HousePlannerNetworkStack(Stack):
    """
    Network foundation stack.
    Creates VPC, subnets, and VPC endpoints required by other stacks.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --------------------------------------------------
        # VPC (public-only, no NAT to minimize costs)
        # --------------------------------------------------
        self.vpc = ec2.Vpc(
            self,
            "HousePlannerVPC",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        # --------------------------------------------------
        # VPC Endpoints (required for EC2 bootstrap without NAT)
        # --------------------------------------------------
        self.vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        self.vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        )

        # --------------------------------------------------
        # Security Group: ALB
        # --------------------------------------------------
        self.alb_security_group = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=self.vpc,
            description="Security group for House Planner ALB",
            allow_all_outbound=True,
        )

        # Allow HTTPS from anywhere (CloudFront will connect here)
        self.alb_security_group.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(443),
            "Allow HTTPS from CloudFront",
        )

        # --------------------------------------------------
        # Security Group: EC2 Instances
        # --------------------------------------------------
        self.ec2_security_group = ec2.SecurityGroup(
            self,
            "Ec2SecurityGroup",
            vpc=self.vpc,
            description="Security group for House Planner EC2 instances",
            allow_all_outbound=True,
        )

        # Allow HTTP from ALB only (instances serve on port 80 behind nginx)
        self.ec2_security_group.add_ingress_rule(
            self.alb_security_group,
            ec2.Port.tcp(80),
            "Allow HTTP from ALB",
        )

        # --------------------------------------------------
        # Outputs for cross-stack references
        # --------------------------------------------------
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "PublicSubnet1Id", value=self.vpc.public_subnets[0].subnet_id)
        CfnOutput(self, "PublicSubnet2Id", value=self.vpc.public_subnets[1].subnet_id)
        CfnOutput(self, "AlbSecurityGroupId", value=self.alb_security_group.security_group_id)
        CfnOutput(self, "Ec2SecurityGroupId", value=self.ec2_security_group.security_group_id)
