from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_ec2 as ec2,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_route53 as r53,
    aws_route53_targets as r53_targets,
    aws_s3_assets as s3_assets
)
from constructs import Construct


class HousePlannerStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        # =========================
        # CONFIG: set these values
        # =========================
        domain_name = "planner.yourdomain.com"   # e.g. planner.example.com
        hosted_zone_name = "yourdomain.com"      # e.g. example.com

        # --- VPC (minimal, custom) ---
        vpc = ec2.Vpc(
            self,
            "HousePlannerVPC",
            max_azs=1,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        # --- EC2 Instance ---
        idle_script_asset = s3_assets.Asset(
            self,
            "IdleShutdownScript",
            path="scripts/idle_shutdown.sh",
        )

        idle_service_asset = s3_assets.Asset(
            self,
            "IdleShutdownService",
            path="service/idle-shutdown.service",
        )

        instance = ec2.Instance(
            self,
            "HousePlannerEC2",
            instance_type=ec2.InstanceType("t4g.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            vpc=vpc,
        )

        # --- User data: install idle shutdown ---
        instance.user_data.add_commands(
            "#!/bin/bash",
            "set -euxo pipefail",

            "dnf install -y awscli",

            "# Fetch idle shutdown script",
            f"aws s3 cp {idle_script_asset.s3_object_url} /usr/local/bin/idle_shutdown.sh",
            "chmod +x /usr/local/bin/idle_shutdown.sh",

            "# Fetch systemd service",
            f"aws s3 cp {idle_service_asset.s3_object_url} /etc/systemd/system/idle-shutdown.service",

            "# Enable and start service",
            "systemctl daemon-reexec",
            "systemctl daemon-reload",
            "systemctl enable idle-shutdown.service",
            "systemctl start idle-shutdown.service",
        )

        idle_script_asset.grant_read(instance.role)
        idle_service_asset.grant_read(instance.role)

        # Start STOPPED
        instance.instance.add_property_override(
            "InstanceInitiatedShutdownBehavior", "stop"
        )

        instance_env = {
            "INSTANCE_ID": instance.instance_id,
        }

        instance.user_data.add_commands(
            "yum install -y awscli",
            "",
            "# Fetch idle shutdown script",
            f"aws s3 cp {idle_script_asset.s3_object_url} /usr/local/bin/idle_shutdown.sh",
            "chmod +x /usr/local/bin/idle_shutdown.sh",
            "",
            "# Fetch systemd service",
            f"aws s3 cp {idle_service_asset.s3_object_url} /etc/systemd/system/idle-shutdown.service",
            "",
            "# Enable and start service",
            "systemctl daemon-reexec",
            "systemctl daemon-reload",
            "systemctl enable idle-shutdown.service",
            "systemctl start idle-shutdown.service",
        )

        # --- Lambda: Start EC2 ---
        start_lambda = _lambda.Function(
            self,
            "StartInstanceLambda",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="start_instance.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            environment=instance_env,
            timeout=Duration.seconds(10),
        )

        start_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ec2:StartInstances"],
                resources=[
                    f"arn:aws:ec2:{self.region}:{self.account}:instance/{instance.instance_id}"
                ],
            )
        )

        start_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ec2:DescribeInstances"],
                resources=["*"],  # required by AWS
            )
        )

        # --- Lambda: Status Page ---
        status_lambda = _lambda.Function(
            self,
            "StatusPageLambda",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="status_page.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            environment=instance_env,
            timeout=Duration.seconds(10),
        )

        status_lambda.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ec2:DescribeInstances"],
                resources=["*"],
            )
        )

        # --- API Gateway ---
        api = apigw.RestApi(self, "HousePlannerAPI")

        api.root.add_method(
            "GET",
            apigw.LambdaIntegration(status_lambda),
        )

        start = api.root.add_resource("start")
        start.add_method(
            "POST",
            apigw.LambdaIntegration(start_lambda),
        )

        # ==========================================================
        # (3) HTTPS: CloudFront + ACM (cert MUST be in us-east-1)
        # ==========================================================
        zone = r53.HostedZone.from_lookup(
            self,
            "HostedZone",
            domain_name=hosted_zone_name,
        )

        cert = acm.DnsValidatedCertificate(
            self,
            "PlannerCert",
            domain_name=domain_name,
            hosted_zone=zone,
            region="us-east-1",  # CloudFront requires the cert in us-east-1
        )

        distribution = cf.Distribution(
            self,
            "PlannerDistribution",
            default_behavior=cf.BehaviorOptions(
                origin=origins.RestApiOrigin(api),
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cf.AllowedMethods.ALLOW_ALL,
                cache_policy=cf.CachePolicy.CACHING_DISABLED,
            ),
            domain_names=[domain_name],
            certificate=cert,
        )

        # ==========================================================
        # (2) Route53: planner.<domain> -> CloudFront (DNS alias)
        # ==========================================================
        r53.ARecord(
            self,
            "PlannerAlias",
            zone=zone,
            record_name=domain_name.replace(f".{hosted_zone_name}", ""),
            target=r53.RecordTarget.from_alias(
                r53_targets.CloudFrontTarget(distribution)
            ),
        )

        # Helpful outputs
        CfnOutput(self, "ApiGatewayUrl", value=api.url)
        CfnOutput(self, "CloudFrontUrl", value=f"https://{distribution.domain_name}")
        CfnOutput(self, "CustomDomainUrl", value=f"https://{domain_name}")
