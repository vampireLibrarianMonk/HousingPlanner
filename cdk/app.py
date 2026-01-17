import aws_cdk as cdk
from house_planner.stack import HousePlannerStack

app = cdk.App()

HousePlannerStack(
    app,
    "HousePlannerStack",
)

app.synth()
