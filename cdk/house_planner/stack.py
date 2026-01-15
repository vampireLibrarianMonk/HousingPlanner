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
        hosted_zone_name = "housing-planner.com"
        domain_name = "app.housing-planner.com"

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

        ssh_cidr = self.node.try_get_context("ssh_cidr")
        if not ssh_cidr:
            raise ValueError("Missing required context value: ssh_cidr")

        sg = ec2.SecurityGroup(
            self,
            "HousePlannerEC2SG",
            vpc=vpc,
            description="House Planner EC2 security group",
            allow_all_outbound=True,
        )

        sg.add_ingress_rule(
            ec2.Peer.ipv4(ssh_cidr),
            ec2.Port.tcp(22),
            "SSH access from operator IP",
        )

        sg.add_ingress_rule(
            ec2.Peer.ipv4(ssh_cidr),
            ec2.Port.tcp(8501),
            "Streamlit access from operator IP",
        )

        instance = ec2.Instance(
            self,
            "HousePlannerEC2",
            vpc=vpc,
            instance_type=ec2.InstanceType("t4g.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            security_group=sg,
            key_name="houseplanner-key",
        )

        # --- User data: idle shutdown + HousingPlanner bootstrap (SSH via key pair) ---
        instance.user_data.add_commands(
            "#!/bin/bash",
            "set -euxo pipefail",

            "echo '===== [BOOT] Cloud-init starting ====='",

            # ------------------------------------------------------------
            # Ensure ec2-user exists and owns its home
            # ------------------------------------------------------------
            "id ec2-user || useradd -m ec2-user",
            "mkdir -p /home/ec2-user",
            "chown -R ec2-user:ec2-user /home/ec2-user",
            "chmod 755 /home/ec2-user",

            # ------------------------------------------------------------
            # SSH sanity (key-based SSH only)
            # ------------------------------------------------------------
            "echo '[CHECK] Ensuring sshd is running'",
            "systemctl enable sshd",
            "systemctl restart sshd",
            "systemctl is-active sshd || (echo '[FATAL] sshd not running' && exit 1)",

            # ------------------------------------------------------------
            # Idle shutdown (root-owned system service)
            # ------------------------------------------------------------
            "echo '[CHECK] Installing idle shutdown script'",
            f"aws s3 cp {idle_script_asset.s3_object_url} /usr/local/bin/idle_shutdown.sh",
            "chmod +x /usr/local/bin/idle_shutdown.sh",

            "echo '[CHECK] Installing idle shutdown systemd service'",
            f"aws s3 cp {idle_service_asset.s3_object_url} /etc/systemd/system/idle-shutdown.service",

            "systemctl daemon-reexec",
            "systemctl daemon-reload",
            "systemctl enable idle-shutdown.service",
            "systemctl start idle-shutdown.service",
            "systemctl is-active idle-shutdown.service || (echo '[FATAL] idle-shutdown not running' && exit 1)",

            # ------------------------------------------------------------
            # Nginx reverse proxy (port 80 → Streamlit :8501)
            # ------------------------------------------------------------
            "echo '[CHECK] Installing nginx'",
            "dnf install -y nginx",

            "systemctl enable nginx",
            "systemctl start nginx",
            "systemctl is-active nginx || (echo '[FATAL] nginx not running' && exit 1)",

            "echo '[CHECK] Writing nginx Streamlit reverse proxy config'",

            "cat > /etc/nginx/conf.d/streamlit.conf << 'EOF'\n"
            "server {\n"
            "    listen 80;\n"
            "    server_name app.housing-planner.com;\n\n"
            "    location / {\n"
            "        proxy_pass http://127.0.0.1:8501;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\n"
            "        proxy_set_header Upgrade $http_upgrade;\n"
            "        proxy_set_header Connection \"upgrade\";\n"
            "        proxy_read_timeout 86400;\n"
            "    }\n"
            "}\n"
            "EOF",

            "nginx -t || (echo '[FATAL] nginx config invalid' && exit 1)",
            "systemctl reload nginx",

            # ============================================================
            # HousingPlanner bootstrap — ALL as ec2-user
            # ============================================================

            "echo '[CHECK] Installing system packages'",
            "dnf install -y git python3.12 python3.12-devel",

            # --- Prepare ec2-user directories ---
            "mkdir -p /home/ec2-user/logs",
            "chown -R ec2-user:ec2-user /home/ec2-user/logs",
            "chmod 755 /home/ec2-user/logs",

            # ------------------------------------------------------------
            # Run application setup as ec2-user
            # ------------------------------------------------------------
            "su - ec2-user -c \""
            "cd ~ && "
            "git clone https://github.com/vampireLibrarianMonk/HousingPlanner.git || true && "
            "cd HousingPlanner && "
            "git fetch && "
            "git checkout base-prototype-sun-disaster && "

            # Python venv
            "python3.12 -m venv .venv && "
            "source .venv/bin/activate && "

            # Dependencies
            "pip install --upgrade pip && "
            "pip install -r requirements.txt && "
            
            # Ensure log directory exists
            "mkdir -p /home/ec2-user/logs &&",
            "ls -ld /home/ec2-user/logs &&"

            # Launch Streamlit (USER-OWNED LOG FILE)
            "nohup streamlit run app.py "
            "--server.port=8501 "
            "--server.address=0.0.0.0 "
            "> /home/ec2-user/logs/streamlit.log 2>&1 &"
            "\"",

            "echo '===== [SUCCESS] Instance fully initialized ====='",
        )

        idle_script_asset.grant_read(instance.role)
        idle_service_asset.grant_read(instance.role)

        # Start STOPPED
        instance.instance_initiated_shutdown_behavior = (
            ec2.InstanceInitiatedShutdownBehavior.STOP
        )

        instance_env = {
            "INSTANCE_ID": instance.instance_id,
        }
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

        cert = acm.Certificate(
            self,
            "PlannerCert",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(zone),
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
