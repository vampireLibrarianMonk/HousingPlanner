import os
import aws_cdk as cdk
from house_planner.stack import HousePlannerStack

app = cdk.App()

HousePlannerStack(
    app,
    "HousePlannerStack",
    env=cdk.Environment(
        account=os.environ["CDK_DEFAULT_ACCOUNT"],
        region=os.environ["CDK_DEFAULT_REGION"],
    ),
)

app.synth()
