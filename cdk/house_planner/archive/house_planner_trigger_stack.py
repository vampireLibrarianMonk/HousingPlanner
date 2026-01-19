from aws_cdk import (
    Stack,
    aws_lambda as _lambda,
    aws_iam as iam,
)
from aws_cdk.aws_cognito import CfnUserPool
from constructs import Construct


class HousePlannerCognitoTriggerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfn_user_pool: CfnUserPool,
        user_pool_id: str,
        post_confirmation_lambda_arn: str,
        **kwargs,
    ):
        super().__init__(scope, construct_id, **kwargs)

        # --------------------------------------------------
        # Import the Lambda by ARN
        # --------------------------------------------------
        post_confirm_fn = _lambda.Function.from_function_attributes(
            self,
            "PostConfirmationFn",
            function_arn=post_confirmation_lambda_arn,
            same_environment=True,
        )

        # --------------------------------------------------
        # Explicitly allow Cognito to invoke the Lambda
        # (add_trigger() would normally add this for you)
        # --------------------------------------------------
        post_confirm_fn.add_permission(
            "AllowCognitoPostConfirmationInvoke",
            principal=iam.ServicePrincipal("cognito-idp.amazonaws.com"),
            source_arn=f"arn:aws:cognito-idp:{self.region}:{self.account}:userpool/{user_pool_id}",
        )

        # --------------------------------------------------
        # Attach POST_CONFIRMATION via L1 UserPool LambdaConfig
        #
        # IMPORTANT: lambda_config is a typed object (LambdaConfigProperty),
        # not a dict. We must preserve any existing fields explicitly.
        # --------------------------------------------------
        existing = cfn_user_pool.lambda_config

        cfn_user_pool.lambda_config = CfnUserPool.LambdaConfigProperty(
            create_auth_challenge=getattr(existing, "create_auth_challenge", None) if existing else None,
            custom_email_sender=getattr(existing, "custom_email_sender", None) if existing else None,
            custom_message=getattr(existing, "custom_message", None) if existing else None,
            custom_sms_sender=getattr(existing, "custom_sms_sender", None) if existing else None,
            define_auth_challenge=getattr(existing, "define_auth_challenge", None) if existing else None,
            kms_key_id=getattr(existing, "kms_key_id", None) if existing else None,
            post_authentication=getattr(existing, "post_authentication", None) if existing else None,
            post_confirmation=post_confirmation_lambda_arn,
            pre_authentication=getattr(existing, "pre_authentication", None) if existing else None,
            pre_sign_up=getattr(existing, "pre_sign_up", None) if existing else None,
            pre_token_generation=getattr(existing, "pre_token_generation", None) if existing else None,
            user_migration=getattr(existing, "user_migration", None) if existing else None,
            verify_auth_challenge_response=getattr(existing, "verify_auth_challenge_response", None) if existing else None,
        )
