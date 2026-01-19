"""
house_planner_compute_stack.py

Compute stack for House Planner.

Responsibilities:
- EC2 Launch Template for user workspaces
- EC2 Instance Role with required permissions
- User data script for instance bootstrap
- S3 assets for idle shutdown scripts

This stack defines HOW instances are created, not WHEN.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    Tags,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_s3_assets as s3_assets,
)
from constructs import Construct


class HousePlannerComputeStack(Stack):
    """
    Compute template stack.
    Creates the EC2 launch template and instance role for user workspaces.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        ec2_security_group: ec2.ISecurityGroup,
        user_pool_id: str,
        app_domain_name: str,
        **kwargs,
    ) -> None:
        """
        :param vpc: VPC for instance placement
        :param ec2_security_group: Security group for EC2 instances
        :param user_pool_id: Cognito user pool ID (for tagging)
        :param app_domain_name: App domain for Streamlit config
        """
        super().__init__(scope, construct_id, **kwargs)

        # --------------------------------------------------
        # Upload bootstrap assets to S3
        # --------------------------------------------------
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

        # --------------------------------------------------
        # EC2 Instance Role
        # --------------------------------------------------
        self.instance_role = iam.Role(
            self,
            "HousePlannerInstanceRole",
            role_name="HousePlannerEC2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            description="Role for House Planner EC2 user workspace instances",
        )

        # Grant read access to bootstrap assets
        idle_script_asset.grant_read(self.instance_role)
        idle_service_asset.grant_read(self.instance_role)
        idle_timer_asset.grant_read(self.instance_role)

        # Allow reading application secrets
        self.instance_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadApplicationSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:houseplanner/*"
                ],
            )
        )

        # --------------------------------------------------
        # User Data Script
        # --------------------------------------------------
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "#!/bin/bash",
            "set -euxo pipefail",
            "",
            "echo '===== [BOOT] Cloud-init starting ====='",
            "",
            "# ------------------------------------------------------------",
            "# Ensure ec2-user exists and owns its home",
            "# ------------------------------------------------------------",
            "id ec2-user || useradd -m ec2-user",
            "mkdir -p /home/ec2-user",
            "chown -R ec2-user:ec2-user /home/ec2-user",
            "chmod 755 /home/ec2-user",
            "",
            "# ------------------------------------------------------------",
            "# SSH and EC2 Instance Connect setup",
            "# ------------------------------------------------------------",
            "echo '[CHECK] Installing EC2 Instance Connect'",
            "dnf install -y ec2-instance-connect",
            "",
            "echo '[CHECK] Ensuring sshd is running'",
            "systemctl enable sshd",
            "systemctl restart sshd",
            "systemctl is-active sshd || (echo '[FATAL] sshd not running' && exit 1)",
            "",
            "# ------------------------------------------------------------",
            "# Idle shutdown (root-owned system service)",
            "# ------------------------------------------------------------",
            "echo '[CHECK] Installing idle shutdown script'",
            f"aws s3 cp {idle_script_asset.s3_object_url} /usr/local/bin/idle_shutdown.sh",
            "chmod +x /usr/local/bin/idle_shutdown.sh",
            "",
            "echo '[CHECK] Installing idle shutdown systemd service'",
            f"aws s3 cp {idle_service_asset.s3_object_url} /etc/systemd/system/idle-shutdown.service",
            "",
            "echo '[CHECK] Installing idle shutdown systemd timer'",
            f"aws s3 cp {idle_timer_asset.s3_object_url} /etc/systemd/system/idle-shutdown.timer",
            "",
            "systemctl daemon-reexec",
            "systemctl daemon-reload",
            "# Enable and start the TIMER (not the service) - the timer triggers the service",
            "systemctl enable idle-shutdown.timer",
            "systemctl start idle-shutdown.timer",
            "systemctl list-timers | grep idle-shutdown || (echo '[FATAL] idle-shutdown timer not running' && exit 1)",
            "",
            "# ------------------------------------------------------------",
            "# Nginx reverse proxy (port 80 → Streamlit :8501)",
            "# ------------------------------------------------------------",
            "echo '[CHECK] Installing nginx'",
            "dnf install -y nginx",
            "",
            "systemctl enable nginx",
            "systemctl start nginx",
            "systemctl is-active nginx || (echo '[FATAL] nginx not running' && exit 1)",
            "",
            "# ------------------------------------------------------------",
            "# Nginx startup fallback page",
            "# ------------------------------------------------------------",
            "echo '[CHECK] Writing nginx startup splash page'",
            "cat > /usr/share/nginx/html/starting.html << 'EOF'",
            "<html>",
            "<head>",
            "  <title>House Planner</title>",
            '  <meta http-equiv="refresh" content="10">',
            "</head>",
            "<body>",
            "  <h1>Starting your workspace…</h1>",
            "  <p>This usually takes under a minute.</p>",
            "</body>",
            "</html>",
            "EOF",
            "",
            "# ------------------------------------------------------------",
            "# Nginx Streamlit reverse proxy config",
            "# ------------------------------------------------------------",
            "echo '[CHECK] Writing nginx Streamlit reverse proxy config'",
            "",
            "cat > /etc/nginx/conf.d/streamlit.conf << 'EOF'",
            "server {",
            "    listen 80 default_server;",
            "    server_name _;",
            "",
            "    # Root for static files",
            "    root /usr/share/nginx/html;",
            "",
            "    # Serve startup page on backend errors",
            "    error_page 502 503 504 /starting.html;",
            "    location = /starting.html {",
            "        internal;",
            "    }",
            "",
            "    location / {",
            "        proxy_connect_timeout 5s;",
            "        proxy_pass http://127.0.0.1:8501;",
            "        proxy_intercept_errors on;",
            "        proxy_http_version 1.1;",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto https;",
            '        proxy_set_header Upgrade $http_upgrade;',
            '        proxy_set_header Connection "upgrade";',
            "        proxy_read_timeout 86400;",  # Long timeout for WebSocket connections
            "    }",
            "}",
            "EOF",
            "",
            "nginx -t || (echo '[FATAL] nginx config invalid' && exit 1)",
            "systemctl reload nginx",
            "",
            "# ============================================================",
            "# HousingPlanner bootstrap — ALL as ec2-user",
            "# ============================================================",
            "",
            "echo '[CHECK] Installing system packages'",
            "dnf install -y git python3.12 python3.12-devel",
            "",
            "# --- Prepare ec2-user directories ---",
            "mkdir -p /home/ec2-user/logs",
            "chown -R ec2-user:ec2-user /home/ec2-user/logs",
            "chmod 755 /home/ec2-user/logs",
            "",
            "# ------------------------------------------------------------",
            "# Run application setup as ec2-user",
            "# ------------------------------------------------------------",
            "echo '[STREAMLIT] Starting application setup as ec2-user'",
            'su - ec2-user -c "',
            "set -x && ",
            "cd ~ && ",
            "echo '[STREAMLIT] Cloning repository...' && ",
            "git clone https://github.com/vampireLibrarianMonk/HousingPlanner.git || true && ",
            "cd HousingPlanner && ",
            "git fetch && ",
            "echo '[STREAMLIT] Repository ready' && ",
            "",
            "# Python venv",
            "echo '[STREAMLIT] Creating Python virtual environment...' && ",
            "python3.12 -m venv .venv && ",
            "source .venv/bin/activate && ",
            "echo '[STREAMLIT] Virtual environment activated' && ",
            "",
            "# Dependencies",
            "echo '[STREAMLIT] Installing dependencies (this may take 1-2 minutes)...' && ",
            "python -m pip install --upgrade pip && ",
            "python -m pip install -r requirements.txt && ",
            "echo '[STREAMLIT] Dependencies installed successfully' && ",
            "",
            "# Ensure log directory exists",
            "mkdir -p /home/ec2-user/logs && ",
            "",
            "# Environment Variables (INLINE, REAL EXPORTS)",
            "echo '[STREAMLIT] Fetching API keys from Secrets Manager...' && ",
            "export ORS_API_KEY=\\$(aws secretsmanager get-secret-value ",
            "--secret-id houseplanner/ors_api_key ",
            "--query SecretString ",
            "--output text) && ",
            "echo '[STREAMLIT] ORS_API_KEY loaded' && ",
            "",
            "export GOOGLE_MAPS_API_KEY=\\$(aws secretsmanager get-secret-value ",
            "--secret-id houseplanner/google_maps_api_key ",
            "--query SecretString ",
            "--output text) && ",
            "echo '[STREAMLIT] GOOGLE_MAPS_API_KEY loaded' && ",
            "",
            "# Launch Streamlit",
            "echo '[STREAMLIT] Launching Streamlit application...' && ",
            "nohup streamlit run app.py ",
            "--server.address=127.0.0.1 ",
            "--server.port=8501 ",
            "--server.headless=true ",
            "--server.enableCORS=false ",
            "--server.enableXsrfProtection=false ",
            f"--browser.serverAddress={app_domain_name} ",
            "--browser.serverPort=443 ",
            "--browser.gatherUsageStats=false ",
            '> /home/ec2-user/logs/streamlit.log 2>&1 &"',
            "",
            "# Wait briefly and check if Streamlit is listening",
            "sleep 5",
            "if ss -tlnp | grep -q ':8501'; then",
            "  echo '[STREAMLIT] SUCCESS - Streamlit is listening on port 8501'",
            "else",
            "  echo '[STREAMLIT] WARNING - Streamlit may still be starting, check /home/ec2-user/logs/streamlit.log'",
            "fi",
            "",
            "echo '===== [SUCCESS] Instance fully initialized ====='",
        )

        # --------------------------------------------------
        # EC2 Launch Template
        # --------------------------------------------------
        self.launch_template = ec2.LaunchTemplate(
            self,
            "HousePlannerLaunchTemplate",
            launch_template_name="HousePlannerWorkspace",
            instance_type=ec2.InstanceType("t4g.small"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            role=self.instance_role,
            security_group=ec2_security_group,
            user_data=user_data,
        )

        # Add tags to the launch template
        Tags.of(self.launch_template).add("App", "HousePlanner")
        Tags.of(self.launch_template).add("UserPoolId", user_pool_id)

        # --------------------------------------------------
        # Expose values for other stacks
        # --------------------------------------------------
        self.launch_template_id = self.launch_template.launch_template_id

        # --------------------------------------------------
        # Outputs
        # --------------------------------------------------
        CfnOutput(self, "LaunchTemplateId", value=self.launch_template_id)
        CfnOutput(self, "InstanceRoleArn", value=self.instance_role.role_arn)
