# Follow-Up-Directions-03-EC2.md
## EC2 Instance Diagnostics & Troubleshooting

This document covers EC2 instance-level verification for House Planner user workspaces.

**Related Files:**
- `house_planner/house_planner_compute_stack.py` - EC2 launch template and user data
- `service/streamlit.service` - Streamlit systemd service
- `service/idle-shutdown.service` - Idle shutdown service
- `service/idle-shutdown.timer` - Idle shutdown timer
- `scripts/idle_shutdown.sh` - Idle shutdown script
- `common.py` - Shared HTML for warm-up pages

---

## 1. Find User's EC2 Instance

### By User Email

```bash
# Get user's sub from Cognito
USER_SUB=$(aws cognito-idp admin-get-user \
  --user-pool-id "$COGNITO_USER_POOL_ID" \
  --username "user@example.com" \
  --query "UserAttributes[?Name=='sub'].Value | [0]" --output text)

# Find instance by OwnerSub tag
aws ec2 describe-instances \
  --filters "Name=tag:OwnerSub,Values=$USER_SUB" \
  --query "Reservations[*].Instances[*].{Id:InstanceId,State:State.Name,IP:PublicIpAddress}" \
  --output table
```

### All House Planner Instances

```bash
aws ec2 describe-instances \
  --filters "Name=tag:Purpose,Values=HousePlannerUser" \
  --query "Reservations[*].Instances[*].{Id:InstanceId,State:State.Name,IP:PublicIpAddress,Owner:Tags[?Key=='OwnerSub'].Value|[0]}" \
  --output table
```

---

## 2. Connect to Instance (SSH)

```bash
INSTANCE_ID="i-xxxxxxxxxx"

ssh -i houseplanner-key.pem ec2-user@$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)
```

---

## 3. Verify Streamlit Application

### Check Streamlit Service

```bash
sudo systemctl status streamlit.service
```

Expected:
- `Active: active (running)`
- Main process is Streamlit

### View Streamlit Logs

```bash
sudo journalctl -u streamlit.service -f
```

Or check log file:

```bash
tail -100 /home/ec2-user/logs/streamlit.log
```

### Verify Streamlit Is Listening

```bash
ss -tlnp | grep 8501
```

Expected: Listener on `127.0.0.1:8501`

### Test Streamlit Directly

```bash
curl -I http://127.0.0.1:8501
```

Expected: `HTTP/1.1 200 OK`

---

## 4. Verify nginx Reverse Proxy

### Check nginx Service

```bash
sudo systemctl status nginx
```

### Test nginx Locally

```bash
curl -I http://localhost
```

Expected: `HTTP/1.1 200 OK` or redirect

### Check nginx Health Endpoint

```bash
curl http://localhost/health
```

Expected: `OK`

### View nginx Logs

```bash
sudo tail -50 /var/log/nginx/access.log
sudo tail -50 /var/log/nginx/error.log
```

### Check nginx Configuration

```bash
sudo cat /etc/nginx/conf.d/streamlit.conf
```

---

## 5. Verify Idle Shutdown

### Check Timer Status

```bash
sudo systemctl list-timers | grep idle-shutdown
```

Expected: Shows next run time

### Check Service Status

```bash
sudo systemctl status idle-shutdown.service
```

### View Idle Shutdown Logs

```bash
sudo journalctl -u idle-shutdown.service --since "1 hour ago"
```

### Follow Logs Live

```bash
sudo journalctl -u idle-shutdown.service -f
```

Expected log patterns:
- `[START] Idle shutdown monitor started`
- `[ACTIVE] Recent nginx traffic detected` - when app is in use
- `[IDLE] … elapsed` - when no traffic
- `[SHUTDOWN] Idle limit reached` - before instance stops

### Check Last Activity Time

```bash
NOW=$(date +%s)
LAST_HIT=$(stat -c %Y /var/log/nginx/access.log)
echo "Seconds since last activity: $(( NOW - LAST_HIT ))"
```

---

## 6. Troubleshooting Common Issues

### Streamlit Not Running

1. **Check service status:**
   ```bash
   sudo systemctl status streamlit.service
   ```

2. **Check for errors:**
   ```bash
   sudo journalctl -u streamlit.service -n 100
   ```

3. **Restart service:**
   ```bash
   sudo systemctl restart streamlit.service
   ```

4. **Check Python environment:**
   ```bash
   sudo -u ec2-user /home/ec2-user/HousingPlanner/.venv/bin/python --version
   ```

5. **Check dependencies:**
   ```bash
   sudo -u ec2-user /home/ec2-user/HousingPlanner/.venv/bin/pip list | grep streamlit
   ```

---

### nginx Not Working

1. **Check configuration:**
   ```bash
   sudo nginx -t
   ```

2. **Check logs:**
   ```bash
   sudo tail -50 /var/log/nginx/error.log
   ```

3. **Restart nginx:**
   ```bash
   sudo systemctl restart nginx
   ```

---

### Secrets Manager Issues

If Streamlit can't get API keys:

1. **Check IAM role:**
   ```bash
   curl -s http://169.254.169.254/latest/meta-data/iam/security-credentials/
   ```

2. **Test secrets access:**
   ```bash
   aws secretsmanager get-secret-value \
     --secret-id houseplanner/ors_api_key \
     --query SecretString --output text
   ```

---

### Instance Won't Stop (Idle Shutdown)

1. **Check timer is running:**
   ```bash
   sudo systemctl status idle-shutdown.timer
   ```

2. **Check script exists:**
   ```bash
   ls -l /usr/local/bin/idle_shutdown.sh
   ```

3. **Check nginx access log exists:**
   ```bash
   ls -l /var/log/nginx/access.log
   ```

4. **Manually trigger check:**
   ```bash
   sudo /usr/local/bin/idle_shutdown.sh
   ```

---

## 7. View Cloud-Init Logs (First Boot Issues)

If instance failed during initial setup:

```bash
sudo cat /var/log/cloud-init-output.log
```

Look for:
- `[FATAL]` - Critical failures
- `[CHECK]` - Setup steps
- `[STREAMLIT]` - Application setup

---

## 8. Restart Instance After Idle Shutdown

Instances stopped by idle shutdown are restarted when user logs in again.
The ensure Lambda handles this automatically.

To manually start:

```bash
aws ec2 start-instances --instance-ids "$INSTANCE_ID"
```

After restart, Streamlit starts automatically via systemd.

---

## 9. Success Criteria

- ✅ SSH access works
- ✅ Streamlit service running on port 8501
- ✅ nginx service running on port 80
- ✅ `/health` endpoint returns `OK`
- ✅ Idle shutdown timer enabled
- ✅ Instance stops after inactivity period
- ✅ Instance restarts when user returns

---

## 10. Related Documentation

- `Follow-Up-Directions-01-Cognito.md` - User management
- `Follow-Up-Directions-02-Lambda.md` - Lambda troubleshooting
- `Follow-Up-Directions-04-ALB.md` - ALB and routing issues
