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

## 10. End-to-End User Check

```bash
curl https://app.housing-planner.com
```

Expected:
- Status page loads
- EC2 auto-starts if stopped
- Application transitions to ready

---

## Success Criteria

- No always-on compute
- EC2 starts only on demand
- EC2 stops automatically after inactivity
- Custom domain resolves via CloudFront
- ACM certificate is valid
- All checks pass without manual resource IDs
