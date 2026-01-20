# Follow-Up-Directions-02-Lambda.md
## Lambda Troubleshooting (Ensure & Delete)

This document covers troubleshooting for the two Lambda functions that manage user EC2 instances.

**Related Files:**
- `lambda/ensure_instance.py` - Creates/starts EC2 instances and ALB routing rules
- `lambda/delete_instance.py` - Cleans up EC2 instances and ALB resources on user deletion
- `house_planner/house_planner_load_balancer_stack.py` - Ensure Lambda configuration
- `house_planner/house_planner_cleanup_stack.py` - Delete Lambda and EventBridge rule

---

## 1. Lambda Overview

| Lambda | Trigger | Purpose |
|--------|---------|---------|
| `HousePlannerEnsureInstance` | ALB path `/internal/ensure` | Provisions or starts EC2 for authenticated user |
| `HousePlannerDeleteUserResources` | CloudTrail event (user deletion) | Cleans up EC2 and ALB resources |

---

## 2. View Lambda Logs

### Ensure Lambda Logs

```bash
aws logs tail /aws/lambda/HousePlannerEnsureInstance --follow
```

Or view recent logs:

```bash
aws logs filter-log-events \
  --log-group-name /aws/lambda/HousePlannerEnsureInstance \
  --start-time $(date -d '1 hour ago' +%s000) \
  --query "events[*].message" --output text
```

---

### Delete Lambda Logs

```bash
aws logs tail /aws/lambda/HousePlannerDeleteUserResources --follow
```

---

## 3. Ensure Lambda Troubleshooting

### Common Errors

#### `InvalidTarget: targets are not in a running state`

**Cause:** Lambda tried to register EC2 with ALB before instance reached "running" state.

**Solution:** Lambda now waits up to 60 seconds for running state. If still failing:
1. Check EC2 is starting properly
2. Increase Lambda timeout (currently 120s)

---

#### `Missing x-amzn-oidc-identity header`

**Cause:** OIDC authentication did not complete before request reached Lambda.

**Solution:** Check ALB OIDC configuration:
```bash
aws elbv2 describe-rules \
  --listener-arn $(aws elbv2 describe-listeners \
    --load-balancer-arn $(aws elbv2 describe-load-balancers \
      --names HousePlannerALB --query "LoadBalancers[0].LoadBalancerArn" --output text) \
    --query "Listeners[?Port==\`443\`].ListenerArn | [0]" --output text) \
  --query "Rules[?Priority=='1'].Actions"
```

Should show `authenticate-oidc` action before `forward`.

---

#### `AccessDenied: ModifyTargetGroupAttributes`

**Cause:** Lambda IAM role missing permission.

**Solution:** Lambda role needs `elasticloadbalancing:ModifyTargetGroupAttributes`.
This is configured in `house_planner_load_balancer_stack.py`.

---

### Verify Lambda Execution

1. **Get recent invocations:**
   ```bash
   aws logs filter-log-events \
     --log-group-name /aws/lambda/HousePlannerEnsureInstance \
     --filter-pattern "START" \
     --start-time $(date -d '30 minutes ago' +%s000) \
     --query "events[*].message" --output text
   ```

2. **Check for successful instance creation:**
   ```bash
   aws logs filter-log-events \
     --log-group-name /aws/lambda/HousePlannerEnsureInstance \
     --filter-pattern "Created new instance" \
     --start-time $(date -d '1 hour ago' +%s000)
   ```

3. **Check for errors:**
   ```bash
   aws logs filter-log-events \
     --log-group-name /aws/lambda/HousePlannerEnsureInstance \
     --filter-pattern "ERROR" \
     --start-time $(date -d '1 hour ago' +%s000)
   ```

---

### Lambda Timeout Issues

The ensure Lambda has a 120-second timeout to allow for:
- EC2 boot time (~20-40 seconds to reach "running")
- Target group creation
- ALB rule creation

If timing out:
```bash
# Check Lambda configuration
aws lambda get-function-configuration \
  --function-name HousePlannerEnsureInstance \
  --query "{Timeout:Timeout,Memory:MemorySize}"
```

Expected: `{"Timeout": 120, "Memory": 256}`

---

## 4. Delete Lambda Troubleshooting

### Verify EventBridge Rule

```bash
aws events describe-rule --name HousePlannerUserDeletedRule
```

Should show:
- State: `ENABLED`
- EventPattern matching `AdminDeleteUser`

---

### Check Rule Target

```bash
aws events list-targets-by-rule --rule HousePlannerUserDeletedRule
```

Should target `HousePlannerDeleteUserResources` Lambda.

---

### Test Delete Lambda Manually

**⚠️ Only for testing - will delete user's resources!**

```bash
# Get user sub first
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username "test@example.com" \
  --query "UserAttributes[?Name=='sub'].Value | [0]" --output text)

# Invoke delete Lambda with test event
aws lambda invoke \
  --function-name HousePlannerDeleteUserResources \
  --payload "{\"detail\":{\"additionalEventData\":{\"sub\":\"$USER_SUB\"}}}" \
  --cli-binary-format raw-in-base64-out \
  response.json

cat response.json
```

---

### Verify Cleanup Completed

After deleting a user:

1. **Check EC2 instance terminated:**
   ```bash
   aws ec2 describe-instances \
     --filters "Name=tag:OwnerSub,Values=$USER_SUB" \
     --query "Reservations[*].Instances[*].{Id:InstanceId,State:State.Name}"
   ```
   Expected: Empty or state=terminated

2. **Check ALB rules cleaned up:**
   ```bash
   aws elbv2 describe-rules \
     --listener-arn $(aws elbv2 describe-listeners \
       --load-balancer-arn $(aws elbv2 describe-load-balancers \
         --names HousePlannerALB --query "LoadBalancers[0].LoadBalancerArn" --output text) \
       --query "Listeners[?Port==\`443\`].ListenerArn | [0]" --output text) \
     --query "Rules[*].Priority"
   ```
   User's rule should be removed.

3. **Check target group deleted:**
   ```bash
   aws elbv2 describe-target-groups \
     --query "TargetGroups[?starts_with(TargetGroupName, 'u-')].TargetGroupName"
   ```
   User's target group should be removed.

---

## 5. Manual Cleanup (If Lambda Fails)

If the delete Lambda fails, manually clean up:

```bash
# 1. Get user's target group
TG_ARN=$(aws elbv2 describe-target-groups \
  --query "TargetGroups[?starts_with(TargetGroupName, 'u-')].TargetGroupArn | [0]" --output text)

# 2. Find and delete the listener rule (replace RULE_ARN)
aws elbv2 delete-rule --rule-arn RULE_ARN

# 3. Deregister targets and delete target group
aws elbv2 delete-target-group --target-group-arn "$TG_ARN"

# 4. Terminate EC2 instance
aws ec2 terminate-instances --instance-ids INSTANCE_ID
```

---

## 6. Success Criteria

### Ensure Lambda
- ✅ Creates EC2 instance with correct tags
- ✅ Waits for "running" state before registering
- ✅ Creates target group with `/health` health check
- ✅ Creates ALB routing rule with OIDC auth
- ✅ Returns Set-Cookie header with routing cookie

### Delete Lambda
- ✅ Triggered by CloudTrail AdminDeleteUser event
- ✅ Terminates user's EC2 instance
- ✅ Deletes user's ALB routing rule
- ✅ Deletes user's target group

---

## 7. Related Documentation

- `Follow-Up-Directions-01-Cognito.md` - User management
- `Follow-Up-Directions-03-EC2.md` - EC2 instance diagnostics
- `Follow-Up-Directions-04-ALB.md` - ALB and routing issues
