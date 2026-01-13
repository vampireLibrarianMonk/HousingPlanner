import aws_cdk as cdk
from house_planner.stack import HousePlannerStack

app = cdk.App()

HousePlannerStack(
    app,
    "HousePlannerStack",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region"),
    ),
)

app.synth()
