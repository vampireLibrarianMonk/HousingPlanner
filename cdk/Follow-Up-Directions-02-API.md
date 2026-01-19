# Deploy-Check.md

This checklist verifies **API Gateway + Lambda + CloudFront routing** after a successful `cdk deploy`.

It is intentionally scoped to:
- API Gateway
- Lambda (status / start)
- CloudFront (UI only)
- Route53
- ACM

**Explicitly excluded:** EC2 internals, idle shutdown, Cognito setup.

All commands assume AWS CLI v2 is configured.

---

## 1. Export Stack Outputs (one-time per shell)

Fetch the API and UI endpoints directly from CloudFormation and export them for reuse.

```bash
export STACK=HousePlannerStack

export API_BASE_URL=$(aws cloudformation describe-stacks \
  --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayBaseUrl'].OutputValue" \
  --output text)

export API_STATUS_URL=$(aws cloudformation describe-stacks \
  --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayStatusUrl'].OutputValue" \
  --output text)

export API_START_URL=$(aws cloudformation describe-stacks \
  --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='ApiGatewayStartUrl'].OutputValue" \
  --output text)

export UI_URL=$(aws cloudformation describe-stacks \
  --stack-name $STACK \
  --query "Stacks[0].Outputs[?OutputKey=='StreamlitUiUrl'].OutputValue" \
  --output text)
```

Verify:

```bash
echo $API_BASE_URL
echo $API_STATUS_URL
echo $API_START_URL
echo $UI_URL
```

---

## 2. API Gateway Sanity Check

Confirm the API exists and has a deployed stage.

```bash
aws apigateway get-rest-apis \
  --query "items[?contains(name,'HousePlanner')].id" \
  --output text
```

Expected: a single API ID.

---

## 3. Obtain an Access Token (assumed existing Cognito setup)

This guide assumes you **already have** a valid Cognito user and know how to authenticate.

Export a **valid ID token** before proceeding.

```bash
CLIENT_ID="xxxxxxxxxxxxxxxxxxxx"
CLIENT_SECRET="yyyyyyyyyyyyyyyyyyyy"
USERNAME="user@example.com"
PASSWORD="super-secret-password"

# --- Compute SECRET_HASH ---
SECRET_HASH=$(echo -n "${USERNAME}${CLIENT_ID}" | \
  openssl dgst -sha256 -hmac "${CLIENT_SECRET}" -binary | \
  base64)

# --- Authenticate and capture response ---
AUTH_RESPONSE=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id "$CLIENT_ID" \
  --auth-parameters \
    USERNAME="$USERNAME",PASSWORD="$PASSWORD",SECRET_HASH="$SECRET_HASH")

# --- Export tokens ---
export ID_TOKEN=$(echo "$AUTH_RESPONSE" | jq -r '.AuthenticationResult.IdToken')
export ACCESS_TOKEN=$(echo "$AUTH_RESPONSE" | jq -r '.AuthenticationResult.AccessToken')
export REFRESH_TOKEN=$(echo "$AUTH_RESPONSE" | jq -r '.AuthenticationResult.RefreshToken')

echo "Tokens exported:"
echo "  ID_TOKEN      = ${#ID_TOKEN} chars"
echo "  ACCESS_TOKEN  = ${#ACCESS_TOKEN} chars"
echo "  REFRESH_TOKEN = ${#REFRESH_TOKEN} chars"
```

You will reuse this token for all API calls below.

---

## 4. Status Endpoint (GET)

```bash
curl -i \
  -H "Authorization: Bearer $ID_TOKEN" \
  $API_STATUS_URL
```

Expected:
- `200 OK`
- HTML response showing instance state

This confirms:
- Correct API Gateway URL + stage
- Cognito authorizer is working
- Status Lambda executes successfully

---


## 5. Start Endpoint (POST)

This endpoint requires the caller to be in the **admin** group.

```bash
curl -i -X POST \
  -H "Authorization: Bearer $ID_TOKEN" \
  $API_START_URL
```

Expected:
- `200 OK`
- Body: `Instance starting`

If you receive `403 Forbidden`, the user is authenticated but **not in the admin group**.

---


## 6. Lambda Direct Invocation (debug only)

Use this section **only** if the authenticated API calls above fail.

### Status Lambda

```bash
export STATUS_FN=$(aws cloudformation list-stack-resources \
  --stack-name $STACK \
  --query "StackResourceSummaries[?LogicalResourceId=='StatusPageLambda'].PhysicalResourceId" \
  --output text)

aws lambda invoke --function-name $STATUS_FN /tmp/status.json >/dev/null
cat /tmp/status.json
```

Expected:
- `statusCode: 200`

### Start Lambda

```bash
export START_FN=$(aws cloudformation list-stack-resources \
  --stack-name $STACK \
  --query "StackResourceSummaries[?LogicalResourceId=='StartInstanceLambda'].PhysicalResourceId" \
  --output text)

aws lambda invoke --function-name $START_FN /tmp/start.json >/dev/null
cat /tmp/start.json
```

Expected:
- `403 Forbidden` (no auth context)

---


## 6. CloudFront (UI only)

Confirm CloudFront is serving the UI and **not** the API.

```bash
curl -I $UI_URL
```

Expected:
- `200 OK`
- `via: cloudfront`
- `server: nginx`

There should be **no** `/api/*` paths routed through CloudFront.

---

## 7. DNS Verification

```bash
dig $(echo $UI_URL | sed 's|https://||') +short
```

Expected:
- CloudFront edge IPs
- Not an EC2 public IP

---

## 8. Common Failure Meanings

**Missing Authentication Token**
- Wrong URL
- Missing `/prod` stage
- Path not defined in API Gateway

**Unauthorized**
- Correct routing
- Auth enforced
- Token missing or invalid

Only the second state is acceptable at this stage.

---

## 9. Final Confirmation

At rest:
- `/api/status` → `401 Unauthorized`
- `/api/start`  → `401 Unauthorized`
- UI loads via CloudFront

After authentication (handled separately):
- Status returns HTML
- Start transitions EC2 to `running`

This confirms the control plane is wired correctly.
