from aws_cdk import (
    Duration,
    Stack,
    CfnOutput,
    aws_ec2 as ec2,
    aws_elasticloadbalancingv2 as elbv2,
    aws_certificatemanager as acm,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_route53 as r53,
    aws_s3_assets as s3_assets,
    aws_cognito as cognito,
    Tags,
    aws_secretsmanager as secretsmanager,
    SecretValue,
)
from aws_cdk import aws_elasticloadbalancingv2_targets as elbv2_targets
from aws_cdk.aws_cognito import CfnUserPool
from constructs import Construct
import hashlib

class HousePlannerIdentityStack(Stack):
    """
    Identity + Edge stack.
    Owns Cognito, ALB (OIDC), CloudFront, EC2 launch template,
    and the ALB-triggered ensure-instance Lambda (lazy provisioning).
    """

    def __init__(self, scope: Construct, id: str, **kwargs):
        super().__init__(scope, id, **kwargs)

        account_hash = hashlib.sha1(self.account.encode()).hexdigest()[:8]

        hosted_zone_name = "housing-planner.com"
        domain_name = "app.housing-planner.com"

        # --------------------------------------------------
        # VPC (public-only, no NAT)
        # --------------------------------------------------
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

        # Required AWS API access for EC2 bootstrap
        vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )
        vpc.add_interface_endpoint(
            "SecretsManagerEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
        )

        # --------------------------------------------------
        # Cognito (invite-only)
        # --------------------------------------------------
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
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your House Planner account is ready",
                email_body=(
                    "Welcome to House Planner 👋\n"
                    "--------------------------------\n"
                    "Your account has been created.\n\n"
                    "Activate your private workspace:\n"
                    "https://app.housing-planner.com\n\n"
                    "Sign in with:\n"
                    "• Username: {username}\n"
                    "• Temporary password: {####}\n\n"
                    "You will be prompted to set a new password on first login.\n"
                    "After that, your private workspace will be created automatically.\n\n"
                    "— House Planner Team"
                ),
            ),
        )

        self.cfn_user_pool = user_pool.node.default_child
        assert isinstance(self.cfn_user_pool, CfnUserPool)

        user_pool_client = cognito.UserPoolClient(
            self,
            "HousePlannerUserPoolClient",
            user_pool=user_pool,
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
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

        # --------------------------------------------------
        # EC2 user-data assets (UNCHANGED)
        # --------------------------------------------------
        idle_script_asset = s3_assets.Asset(
            self, "IdleShutdownScript", path="scripts/idle_shutdown.sh"
        )
        idle_service_asset = s3_assets.Asset(
            self, "IdleShutdownService", path="service/idle-shutdown.service"
        )
        idle_timer_asset = s3_assets.Asset(
            self, "IdleShutdownTimer", path="service/idle-shutdown.timer"
        )

        user_data = ec2.UserData.for_linux()
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

        # --------------------------------------------------
        # EC2 role + launch template
        # --------------------------------------------------
        instance_role = iam.Role(
            self,
            "HousePlannerUserInstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
        )

        idle_script_asset.grant_read(instance_role)
        idle_service_asset.grant_read(instance_role)
        idle_timer_asset.grant_read(instance_role)

        instance_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:houseplanner/*"
                ],
            )
        )

        launch_template = ec2.LaunchTemplate(
            self,
            "HousePlannerLaunchTemplate",
            instance_type=ec2.InstanceType("t4g.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            role=instance_role,
            user_data=user_data,
        )

        Tags.of(launch_template).add("App", "HousePlanner")
        Tags.of(launch_template).add("UserPoolId", user_pool.user_pool_id)

        # --------------------------------------------------
        # ALB + CloudFront + HTTPS
        # --------------------------------------------------
        self.alb = elbv2.ApplicationLoadBalancer(
            self, "HousePlannerALB", vpc=vpc, internet_facing=True
        )

        # Allow CloudFront (public internet) to reach ALB over HTTPS
        self.alb.connections.allow_from_any_ipv4(
            ec2.Port.tcp(443),
            "Allow CloudFront HTTPS traffic"
        )

        self.alb_dns_name = self.alb.load_balancer_dns_name

        cert = acm.Certificate(
            self,
            "PlannerCert",
            domain_name=domain_name,
            validation=acm.CertificateValidation.from_dns(
                r53.HostedZone.from_lookup(
                    self, "HostedZone", domain_name=hosted_zone_name
                )
            ),
        )

        cognito_client_secret = secretsmanager.Secret(
            self,
            "CognitoClientSecret",
            secret_string_value=user_pool_client.user_pool_client_secret,
        )

        https_listener = self.alb.add_listener(
            "HttpsListener",
            port=443,
            certificates=[elbv2.ListenerCertificate(cert.certificate_arn)],
            default_action=elbv2.ListenerAction.authenticate_oidc(
                issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
                authorization_endpoint=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com/oauth2/authorize",
                token_endpoint=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com/oauth2/token",
                user_info_endpoint=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com/oauth2/userInfo",
                client_id=user_pool_client.user_pool_client_id,
                client_secret=SecretValue.secrets_manager(cognito_client_secret.secret_arn),
                scope="openid email",
                next=elbv2.ListenerAction.fixed_response(
                    status_code=200,
                    content_type="text/html",
                    message_body="""
                    <html>
                      <head>
                        <title>House Planner</title>
                        <meta http-equiv="refresh" content="5">
                        <script>
                          fetch("/internal/ensure", {
                            method: "POST",
                            credentials: "include"
                          });
                        </script>
                      </head>
                      <body>
                        <h1>Starting your workspace…</h1>
                        <p>This usually takes under a minute.</p>
                      </body>
                    </html>
                    """,
                ),
            ),
        )

        # Create target group first WITHOUT the Lambda target to break circular dependency
        ensure_tg = elbv2.ApplicationTargetGroup(
            self,
            "EnsureLambdaTG",
            target_type=elbv2.TargetType.LAMBDA,
        )
        ensure_tg.configure_health_check(enabled=False)

        ensure_instance_fn = _lambda.Function(
            self,
            "EnsureUserInstance",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="ensure_instance.lambda_handler",
            code=_lambda.Code.from_asset("lambda"),
            timeout=Duration.seconds(60),
            environment={
                "LAUNCH_TEMPLATE_ID": launch_template.launch_template_id,
                "SUBNET_ID": vpc.public_subnets[0].subnet_id,
                "VPC_ID": vpc.vpc_id,
                # Pass ALB ARN instead of listener ARN to avoid circular dependency
                # Lambda will discover the HTTPS listener at runtime
                "ALB_ARN": self.alb.load_balancer_arn,
            },
        )

        ensure_instance_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:DescribeInstances",
                    "ec2:RunInstances",
                    "ec2:StartInstances",
                    "ec2:CreateTags",
                    "elasticloadbalancing:*",
                ],
                resources=["*"],
            )
        )

        # Add Lambda as target AFTER it's created
        ensure_tg.add_target(elbv2_targets.LambdaTarget(ensure_instance_fn))

        https_listener.add_action(
            "EnsureRoute",
            priority=1,
            conditions=[
                elbv2.ListenerCondition.path_patterns(["/internal/ensure"])
            ],
            action=elbv2.ListenerAction.authenticate_oidc(
                issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
                authorization_endpoint=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com/oauth2/authorize",
                token_endpoint=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com/oauth2/token",
                user_info_endpoint=f"https://{user_pool_domain.domain_name}.auth.{self.region}.amazoncognito.com/oauth2/userInfo",
                client_id=user_pool_client.user_pool_client_id,
                client_secret=SecretValue.secrets_manager(cognito_client_secret.secret_arn),
                scope="openid email",
                next=elbv2.ListenerAction.forward([ensure_tg]),
            ),
        )

        # --------------------------------------------------
        # Cross-stack exports (REQUIRED)
        # --------------------------------------------------
        self.user_pool_id = user_pool.user_pool_id
        self.alb_listener_arn = https_listener.listener_arn
        self.launch_template_id = launch_template.launch_template_id
        self.subnet_id = vpc.public_subnets[0].subnet_id
        self.vpc_id = vpc.vpc_id

        CfnOutput(self, "UserPoolId", value=self.user_pool_id)
        CfnOutput(self, "AlbListenerArn", value=self.alb_listener_arn)
        CfnOutput(self, "LaunchTemplateId", value=self.launch_template_id)
        CfnOutput(self, "SubnetId", value=self.subnet_id)
        CfnOutput(self, "VpcId", value=self.vpc_id)
