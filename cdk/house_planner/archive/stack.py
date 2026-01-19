from aws_cdk import (
    Stack,
    CfnOutput,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_cloudfront as cf,
    aws_cloudfront_origins as origins,
    aws_certificatemanager as acm,
    aws_lambda as _lambda,
    aws_route53 as r53,
    aws_route53_targets as r53_targets,
    aws_s3_assets as s3_assets,
    aws_cognito as cognito,
    Environment, Duration,
)
from constructs import Construct
import hashlib
import os


class HousePlannerStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(
            scope,
            id,
            env=Environment(
                account=os.environ["CDK_DEFAULT_ACCOUNT"],
                region="us-east-1",
            ),
            **kwargs,
        )

        account_hash = hashlib.sha1(self.account.encode("utf-8")).hexdigest()[:8]

        # =========================
        # CONFIG: set these values
        # =========================
        hosted_zone_name = "housing-planner.com"
        domain_name = "app.housing-planner.com"

        # --- VPC (minimal, custom) ---
        vpc = ec2.Vpc(
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

        # --- REQUIRED VPC endpoints for EC2 bootstrap ---
        vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
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

        idle_timer_asset = s3_assets.Asset(
            self,
            "IdleShutdownTimer",
            path="service/idle-shutdown.timer",
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

        cloudfront_pl_id = self.node.try_get_context("cloudfront_pl_id")
        if not cloudfront_pl_id:
            raise ValueError("Missing required context value: cloudfront_pl_id")

        cloudfront_origin_pl = ec2.PrefixList.from_prefix_list_id(
            self,
            "CloudFrontOriginPrefixList",
            cloudfront_pl_id,
        )

        # ==========================================================
        # Cognito User Pool (Private, Invite-Only)
        # ==========================================================

        user_pool = cognito.UserPool(
            self,
            "HousePlannerUserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=True,
            ),
            mfa=cognito.Mfa.OPTIONAL,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
        )

        user_pool_client = cognito.UserPoolClient(
            self,
            "HousePlannerUserPoolClient",
            user_pool=user_pool,
            generate_secret=True,  # REQUIRED for OAuth code → token exchange
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                ),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"https://{domain_name}/oauth2/idpresponse"],
                logout_urls=[f"https://{domain_name}"],
            ),
        )

        user_pool_domain = cognito.UserPoolDomain(
            self,
            "HousePlannerCognitoDomain",
            user_pool=user_pool,
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"houseplanner-auth-{account_hash}"
            ),
        )

        user_data = ec2.UserData.for_linux()

        # --- User data: idle shutdown + HousingPlanner bootstrap (SSH via key pair) ---
        user_data.add_commands(
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

            "echo '[CHECK] Installing idle shutdown systemd timer'",
            f"aws s3 cp {idle_timer_asset.s3_object_url} /etc/systemd/system/idle-shutdown.timer",

            "systemctl daemon-reexec",
            "systemctl daemon-reload",
            "systemctl enable idle-shutdown.service",
            "systemctl start idle-shutdown.service",
            "systemctl list-timers | grep idle-shutdown || (echo '[FATAL] idle-shutdown timer not running' && exit 1)",

            # ------------------------------------------------------------
            # Nginx reverse proxy (port 80 → Streamlit :8501)
            # ------------------------------------------------------------
            "echo '[CHECK] Installing nginx'",
            "dnf install -y nginx",

            "systemctl enable nginx",
            "systemctl start nginx",
            "systemctl is-active nginx || (echo '[FATAL] nginx not running' && exit 1)",

            # ------------------------------------------------------------
            # Nginx startup fallback page
            # ------------------------------------------------------------
            "echo '[CHECK] Writing nginx startup splash page'",
            "cat > /usr/share/nginx/html/starting.html << 'EOF'\n"
            "<html>\n"
            "<head>\n"
            "  <title>House Planner</title>\n"
            "  <meta http-equiv=\"refresh\" content=\"10\">\n"
            "</head>\n"
            "<body>\n"
            "  <h1>Starting your workspace…</h1>\n"
            "  <p>This usually takes under a minute.</p>\n"
            "</body>\n"
            "</html>\n"
            "EOF",

            # ------------------------------------------------------------
            # Nginx Streamlit reverse proxy config
            # ------------------------------------------------------------
            "echo '[CHECK] Writing nginx Streamlit reverse proxy config'",

            "cat > /etc/nginx/conf.d/streamlit.conf << 'EOF'\n"
            "server {\n"
            "    listen 80 default_server;\n"
            "    server_name _;\n\n"
            "    location / {\n"
            "        proxy_connect_timeout 5s;\n"
            "        proxy_read_timeout 30s;\n"
            "        proxy_pass http://127.0.0.1:8501;\n"
            "        error_page 502 503 504 = /starting.html;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Real-IP $remote_addr;\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
            "        proxy_set_header X-Forwarded-Proto https;\n"
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

            # Python venv
            "python3.12 -m venv .venv && "
            "source .venv/bin/activate && "

            # Dependencies
            "python -m pip install --upgrade pip && "
            "python -m pip install -r requirements.txt && "

            # Ensure log directory exists
            "mkdir -p /home/ec2-user/logs && "
            "ls -ld /home/ec2-user/logs && "

            # Environment Variables (INLINE, REAL EXPORTS)
            "export ORS_API_KEY=$(aws secretsmanager get-secret-value "
            "--secret-id houseplanner/ors_api_key "
            "--query SecretString "
            "--output text) && "

            "export GOOGLE_MAPS_API_KEY=$(aws secretsmanager get-secret-value "
            "--secret-id houseplanner/google_maps_api_key "
            "--query SecretString "
            "--output text) && "

            # Launch Streamlit
            "nohup streamlit run app.py "
            "--server.address=127.0.0.1 "
            "--server.port=8501 "
            "--server.headless=true "
            "--server.enableCORS=false "
            "--server.enableXsrfProtection=false "
            "--browser.serverAddress=app.housing-planner.com "
            "--browser.serverPort=443 "
            "--browser.gatherUsageStats=false "
            "> /home/ec2-user/logs/streamlit.log 2>&1 &"
            "\"",

            "echo '===== [SUCCESS] Instance fully initialized ====='",
        )

        user_instance_role = iam.Role(
            self,
            "HousePlannerUserInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        )

        launch_template = ec2.LaunchTemplate(
            self,
            "HousePlannerUserLaunchTemplate",
            instance_type=ec2.InstanceType("t4g.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            security_group=sg,
            key_pair=ec2.KeyPair.from_key_pair_name(
                self,
                "HousePlannerKeyPair",
                "houseplanner-key",
            ),
            role=user_instance_role,  # 👈 REQUIRED
            user_data=user_data,
        )

        alb_sg = ec2.SecurityGroup(
            self,
            "HousePlannerAlbSG",
            vpc=vpc,
            description="ALB security group CloudFront to ALB",
            allow_all_outbound=True,
        )

        # Allow HTTPS to ALB only from CloudFront origin-facing prefix list
        alb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(cloudfront_origin_pl.prefix_list_id),
            ec2.Port.tcp(443),
            "Allow HTTPS from CloudFront origin only",
        )

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "HousePlannerALB",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
        )

        alb_origin = origins.LoadBalancerV2Origin(
            alb,
            protocol_policy=cf.OriginProtocolPolicy.HTTPS_ONLY,
        )

        # ==========================================================
        # HTTPS: CloudFront + ACM (cert MUST be in us-east-1)
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

        https_listener = alb.add_listener(
            "HttpsListener",
            port=443,
            certificates=[elbv2.ListenerCertificate(cert.certificate_arn)],
        )

        https_listener.add_action(
            "OidcAuthOnly",
            action=elbv2.ListenerAction.authenticate_oidc(
                issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
                authorization_endpoint=(
                    f"https://{user_pool_domain.domain_name}.auth."
                    f"{self.region}.amazoncognito.com/oauth2/authorize"
                ),
                token_endpoint=(
                    f"https://{user_pool_domain.domain_name}.auth."
                    f"{self.region}.amazoncognito.com/oauth2/token"
                ),
                user_info_endpoint=(
                    f"https://{user_pool_domain.domain_name}.auth."
                    f"{self.region}.amazoncognito.com/oauth2/userInfo"
                ),
                client_id=user_pool_client.user_pool_client_id,
                client_secret=user_pool_client.user_pool_client_secret,
                scope="openid email",
                next=elbv2.ListenerAction.fixed_response(
                    status_code=403,
                    content_type="text/plain",
                    message_body="No workspace is assigned to this user.",
                ),
            ),
        )

        create_user_instance = _lambda.Function(
            self,
            "CreateUserEc2Instance",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="create_instance.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(60),
            environment={
                "LAUNCH_TEMPLATE_ID": launch_template.launch_template_id,
                "SUBNET_ID": vpc.public_subnets[0].subnet_id,
                "ALB_LISTENER_ARN": https_listener.listener_arn,  # 👈 NEW
                "VPC_ID": vpc.vpc_id,  # 👈 NEW
            },
        )

        create_user_instance.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:RunInstances",
                    "ec2:CreateTags",
                    "elasticloadbalancing:CreateTargetGroup",
                    "elasticloadbalancing:RegisterTargets",
                    "elasticloadbalancing:CreateRule",
                    "elasticloadbalancing:AddTags",
                ],
                resources=["*"],
            )
        )

        delete_user_resources = _lambda.Function(
            self,
            "DeleteUserEc2Resources",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="delete_instance.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(60),
            environment={
                "ALB_LISTENER_ARN": https_listener.listener_arn,
            },
        )

        delete_user_resources.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:TerminateInstances",

                    "elasticloadbalancing:DescribeRules",
                    "elasticloadbalancing:DeleteRule",
                    "elasticloadbalancing:DescribeTargetGroups",
                    "elasticloadbalancing:DeleteTargetGroup",
                    "elasticloadbalancing:DescribeTags",
                ],
                resources=["*"],
            )
        )

        user_delete_rule = events.Rule(
            self,
            "CognitoUserDeleteRule",
            event_pattern=events.EventPattern(
                source=["aws.cognito-idp"],
                detail_type=["AWS API Call via CloudTrail"],
                detail={
                    "eventSource": ["cognito-idp.amazonaws.com"],
                    "eventName": ["AdminDeleteUser"],
                    "requestParameters": {
                        "userPoolId": [user_pool.user_pool_id]
                    },
                },
            ),
        )

        user_delete_rule.add_target(
            targets.LambdaFunction(delete_user_resources)
        )

        idle_script_asset.grant_read(user_instance_role)
        idle_service_asset.grant_read(user_instance_role)
        idle_timer_asset.grant_read(user_instance_role)

        user_instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:houseplanner/ors_api_key*",
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:houseplanner/google_maps_api_key*",
                ],
            )
        )

        user_pool.add_trigger(
            cognito.UserPoolOperation.POST_CONFIRMATION,
            create_user_instance,
        )

        # ==========================================================
        # ALB (Cognito auth happens here, not at Lambda@Edge)
        # ==========================================================

        # EC2 should accept HTTP only from the ALB now (not directly from CloudFront)
        # (Replace the old CloudFront->EC2 ingress rule with this)
        # Allow ALB to reach EC2 on port 80
        sg.add_ingress_rule(
            alb_sg,
            ec2.Port.tcp(80),
            "Allow HTTP from ALB only",
        )

        distribution = cf.Distribution(
            self,
            "PlannerDistribution",
            default_behavior=cf.BehaviorOptions(
                origin=alb_origin,
                viewer_protocol_policy=cf.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cf.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cf.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cf.OriginRequestPolicy.ALL_VIEWER,
            ),
            domain_names=[domain_name],
            certificate=cert,
        )

        # ==========================================================
        # Route53: planner.<domain> -> CloudFront (DNS alias)
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

        # -------------------------------
        # Streamlit UI (CloudFront only)
        # -------------------------------
        CfnOutput(
            self,
            "StreamlitUiUrl",
            value=f"https://{domain_name}",
        )

        # -------------------------------
        # Cognito
        # -------------------------------
        CfnOutput(
            self,
            "CognitoLoginUrl",
            value=(
                f"https://{user_pool_domain.domain_name}.auth."
                f"{self.region}.amazoncognito.com/login"
                f"?client_id={user_pool_client.user_pool_client_id}"
                f"&response_type=code"
                f"&scope=openid+email"
                f"&redirect_uri=https://{domain_name}/oauth2/idpresponse"
            ),
        )
