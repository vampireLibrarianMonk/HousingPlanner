# Deploy-Check.md

This checklist validates each functional component of the **HousePlannerStack**
*after* a successful `cdk deploy`.

All checks are **AWS CLI only**, fully **programmatic**, and safe to re-run.

> Prereqs: AWS CLI v2 configured, and `jq` installed locally (`sudo apt-get install -y jq` on Ubuntu).

---

## 0. Known URLs (what you should be able to load)

These assume you set in `stack.py`:

- `domain_name = "app.housing-planner.com"`
- CloudFront is configured with:
  - **Default** behavior → **EC2 origin** (HTTP/80 via nginx reverse proxy to Streamlit)
  - Additional behaviors:
    - `/status` → API Gateway (StatusPageLambda)
    - `/start` → API Gateway (StartInstanceLambda)

So the intended URLs are:

- **Main Streamlit app (HTTPS via CloudFront):**  
  `https://app.housing-planner.com/`

- **Status page (HTTPS via CloudFront → API Gateway):**  
  `https://app.housing-planner.com/status`

- **Start endpoint (HTTPS via CloudFront → API Gateway):**  
  `https://app.housing-planner.com/start`  (POST)

---

## 1. CloudFormation Stack Health

```bash
aws cloudformation describe-stacks \
--stack-name HousePlannerStack \
--query "Stacks[0].StackStatus" \
--output text
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
--query "Reservations[].Instances[].State.Name" \
--output text
```

Expected:
```
stopped
```
(or `running` if you've already started it)

If you want **only the running one** (and keep the same tag filter behavior):

```bash
aws ec2 describe-instances \
  --filters \
    "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
    "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].State.Name" \
  --output text
```

---

## 3. Status Page Lambda (programmatic lookup + invoke)

```bash
FN=$(aws cloudformation list-stack-resources \
--stack-name HousePlannerStack
--query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId,'StatusPageLambda')].PhysicalResourceId | [0]" \
--output text) && \
aws lambda invoke --function-name "$FN" /tmp/status.json >/dev/null && jq -r '.body' /tmp/status.json | head -n 50
```

Expected output includes `<html>` and a status line. The link should be:

- `https://app.housing-planner.com/`

---

## 4. Start Instance Lambda (programmatic lookup + invoke)

```bash
FN=$(aws cloudformation list-stack-resources \
--stack-name HousePlannerStack \
--query "StackResourceSummaries[?ResourceType=='AWS::Lambda::Function' && contains(LogicalResourceId,'StartInstanceLambda')].PhysicalResourceId | [0]" \
--output text) && \
aws lambda invoke --function-name "$FN" /tmp/start.json >/dev/null && cat /tmp/start.json
```

Then confirm EC2 is running:

```bash
aws ec2 describe-instances \
--filters "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" "Name=instance-state-name,Values=running" \
--query "Reservations[].Instances[].State.Name | [0]" \
--output text
```

Expected:
```
running
```

---

## 5. CloudFront Distribution (custom domain attached)

```bash
aws cloudfront list-distributions \
--query "DistributionList.Items[].{Id:Id,Domain:DomainName,Aliases:Aliases.Items}" \
--output json | jq -r '.[] | select(.Aliases!=null) | select(.Aliases[]=="app.housing-planner.com") | "\(.Id)  \(.Domain)"'
```

Expected: one line with the distribution ID + its `*.cloudfront.net` domain.

---

## 6. ACM Certificate (validated, **us-east-1**)

CloudFront requires the ACM cert to be in **us-east-1**.

```bash
aws acm list-certificates \
--region us-east-1 \
--query "CertificateSummaryList[?DomainName=='app.housing-planner.com'].Status | [0]" \
--output text
```

Expected:
```
ISSUED
```

---

## 7. Route53 record (app → CloudFront)

This should be an **ALIAS** `A` record pointing to CloudFront (not an IP).

```bash
HZ_ID=$(aws route53 list-hosted-zones-by-name \
--dns-name housing-planner.com \
--query "HostedZones[0].Id" \
--output text | sed 's|/hostedzone/||')

aws route53 list-resource-record-sets \
--hosted-zone-id "$HZ_ID" \
--query "ResourceRecordSets[?Name=='app.housing-planner.com.' && Type=='A'].AliasTarget.DNSName | [0]" \
--output text
```

Expected: a `*.cloudfront.net.` DNS name (ends with a dot).

---

## 8. Verify DNS resolution

```bash
dig app.housing-planner.com +short
```

Expected: **CloudFront edge IPs** (these can change), not your EC2 public IP.

---

## 9. Validate CloudFront routing (end-to-end HTTPS)

Status page via CloudFront (should be 200 and HTML):

```bash
curl -fsSL -D- https://app.housing-planner.com/status | head -n 30
```

Main app via CloudFront (should be 200 and Streamlit HTML):

```bash
curl -fsSL -D- https://app.housing-planner.com/ | head -n 30
```

Start endpoint via CloudFront (POST; should return JSON):

```bash
curl -fsSL -X POST -D- https://app.housing-planner.com/start | head -n 30
```

---

## 10. Idle Shutdown (automatic stop after inactivity)

After ~1 hour of inactivity:

```bash
aws ec2 describe-instances \
--filters "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
--query "Reservations[].Instances[].State.Name" \
--output text
```

Expected:
```
stopped
```

If it stays `running`, check the service on-instance:

```bash
sudo systemctl status idle-shutdown.service --no-pager
sudo tail -n 200 /var/log/idle_shutdown.log
```