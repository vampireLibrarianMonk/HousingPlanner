import boto3
import os

ec2 = boto3.client("ec2")
INSTANCE_ID = os.environ["INSTANCE_ID"]

HTML_TEMPLATE = """
<html>
<head>
<title>House Planner</title>
<meta http-equiv="refresh" content="10">
</head>
<body>
<h1>House Planner</h1>
<p>Status: <b>{state}</b></p>
{body}
</body>
</html>
"""

def lambda_handler(event, context):
    res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
    inst = res["Reservations"][0]["Instances"][0]

    state = inst["State"]["Name"]

    if state == "stopped":
        body = """
        <form method="POST" action="/start">
            <button type="submit">Start Application</button>
        </form>
        """
    elif state == "running":
        ip = inst.get("PublicIpAddress", "pending")
        body = f"""
        <p>Application is running.</p>
        <a href="http://{ip}:8501" target="_blank">Open Streamlit App</a>
        """
    else:
        body = "<p>Starting up… please wait.</p>"

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": HTML_TEMPLATE.format(state=state, body=body),
    }
