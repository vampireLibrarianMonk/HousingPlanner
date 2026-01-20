# Follow-Up-Directions-04-ALB.md
## ALB & Routing Troubleshooting

This document covers Application Load Balancer configuration and routing issues.

**Related Files:**
- `house_planner/house_planner_load_balancer_stack.py` - ALB, listener, and routing rules
- `house_planner/house_planner_cloud_front_stack.py` - CloudFront distribution
- `lambda/ensure_instance.py` - Creates user routing rules
- `common.py` - Warm-up page HTML

---

## 1. Architecture Overview

```
User → CloudFront → ALB → EC2 (nginx → Streamlit)
                 ↓
              Lambda (ensure_instance.py)
```

### Routing Flow

1. **New user (no routing cookie):**
   - ALB default action → OIDC auth → warm-up page
   - Warm-up page calls `/internal/ensure`
   - Lambda creates EC2, target group, routing rule, returns cookie

2. **Returning user (has routing cookie):**
   - ALB matches cookie-based rule → OIDC auth → forwards to user's EC2

---

## 2. Check ALB Health

### ALB Status

```bash
aws elbv2 describe-load-balancers \
  --names HousePlannerALB \
  --query "LoadBalancers[0].{DNS:DNSName,State:State.Code}"
```

Expected: `State: active`

---

## 3. Check Target Groups

### List All Target Groups

```bash
aws elbv2 describe-target-groups \
  --query "TargetGroups[*].{Name:TargetGroupName,ARN:TargetGroupArn,Type:TargetType}" \
  --output table
```

### Check User Target Groups

User target groups start with `u-`:

```bash
aws elbv2 describe-target-groups \
  --query "TargetGroups[?starts_with(TargetGroupName, 'u-')].{Name:TargetGroupName,HealthCheck:HealthCheckPath}" \
  --output table
```

Expected: Health check path should be `/health`

---

### Check Target Health

```bash
TG_ARN="arn:aws:elasticloadbalancing:us-east-1:xxx:targetgroup/u-xxx/xxx"

aws elbv2 describe-target-health \
  --target-group-arn "$TG_ARN" \
  --query "TargetHealthDescriptions[*].{Target:Target.Id,State:TargetHealth.State,Reason:TargetHealth.Reason}"
```

Expected states:
- `healthy` - EC2 passing health checks
- `unhealthy` - EC2 failing health checks
- `initial` - Waiting for first health check
- `unused` - Target not registered

---

## 4. Check Listener Rules

### Get Listener ARN

```bash
LISTENER_ARN=$(aws elbv2 describe-listeners \
  --load-balancer-arn $(aws elbv2 describe-load-balancers \
    --names HousePlannerALB --query "LoadBalancers[0].LoadBalancerArn" --output text) \
  --query "Listeners[?Port==\`443\`].ListenerArn | [0]" --output text)
echo "Listener ARN: $LISTENER_ARN"
```

### List All Rules

```bash
aws elbv2 describe-rules \
  --listener-arn "$LISTENER_ARN" \
  --query "Rules[*].{Priority:Priority,Conditions:Conditions[0]}" \
  --output table
```

Expected rules:
- Priority 1: `/internal/ensure` path → Lambda target
- Priority 2+: Cookie-based user rules → User target groups
- Default: Fixed response (warm-up page)

---

### Check Specific Rule

```bash
aws elbv2 describe-rules \
  --listener-arn "$LISTENER_ARN" \
  --query "Rules[?Priority=='1'].Actions"
```

Should show:
1. `authenticate-oidc` action
2. `forward` to Lambda target group

---

## 5. Troubleshooting Routing Issues

### User Stuck on Warm-Up Page

**Symptom:** Page shows "Starting... (Xs)" but never loads Streamlit.

1. **Check Lambda logs for errors:**
   ```bash
   aws logs tail /aws/lambda/HousePlannerEnsureInstance --follow
   ```

2. **Check if routing cookie is set:**
   - Open browser DevTools → Application → Cookies
   - Look for `hp_route_*` cookie

3. **Check if user's routing rule exists:**
   ```bash
   aws elbv2 describe-rules \
     --listener-arn "$LISTENER_ARN" \
     --query "Rules[*].{Priority:Priority,Cookie:Conditions[?Field=='http-header'].HttpHeaderConfig.Values|[0]}"
   ```

4. **Check target group health:**
   ```bash
   # Find user's target group
   aws elbv2 describe-target-groups \
     --query "TargetGroups[?starts_with(TargetGroupName, 'u-')].TargetGroupArn" --output text | while read TG; do
       echo "=== $TG ==="
       aws elbv2 describe-target-health --target-group-arn "$TG"
     done
   ```

---

### 502 Bad Gateway

**Symptom:** Getting 502 errors after warm-up page.

1. **Check target health:**
   - If unhealthy, EC2 nginx might not be running
   - See `Follow-Up-Directions-03-EC2.md` for nginx troubleshooting

2. **Check if EC2 is running:**
   ```bash
   aws ec2 describe-instances \
     --filters "Name=tag:Purpose,Values=HousePlannerUser" \
     --query "Reservations[*].Instances[*].{Id:InstanceId,State:State.Name}"
   ```

3. **Check nginx health endpoint directly (from EC2):**
   ```bash
   curl http://localhost/health
   ```
   Expected: `OK`

---

### OIDC Authentication Issues

**Symptom:** Redirect loops or authentication errors.

1. **Check Cognito callback URL:**
   ```bash
   aws cognito-idp describe-user-pool-client \
     --user-pool-id "$COGNITO_USER_POOL_ID" \
     --client-id $(aws cognito-idp list-user-pool-clients \
       --user-pool-id "$COGNITO_USER_POOL_ID" \
       --query "UserPoolClients[?ClientName=='HousePlannerALBClient'].ClientId | [0]" \
       --output text) \
     --query "UserPoolClient.CallbackURLs"
   ```
   Expected: `["https://app.housing-planner.com/oauth2/idpresponse"]`

2. **Check ALB OIDC configuration:**
   - AWS Console → EC2 → Load Balancers → HousePlannerALB
   - Listeners → HTTPS:443 → View/edit rules
   - Verify OIDC config points to correct Cognito domain

---

## 6. CloudFront Issues

### Check Distribution

```bash
aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Aliases.Items, 'app.housing-planner.com')].{Id:Id,Status:Status,Domain:DomainName}"
```

### Check Origin Configuration

```bash
DIST_ID=$(aws cloudfront list-distributions \
  --query "DistributionList.Items[?contains(Aliases.Items, 'app.housing-planner.com')].Id | [0]" --output text)

aws cloudfront get-distribution \
  --id "$DIST_ID" \
  --query "Distribution.DistributionConfig.Origins.Items[0].DomainName"
```

Should point to ALB DNS name.

---

### Check Cache Policy

CloudFront should use `CACHING_DISABLED` and `ALL_VIEWER` origin request policy:

```bash
aws cloudfront get-distribution \
  --id "$DIST_ID" \
  --query "Distribution.DistributionConfig.DefaultCacheBehavior.{CachePolicy:CachePolicyId,OriginRequestPolicy:OriginRequestPolicyId}"
```

---

## 7. Manual Cleanup

### Delete User's Routing Rule

```bash
RULE_ARN="arn:aws:elasticloadbalancing:us-east-1:xxx:listener-rule/xxx"
aws elbv2 delete-rule --rule-arn "$RULE_ARN"
```

### Delete Target Group

```bash
TG_ARN="arn:aws:elasticloadbalancing:us-east-1:xxx:targetgroup/u-xxx/xxx"

# Deregister targets first
aws elbv2 deregister-targets --target-group-arn "$TG_ARN" --targets Id=i-xxx

# Delete target group
aws elbv2 delete-target-group --target-group-arn "$TG_ARN"
```

---

## 8. Success Criteria

- ✅ ALB is active and healthy
- ✅ Lambda target group has no health check (Lambda doesn't need it)
- ✅ User target groups have `/health` health check
- ✅ Priority 1 rule routes `/internal/ensure` to Lambda
- ✅ User rules (priority 2+) match on cookie and forward to user target groups
- ✅ Default action shows warm-up page after OIDC auth
- ✅ CloudFront correctly forwards to ALB

---

## 9. Related Documentation

- `Follow-Up-Directions-01-Cognito.md` - User management
- `Follow-Up-Directions-02-Lambda.md` - Lambda troubleshooting
- `Follow-Up-Directions-03-EC2.md` - EC2 instance diagnostics
