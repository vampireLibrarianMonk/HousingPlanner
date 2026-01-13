import boto3
import os

ec2 = boto3.client("ec2")
INSTANCE_ID = os.environ["INSTANCE_ID"]

def lambda_handler(event, context):
    ec2.start_instances(InstanceIds=[INSTANCE_ID])
    return {
        "statusCode": 200,
        "body": "Instance starting"
    }
