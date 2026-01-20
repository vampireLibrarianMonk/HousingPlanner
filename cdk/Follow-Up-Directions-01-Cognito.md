# Follow-Up-Directions-01-Cognito.md
## User Management & Authentication Troubleshooting

This document covers Cognito user management and authentication troubleshooting for House Planner.

**Related Files:**
- `house_planner/house_planner_cognito_stack.py` - Cognito User Pool and Client
- `house_planner/house_planner_load_balancer_stack.py` - ALB OIDC authentication

---

## 1. Environment Variables

```bash
export AWS_REGION="us-east-1"
export COGNITO_USER_POOL_ID=$(aws cognito-idp list-user-pools --max-results 10 \
  --query "UserPools[?Name=='HousePlannerUsers'].Id | [0]" --output text)
echo "User Pool ID: $COGNITO_USER_POOL_ID"
```

---

## 2. Authentication Flow

| Step | Component | What Happens |
|------|-----------|--------------|
| 1 | CloudFront | User accesses `app.housing-planner.com` |
| 2 | ALB | OIDC authentication enforced |
| 3 | Cognito | User redirected to Cognito Hosted UI |
| 4 | User | Enters credentials, changes password if needed |
| 5 | Cognito | Redirects back to `/oauth2/idpresponse` |
| 6 | ALB | Validates tokens, sets session cookies |
| 7 | ALB | Returns warm-up page (or routes to EC2) |

---

## 3. User Management (Admin Only)

### Create User (Invite)

```bash
aws cognito-idp admin-create-user \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username "user@example.com" \
  --user-attributes \
    Name=email,Value="user@example.com" \
    Name=name,Value="User Name"
```

User will receive email with temporary password.

---

### Set Permanent Password (Skip Email)

```bash
aws cognito-idp admin-set-user-password \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username "user@example.com" \
  --password "SecurePassword123!" \
  --permanent
```

---

### List Users

```bash
aws cognito-idp list-users \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --query "Users[*].{Username:Username,Status:UserStatus,Created:UserCreateDate}" \
  --output table
```

---

### Delete User

**⚠️ This triggers cleanup of EC2 instance and ALB resources via CloudTrail event.**

```bash
aws cognito-idp admin-delete-user \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username "user@example.com"
```

See `Follow-Up-Directions-02-Lambda.md` for cleanup Lambda troubleshooting.

---

### Get User Details (including sub)

```bash
aws cognito-idp admin-get-user \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username "user@example.com" \
  --query "UserAttributes[?Name=='sub'].Value | [0]" \
  --output text
```

---

## 4. Troubleshooting Authentication

### User Cannot Log In

1. **Check user status:**
   ```bash
   aws cognito-idp admin-get-user \
     --region "$AWS_REGION" \
     --user-pool-id "$COGNITO_USER_POOL_ID" \
     --username "user@example.com" \
     --query "UserStatus" --output text
   ```
   
   - `CONFIRMED` - User can log in
   - `FORCE_CHANGE_PASSWORD` - User must change password on first login
   - `UNCONFIRMED` - User hasn't verified email

2. **Reset password:**
   ```bash
   aws cognito-idp admin-set-user-password \
     --region "$AWS_REGION" \
     --user-pool-id "$COGNITO_USER_POOL_ID" \
     --username "user@example.com" \
     --password "NewSecurePassword123!" \
     --permanent
   ```

---

### Cognito Callback URL Issues

The callback URL must be registered in the User Pool Client:

```bash
aws cognito-idp describe-user-pool-client \
  --region "$AWS_REGION" \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --client-id $(aws cognito-idp list-user-pool-clients \
    --user-pool-id "$COGNITO_USER_POOL_ID" \
    --query "UserPoolClients[?ClientName=='HousePlannerALBClient'].ClientId | [0]" \
    --output text) \
  --query "UserPoolClient.CallbackURLs"
```

Expected: `["https://app.housing-planner.com/oauth2/idpresponse"]`

---

### Check ALB OIDC Configuration

OIDC is configured in the ALB listener rules. Verify in AWS Console:
1. EC2 → Load Balancers → HousePlannerALB
2. Listeners → HTTPS:443 → View/edit rules
3. Default action should show "authenticate-oidc" before "fixed-response"

---

## 5. Success Criteria

- ✅ User receives invite email with temporary password
- ✅ User can log in via Cognito Hosted UI
- ✅ After authentication, user sees the warm-up page
- ✅ User deletion triggers EC2 cleanup

---

## 6. Related Documentation

- `Follow-Up-Directions-02-Lambda.md` - Lambda troubleshooting (ensure/delete)
- `Follow-Up-Directions-03-EC2.md` - EC2 instance diagnostics
- `Follow-Up-Directions-04-ALB.md` - ALB and routing issues
