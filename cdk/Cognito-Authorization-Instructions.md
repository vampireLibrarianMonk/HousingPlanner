# Cognito-Authorization-Instructions.md

This document describes the **final authentication and authorization model** for the HousePlanner deployment.
It mirrors the structure and operational tone of `Deploy-Check.md` and is intended for **immediate operational use**.

---

## 0. Authentication Model (Final)

| Surface | Auth Mechanism | Enforcement Point |
|------|---------------|-------------------|
| Streamlit UI | Cognito Hosted UI (OIDC) | Application Load Balancer |
| `/api/status` | Cognito User Pool | API Gateway Authorizer |
| `/api/start` | Cognito + `admin` group | API Gateway + Lambda |

Key rules:

- CloudFront does **not** perform authentication.
- Authentication for the UI happens **at the ALB**, before EC2.
- Lambda@Edge is **not used for auth**.
- No secrets are stored in CloudFront, Edge, or client code.

---

## 1. Required Environment Variables

```bash
export STACK_NAME="HousePlannerStack"
export AWS_REGION="us-east-1"
```

---

## 2. Export Cognito Identifiers (Programmatic)

### Cognito User Pool ID

```bash
export COGNITO_USER_POOL_ID=$(aws cloudformation list-stack-resources \
--region "$AWS_REGION" \
--stack-name "$STACK_NAME" \
--query "StackResourceSummaries[?ResourceType=='AWS::Cognito::UserPool'].PhysicalResourceId | [0]" \
--output text)
```

---

### Cognito User Pool Client ID

```bash
export COGNITO_USER_POOL_CLIENT_ID=$(aws cloudformation list-stack-resources \
--region "$AWS_REGION" \
--stack-name "$STACK_NAME" \
--query "StackResourceSummaries[?ResourceType=='AWS::Cognito::UserPoolClient'].PhysicalResourceId | [0]" \
--output text)
```

---

### Cognito Hosted UI Domain Prefix

```bash
export COGNITO_DOMAIN_PREFIX=$(aws cloudformation list-stack-resources \
--region "$AWS_REGION" \
--stack-name "$STACK_NAME" \
--query "StackResourceSummaries[?ResourceType=='AWS::Cognito::UserPoolDomain'].PhysicalResourceId | [0]" \
--output text)
```

---

## 3. Export UI and API Endpoints

### UI Base URL

```bash
export UI_BASE_URL=$(aws cloudformation describe-stacks \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='StreamlitUiUrl'].OutputValue | [0]" \
  --output text)
```

---

### Cognito Login URL (ALB Callback)

```bash
export COGNITO_LOGIN_URL="https://${COGNITO_DOMAIN_PREFIX}.auth.${AWS_REGION}.amazoncognito.com/login?client_id=${COGNITO_USER_POOL_CLIENT_ID}&response_type=code&scope=openid+email&redirect_uri=${UI_BASE_URL}/oauth2/idpresponse"
```

---

## 4. User Management (Invite Only)

### Create User

Note: Using --message-action SUPPRESS creates the user silently with no email notification. Remove this flag to have Cognito send the default welcome email and temporary password. When suppressing email, a permanent password must be set manually.

```bash
aws cognito-idp admin-create-user \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username pmflani@gmail.com \
  --user-attributes \
    Name=email,Value=pmflani@gmail.com \
    Name=name,Value="Patrick Flanigan" \
  --message-action SUPPRESS 
```

### Set Permanent Password

```bash
aws cognito-idp admin-set-user-password \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username pmflani@gmail.com \
  --password '#12delta,HOUSING' \
  --permanent
```

---

## 5. Admin Privileges

```bash
aws cognito-idp admin-add-user-to-group \
--region "$AWS_REGION" \
--user-pool-id "$COGNITO_USER_POOL_ID" \
--username pmflani@gmail.com \
--group-name admin
```

---

## 6. UI Auth Flow

1. User accesses the CloudFront URL
2. ALB enforces Cognito OIDC authentication
3. Cognito redirects to `/oauth2/idpresponse`
4. ALB forwards authenticated traffic to EC2

---

## 7. API Authorization

All API requests require a Cognito access token.

---

## 8. Failure Modes

| Scenario | Result |
|------|--------|
| UI unauthenticated | Redirect to Cognito |
| API unauthenticated | 401 |
| API non-admin start | 403 |
| Direct EC2 access | Blocked |

---

## 9. Summary

This model provides secure, AWS-supported authentication without secrets at the edge.