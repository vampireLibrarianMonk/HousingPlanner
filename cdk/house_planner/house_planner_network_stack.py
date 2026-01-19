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

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        ssh_cidr: str | None = None,
        cloudfront_pl_id: str | None = None,
        **kwargs,
    ) -> None:
        """
        :param ssh_cidr: Optional CIDR for SSH access to EC2 instances (e.g., "1.2.3.4/32")
        :param cloudfront_pl_id: Optional CloudFront prefix list ID for restricting ALB access
        """
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

        # Allow HTTPS from CloudFront
        # If prefix list is provided, restrict to CloudFront IPs only (more secure)
        # Otherwise allow from anywhere (less secure but works without prefix list)
        if cloudfront_pl_id:
            self.alb_security_group.add_ingress_rule(
                ec2.Peer.prefix_list(cloudfront_pl_id),
                ec2.Port.tcp(443),
                "Allow HTTPS from CloudFront (prefix list)",
            )
        else:
            self.alb_security_group.add_ingress_rule(
                ec2.Peer.any_ipv4(),
                ec2.Port.tcp(443),
                "Allow HTTPS from CloudFront (any IPv4)",
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

        # Allow SSH from specified CIDR (optional - for debugging/admin access)
        if ssh_cidr:
            self.ec2_security_group.add_ingress_rule(
                ec2.Peer.ipv4(ssh_cidr if "/" in ssh_cidr else f"{ssh_cidr}/32"),
                ec2.Port.tcp(22),
                f"Allow SSH from {ssh_cidr}",
            )

        # Allow SSH from EC2 Instance Connect for us-east-1
        # This enables browser-based SSH via AWS Console
        # See: https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-connect-prerequisites.html
        self.ec2_security_group.add_ingress_rule(
            ec2.Peer.ipv4("18.206.107.24/29"),
            ec2.Port.tcp(22),
            "Allow SSH from EC2 Instance Connect (us-east-1)",
        )

        # --------------------------------------------------
        # Outputs for cross-stack references
        # --------------------------------------------------
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "PublicSubnet1Id", value=self.vpc.public_subnets[0].subnet_id)
        CfnOutput(self, "PublicSubnet2Id", value=self.vpc.public_subnets[1].subnet_id)
        CfnOutput(self, "AlbSecurityGroupId", value=self.alb_security_group.security_group_id)
        CfnOutput(self, "Ec2SecurityGroupId", value=self.ec2_security_group.security_group_id)
