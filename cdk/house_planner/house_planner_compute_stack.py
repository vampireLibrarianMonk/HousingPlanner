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

from common import get_nginx_warmup_page_html


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
        user_pool_client_id: str,
        user_pool_domain_name: str,
        app_domain_name: str,
        **kwargs,
    ) -> None:
        """
        :param vpc: VPC for instance placement
        :param ec2_security_group: Security group for EC2 instances
        :param user_pool_id: Cognito user pool ID (for tagging)
        :param user_pool_client_id: Cognito client ID (for logout URL)
        :param user_pool_domain_name: Cognito domain prefix (for logout URL)
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

        streamlit_service_asset = s3_assets.Asset(
            self,
            "StreamlitService",
            path="service/streamlit.service",
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
        streamlit_service_asset.grant_read(self.instance_role)

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
            "# Disable the default nginx server block (conflicts with our config)",
            "# Create a minimal nginx.conf that only includes our conf.d configs",
            "echo '[CHECK] Creating minimal nginx.conf'",
            "cat > /etc/nginx/nginx.conf << 'NGINXEOF'",
            "user nginx;",
            "worker_processes auto;",
            "error_log /var/log/nginx/error.log;",
            "pid /run/nginx.pid;",
            "",
            "include /usr/share/nginx/modules/*.conf;",
            "",
            "events {",
            "    worker_connections 1024;",
            "}",
            "",
            "http {",
            "    log_format main '$remote_addr - $remote_user [$time_local] \"$request\" '",
            "                    '$status $body_bytes_sent \"$http_referer\" '",
            "                    '\"$http_user_agent\" \"$http_x_forwarded_for\"';",
            "    access_log /var/log/nginx/access.log main;",
            "",
            "    sendfile on;",
            "    tcp_nopush on;",
            "    tcp_nodelay on;",
            "    keepalive_timeout 65;",
            "    types_hash_max_size 4096;",
            "",
            "    include /etc/nginx/mime.types;",
            "    default_type application/octet-stream;",
            "",
            "    # Load configs from conf.d (our streamlit.conf)",
            "    include /etc/nginx/conf.d/*.conf;",
            "}",
            "NGINXEOF",
            "",
            "# Remove any default config files",
            "rm -f /etc/nginx/conf.d/default.conf",
            "",
            "# Write startup pages BEFORE starting nginx to avoid 'Welcome to nginx!' flash",
            "echo '[CHECK] Writing nginx startup splash page'",
            "# Match the ALB warm-up page style for consistent UX (from common.py)",
            "cat > /usr/share/nginx/html/starting.html << 'EOF'",
            get_nginx_warmup_page_html(),
            "EOF",
            "",
            "# Copy starting.html to index.html to replace default 'Welcome to nginx!' page",
            "cp /usr/share/nginx/html/starting.html /usr/share/nginx/html/index.html",
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
            "    # Health check endpoint for ALB - always returns 200",
            "    location = /health {",
            "        access_log off;",
            "        return 200 'OK';",
            "        add_header Content-Type text/plain;",
            "    }",
            "",
            "    # Serve startup page on backend errors - use =200 to return 200 status",
            "    # This keeps ALB health checks passing while Streamlit starts up",
            "    error_page 502 503 504 =200 /starting.html;",
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
            "# Validate and start nginx with our config (not the default welcome page)",
            "nginx -t || (echo '[FATAL] nginx config invalid' && exit 1)",
            "systemctl enable nginx",
            "systemctl start nginx",
            "systemctl is-active nginx || (echo '[FATAL] nginx not running' && exit 1)",
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
            "git checkout logout-functionality && "
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
            'mkdir -p /home/ec2-user/logs"',
            "",
            "# ------------------------------------------------------------",
            "# Streamlit systemd service (starts on every boot)",
            "# ------------------------------------------------------------",
            "echo '[STREAMLIT] Installing Streamlit systemd service'",
            f"aws s3 cp {streamlit_service_asset.s3_object_url} /etc/systemd/system/streamlit.service",
            "",
            "# Update the service file with the app domain and logout URL",
            f"sed -i 's/--browser.serverPort=443/--browser.serverAddress={app_domain_name} --browser.serverPort=443/' /etc/systemd/system/streamlit.service",
            f"sed -i 's|COGNITO_LOGOUT_URL_PLACEHOLDER|https://{user_pool_domain_name}.auth.{self.region}.amazoncognito.com/logout?client_id={user_pool_client_id}\\&logout_uri=https://{app_domain_name}|' /etc/systemd/system/streamlit.service",
            "",
            "systemctl daemon-reload",
            "systemctl enable streamlit.service",
            "systemctl start streamlit.service",
            "",
            "# Wait briefly and check if Streamlit is listening",
            "sleep 10",
            "if ss -tlnp | grep -q ':8501'; then",
            "  echo '[STREAMLIT] SUCCESS - Streamlit is listening on port 8501'",
            "else",
            "  echo '[STREAMLIT] WARNING - Streamlit may still be starting, check: journalctl -u streamlit'",
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
