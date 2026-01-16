# Cognito-Authorization-Instructions.md

This document validates and explains the **Cognito authorization model** used by the **HousePlannerStack**, mirroring the structure and intent of `Deploy-Check.md`.

It focuses on **who can access what**, **how authentication is enforced**, and **how to operate Cognito safely** using **AWS CLI only**, with **all required identifiers exported programmatically as environment variables**.

> Scope: Streamlit UI (CloudFront + Lambda@Edge) and API Gateway (`/api/status`, `/api/start`).

---

## 0. Authorization Model (At a Glance)

| Surface | Auth Mechanism | Enforcement Point |
|------|---------------|-------------------|
| Streamlit UI | Cognito Hosted UI | CloudFront (Lambda@Edge) |
| `/api/status` | Cognito User Pool | API Gateway Authorizer |
| `/api/start` | Cognito + `admin` group | API Gateway + Lambda check |

All entry points require **valid Cognito authentication**. There are **no public endpoints**.

---

## 1. Export Required Cognito Identifiers (Programmatic)

All Cognito identifiers are derived **directly from CloudFormation**, never hard‑coded.

### Export User Pool ID

```bash
export COGNITO_USER_POOL_ID=$(aws cloudformation list-stack-resources \
  --stack-name HousePlannerStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::Cognito::UserPool'].PhysicalResourceId | [0]" \
  --output text)
```

Verify:
```bash
aws cognito-idp describe-user-pool \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --query "UserPool.Name" \
  --output text
```

---

### Export User Pool Client ID

```bash
export COGNITO_USER_POOL_CLIENT_ID=$(aws cloudformation list-stack-resources \
  --stack-name HousePlannerStack \
  --query "StackResourceSummaries[?ResourceType=='AWS::Cognito::UserPoolClient'].PhysicalResourceId | [0]" \
  --output text)
```

---

### Export Cognito Hosted UI Domain

```bash
export COGNITO_DOMAIN_PREFIX=$(aws cloudformation list-stack-resources \
  --stack-name HousePlannerStack \
  --query "StackResourceSummaries[?LogicalResourceId=='HousePlannerCognitoDomain'].PhysicalResourceId | [0]" \
  --output text)
```

---

### Export Cognito Login URL

```bash
export COGNITO_LOGIN_URL="https://${COGNITO_DOMAIN_PREFIX}.auth.${AWS_REGION}.amazoncognito.com/login?client_id=${COGNITO_USER_POOL_CLIENT_ID}&response_type=code&scope=openid+email&redirect_uri=https://app.housing-planner.com"

echo "$COGNITO_LOGIN_URL"
```

---

## 2. Creating a User (Invite‑Only)

Self sign‑up is disabled. Users must be created by an operator.

### Create user

```bash
aws cognito-idp admin-create-user \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com \
  --message-action SUPPRESS
```

### Set permanent password

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username user@example.com \
  --password '<StrongPasswordHere>' \
  --permanent
```

---

## 3. Granting Admin Privileges (Required for `/api/start`)

Only users in the `admin` group may start the EC2 instance.

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username user@example.com \
  --group-name admin
```

Verify:

```bash
aws cognito-idp admin-list-groups-for-user \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username user@example.com
```

---

## 4. Streamlit UI Authorization Flow

1. User navigates to:
   ```
   https://app.housing-planner.com/
   ```
2. CloudFront invokes **Lambda@Edge** on every request.
3. If unauthenticated:
   - User is redirected to `$COGNITO_LOGIN_URL`.
4. After login:
   - Cognito redirects back to CloudFront.
   - UI access is granted.

The EC2 instance is **never reachable** without Cognito authentication.

---

## 5. API Authorization Flow (API Gateway)

All API endpoints require a **Bearer access token**.

### Call `/api/status`

```bash
curl -i \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  https://<api-id>.execute-api.<region>.amazonaws.com/prod/api/status
```

Expected:
- `200 OK`

Without token:
- `401 Unauthorized`

---

## 6. Admin‑Only `/api/start` Enforcement

Two layers:

1. **API Gateway Cognito Authorizer** (valid token)
2. **Lambda group check** (`cognito:groups` contains `admin`)

```bash
curl -i -X POST \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  https://<api-id>.execute-api.<region>.amazonaws.com/prod/api/start
```

Expected:
- `200 OK` → EC2 starts

Non‑admin:
- `403 Forbidden`

---

## 7. Failure Modes (Expected)

| Scenario | Result |
|-------|--------|
| No token | 401 |
| Expired token | 401 |
| Valid token, not admin | 403 |
| Forged group claim | 403 |
| Direct EC2 access | Blocked |

---

## 8. Operational Safety Notes

- Cognito is the **single source of identity**
- All identifiers are **programmatically derived**
- No secrets exist in Streamlit or CloudFront
- Admin access is auditable and revocable

---

## 9. Decommission / Lockdown

```bash
aws cognito-idp update-user-pool \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --admin-create-user-config AllowAdminCreateUserOnly=true
```

---

## 10. Summary

This model provides:

- Centralized identity
- Strong authentication
- Explicit admin controls
- Zero‑trust edge enforcement
- Fully scriptable operations

It is appropriate for **small trusted user groups** with **production‑grade security guarantees**.
