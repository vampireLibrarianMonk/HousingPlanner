# House Planner CDK Deployment

This document covers infrastructure deployment only. It does not include local app usage.

## Prerequisites

- AWS CLI configured for the target account
- AWS CDK installed and bootstrapped for the account/region
- A registered domain in Route 53 or delegated to Route 53
- Python 3.12 and a virtual environment

## Configure Domains

Edit `app/cdk/app.py` and set the domain values:

```python
domain_name = "planner.yourdomain.com"
hosted_zone_name = "yourdomain.com"
```

## Secrets Manager

Create the required secrets:

```bash
aws secretsmanager create-secret \
  --name houseplanner/ors_api_key \
  --description "OpenRouteService API key for HousePlanner" \
  --secret-string "<ors-api-key>"
```

```bash
aws secretsmanager create-secret \
  --name houseplanner/google_maps_api_key \
  --description "Google Maps Routes API key for HousePlanner" \
  --secret-string "<google-maps-key>"
```

```bash
aws secretsmanager create-secret \
  --name houseplanner/waze_api_key \
  --description "Waze (OpenWebNinja) API key for commute incidents" \
  --secret-string "<waze-api-key>"
```


```bash
aws secretsmanager create-secret \
  --name houseplanner/door_profit_api_key \
  --description "DoorProfit API key for crime + sex offender overlays" \
  --secret-string "<door-profit-key>"
```

```bash
aws secretsmanager create-secret \
  --name houseplanner/schooldigger_app_id \
  --description "SchoolDigger App ID for school data enrichment" \
  --secret-string "<schooldigger-app-id>"
```

```bash
aws secretsmanager create-secret \
  --name houseplanner/schooldigger_api_key \
  --description "SchoolDigger API key for school data enrichment" \
  --secret-string "<schooldigger-api-key>"
```

```bash
aws secretsmanager create-secret \
  --name houseplanner/waze_api_key \
  --description "Waze (OpenWebNinja) API key for commute incidents" \
  --secret-string "<waze-api-key>"
```

## Deployment Steps

1) Export required deployment variables:

```bash
export MY_PUBLIC_IP=$(curl -s https://checkip.amazonaws.com)/32
echo ${MY_PUBLIC_IP}
export CLOUDFRONT_PL_ID=$(\
  aws ec2 describe-managed-prefix-lists \
    --query "PrefixLists[?PrefixListName=='com.amazonaws.global.cloudfront.origin-facing'].PrefixListId" \
    --output text
)
echo ${CLOUDFRONT_PL_ID}
```

2) Prepare the CDK context qualifier:

```bash
QUALIFIER=$(git remote get-url origin | tr -d '\n' | sha256sum | cut -c1-10)
jq --arg q "$QUALIFIER" '.context["@aws-cdk/core:bootstrapQualifier"]=$q' cdk.json > cdk.json.tmp && mv cdk.json.tmp cdk.json
```

3) Create the EC2 key pair:

```bash
aws ec2 create-key-pair \
  --key-name houseplanner-key \
  --query 'KeyMaterial' \
  --output text > houseplanner-key.pem
chmod 400 houseplanner-key.pem
```

4) Install CDK Python dependencies:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

5) Clean old context:

```bash
cdk context --clear
rm -rf cdk.out
```

6) Bootstrap CDK:

```bash
cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1
```

7) Synthesize and deploy:

```bash
cdk synth
cdk deploy --all \
  -c ssh_cidr="$MY_PUBLIC_IP" \
  -c cloudfront_pl_id="$CLOUDFRONT_PL_ID"
```

## After Deployment

Open the application status page:

```
https://planner.yourdomain.com
```

The page exposes:

- Instance status
- A start button for the EC2 workspace
- A link to the Streamlit app