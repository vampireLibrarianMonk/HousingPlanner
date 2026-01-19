"""
house_planner_cognito_stack.py

Cognito Identity stack for House Planner.

Responsibilities:
- Cognito User Pool (invite-only, no self-registration)
- User Pool Client with OAuth settings
- User Pool Domain for hosted UI
- Store client secret in Secrets Manager

This stack handles authentication only - no compute or networking.
"""

from aws_cdk import (
    Stack,
    CfnOutput,
    aws_cognito as cognito,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct
import hashlib


class HousePlannerCognitoStack(Stack):
    """
    Cognito authentication stack.
    Creates the user pool, client, and domain for OIDC authentication.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        app_domain_name: str,
        **kwargs,
    ) -> None:
        """
        :param app_domain_name: Full app domain (e.g. app.housing-planner.com)
                                Used for OAuth callback URLs
        """
        super().__init__(scope, construct_id, **kwargs)

        # Generate a unique suffix for the Cognito domain
        account_hash = hashlib.sha1(self.account.encode()).hexdigest()[:8]

        # --------------------------------------------------
        # Cognito User Pool (invite-only)
        # --------------------------------------------------
        self.user_pool = cognito.UserPool(
            self,
            "HousePlannerUserPool",
            user_pool_name="HousePlannerUsers",
            self_sign_up_enabled=False,  # Invite only
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
                # Use HTML for proper email formatting
                email_body=(
                    "<html>"
                    "<body style=\"font-family: Arial, sans-serif; color: #333; line-height: 1.6;\">"
                    "<h2 style=\"color: #2563eb;\">Welcome to House Planner 👋</h2>"
                    "<hr style=\"border: 1px solid #e5e7eb;\">"
                    "<p>Your account has been created.</p>"
                    "<h3>Activate your private workspace:</h3>"
                    f"<p><a href=\"https://{app_domain_name}\" style=\"color: #2563eb;\">https://{app_domain_name}</a></p>"
                    "<h3>Sign in with:</h3>"
                    "<ul>"
                    "<li><strong>Username:</strong> {username}</li>"
                    "<li><strong>Temporary password:</strong> <code style=\"background: #f3f4f6; padding: 2px 6px;\">{####}</code></li>"
                    "</ul>"
                    "<p>You will be prompted to set a new password on first login.</p>"
                    "<p>After that, your private workspace will be created automatically.</p>"
                    "<br>"
                    "<p style=\"color: #6b7280;\">— House Planner Team</p>"
                    "</body>"
                    "</html>"
                ),
            ),
        )

        # --------------------------------------------------
        # User Pool Client (for ALB OIDC integration)
        # --------------------------------------------------
        self.user_pool_client = cognito.UserPoolClient(
            self,
            "HousePlannerUserPoolClient",
            user_pool=self.user_pool,
            user_pool_client_name="HousePlannerALBClient",
            generate_secret=True,  # Required for ALB OIDC
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                ],
                callback_urls=[f"https://{app_domain_name}/oauth2/idpresponse"],
                logout_urls=[f"https://{app_domain_name}"],
            ),
        )

        # --------------------------------------------------
        # User Pool Domain (hosted UI)
        # --------------------------------------------------
        self.user_pool_domain = cognito.UserPoolDomain(
            self,
            "HousePlannerCognitoDomain",
            user_pool=self.user_pool,
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"houseplanner-auth-{account_hash}"
            ),
        )

        # --------------------------------------------------
        # Store client secret in Secrets Manager
        # (Required for ALB OIDC to retrieve the secret)
        # --------------------------------------------------
        self.client_secret = secretsmanager.Secret(
            self,
            "CognitoClientSecret",
            secret_name="houseplanner/cognito-client-secret",
            secret_string_value=self.user_pool_client.user_pool_client_secret,
        )

        # --------------------------------------------------
        # Expose values for other stacks
        # --------------------------------------------------
        self.user_pool_id = self.user_pool.user_pool_id
        self.user_pool_client_id = self.user_pool_client.user_pool_client_id
        self.user_pool_domain_name = self.user_pool_domain.domain_name
        self.client_secret_arn = self.client_secret.secret_arn

        # --------------------------------------------------
        # Outputs
        # --------------------------------------------------
        CfnOutput(self, "UserPoolId", value=self.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.user_pool_client_id)
        CfnOutput(self, "UserPoolDomainName", value=self.user_pool_domain_name)
        CfnOutput(self, "ClientSecretArn", value=self.client_secret_arn)
        CfnOutput(
            self,
            "CognitoLoginUrl",
            value=f"https://{self.user_pool_domain_name}.auth.{self.region}.amazoncognito.com/login",
        )
