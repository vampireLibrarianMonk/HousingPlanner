# Deploy-Check.md

This checklist validates each functional component of the **HousePlannerStack**
*after* a successful `cdk deploy`.

All checks are **AWS CLI only**, fully **programmatic**, and safe to re-run.

---

## 1. CloudFormation Stack Health

```bash
aws cloudformation describe-stacks   --stack-name HousePlannerStack   --query "Stacks[0].StackStatus"
```

Expected:
```
CREATE_COMPLETE
```
(or `UPDATE_COMPLETE`)

---

## 2. EC2 Instance Exists (and is stopped by default)

```bash
aws ec2 describe-instances \
--filters "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
--query "Reservations[].Instances[].State.Name"
```

Expected:
```
stopped # or running depending on when you deployed the stack (within 1 hr)
```

---

## 3. Status Page Lambda (programmatic lookup + invoke)

```bash
FN=$(aws cloudformation list-stack-resources \
  --stack-name HousePlannerStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId,'StatusPageLambda')].PhysicalResourceId | [0]" \
  --output text) && \
aws lambda invoke --function-name "$FN" /tmp/status.json >/dev/null && \
jq -r '.body' /tmp/status.json
```

Expected output includes:
```bash
<html>
<head>
<title>House Planner</title>
<meta http-equiv="refresh" content="10">
</head>
<body>
<h1>House Planner</h1>
<p>Status: <b>running</b></p>

<p>Application is running.</p>
<a href="http://##.##.#.##:8501" target="_blank">Open Streamlit App</a>

</body>
</html>

```

---

## 4. Start Instance Lambda (programmatic lookup + invoke)

```bash
FN=$(aws cloudformation list-stack-resources \
--stack-name HousePlannerStack \
--query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId,'StartInstanceLambda')].PhysicalResourceId | [0]" \
--output text) && \
aws lambda invoke --function-name "$FN" /tmp/start.json && cat /tmp/start.json
```

Then confirm EC2 is running:

```bash
aws ec2 describe-instances \
--filters "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
--query "Reservations[].Instances[].State.Name"
```

Expected:
```
running
```

---

## 5. Idle Shutdown (automatic stop after inactivity)

After ~1 hour of inactivity:

```bash
aws ec2 describe-instances \
--filters "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
--query "Reservations[].Instances[].State.Name"
```

Expected:
```
stopped
```

---

## 6. API Gateway Exists

Capture the API ID and test directly:

```bash
API_ID=$(aws apigateway get-rest-apis \
  --query "items[?name=='HousePlannerAPI'].id | [0]" \
  --output text) && \
URL="https://${API_ID}.execute-api.${AWS_DEFAULT_REGION:-us-east-1}.amazonaws.com/prod/"
```

```bash
echo "API URL: $URL" && curl -fsSL "$URL"
```

Expected:
```bash
I URL: https://yu9zntqk41.execute-api.us-east-1.amazonaws.com/prod/

<html>
<head>
<title>House Planner</title>
<meta http-equiv="refresh" content="10">
</head>
<body>
<h1>House Planner</h1>
<p>Status: <b>running</b></p>

        <p>Application is running.</p>
        <a href="http://##.##.##.##:8501" target="_blank">Open Streamlit App</a>
        
</body>
</html>
```

---

## 7. CloudFront Distribution (custom domain attached)

```bash
aws cloudfront list-distributions --query "DistributionList.Items[].Aliases.Items[]"
```

Expected to include:
```
app.housing-planner.com
```

---

## 8. ACM Certificate (validated)

```bash
aws acm list-certificates \
--region us-east-1 \
--query "CertificateSummaryList[?DomainName=='app.housing-planner.com'].Status"
```

Expected:
```
ISSUED
```

---

## 9. Route53 Alias Record

```bash
HZ_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name housing-planner.com \
  --query "HostedZones[0].Id" \
  --output text | sed 's|/hostedzone/||')
```

```bash
aws route53 list-resource-record-sets \
  --hosted-zone-id "$HZ_ID" \
  --query "ResourceRecordSets[?Name=='app.housing-planner.com.' && Type=='A'].AliasTarget.DNSName" \
  --output text
```

Expected:
```
"A"
```

---

## 10. Get your Hosted Zone ID

```bash
HOSTED_ZONE_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name housing-planner.com \
  --query "HostedZones[0].Id" \
  --output text | sed 's|/hostedzone/||')

echo "Hosted Zone ID: $HOSTED_ZONE_ID"
```

Example Expected Output:
```bash
Hosted Zone ID: Z02860032ZNCH6LLUL27W
```

---

## 11. Get the EC2 public IP

```bash
EC2_IP=$(aws ec2 describe-instances \
  --filters \
    "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
    "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)

echo "EC2 IP: $EC2_IP"
```

Example Expected Output:
```bash
EC2 IP: 34.236.254.151
```

---

## 12. Create the Route 53 record

```bash
aws route53 change-resource-record-sets \
  --hosted-zone-id "$HOSTED_ZONE_ID" \
  --change-batch "{
    \"Changes\": [{
      \"Action\": \"UPSERT\",
      \"ResourceRecordSet\": {
        \"Name\": \"app.housing-planner.com\",
        \"Type\": \"A\",
        \"TTL\": 60,
        \"ResourceRecords\": [{\"Value\": \"$EC2_IP\"}]
      }
    }]
  }"
```

Example Expected Output:
```bash
{
    "ChangeInfo": {
        "Id": "/change/C030778642DUENE5B2VK",
        "Status": "PENDING",
        "SubmittedAt": "2026-01-14T18:00:56.832000+00:00"
    }
}
```

---

## 13. Verify DNS resolution

```bash
dig app.housing-planner.com +short
```

Expected:
```bash
34.236.254.151
```

---
