# Deploy-Check-Addendum.md
## EC2 Instance-Level Verification (Idle Shutdown & Runtime Health)

This addendum validates **instance-local behavior** that cannot be verified via Lambda or API Gateway:

- SSH access to the EC2 instance
- `idle-shutdown` systemd service status and logs
- Idle timer behavior (nginx-based activity detection)
- Streamlit runtime health

All steps use **AWS CLI + standard Linux commands**.
No CDK, Python, or build checks are required.

---

## Resolve the EC2 Instance ID (programmatic)

```bash
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters \
    "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
    "Name=instance-state-name,Values=running" \
  --query "Reservations[].Instances[].InstanceId | [0]" \
  --output text) && echo "Instance ID: $INSTANCE_ID"
```

---

## Connect to the Instance (SSH)

> Note: This stack uses **key-based SSH**, not EC2 Instance Connect.

```bash
ssh -i houseplanner-key.pem ec2-user@$(aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)
```

---

## Verify `idle-shutdown` systemd Service

Once logged into the instance:

```bash
sudo systemctl status idle-shutdown.service
```

Expected:
- Service is **loaded**
- Service is **active (running)**
- Main process is `/usr/local/bin/idle_shutdown.sh`

Example:
```text
● idle-shutdown.service - Idle Shutdown Monitor
   Loaded: loaded (/etc/systemd/system/idle-shutdown.service; enabled)
   Active: active (running)
   Main PID: 1759 (bash)
```

---

## Verify Service Is Enabled on Boot

```bash
sudo systemctl is-enabled idle-shutdown.service
```

Expected:
```text
enabled
```

---

## Verify `idle-shutdown` Script Location & Permissions

```bash
ls -l /usr/local/bin/idle_shutdown.sh
```

Expected:
- File exists
- Executable bit set (`-rwxr-xr-x`)
- Owned by `root`

---

## Inspect `idle-shutdown` Logs (journal)

The service logs **only** to systemd’s journal.

### View recent logs
```bash
sudo journalctl -u idle-shutdown.service --since "1 hour ago"
```

### Follow logs live (recommended while testing)
```bash
sudo journalctl -u idle-shutdown.service -f
```

Expected:
- `[START] Idle shutdown monitor started`
- `[ACTIVE] Recent nginx traffic detected` **when the app is used**
- `[IDLE] … elapsed` messages when no traffic occurs
- A final `[SHUTDOWN] Idle limit reached` message before stop

❗ If logs show continuous `[IDLE]` while the app is in use, nginx traffic is not reaching the instance.

---

## Confirm Activity Detection Mechanism (nginx-based)

The idle shutdown logic **does not inspect Streamlit sessions**.
It relies on **nginx access log modification time**.

Manual verification:

```bash
NOW=$(date +%s)
LAST_HIT=$(stat -c %Y /var/log/nginx/access.log)
echo $(( NOW - LAST_HIT ))
```

Expected:
- Small number (seconds) when actively using the app
- Large number when idle

This is the **authoritative signal** for user activity.

---

## Confirm `idle-shutdown` Process Is Running

```bash
ps aux | grep idle_shutdown | grep -v grep
```

Expected:
- Exactly one running process

---

## Verify Streamlit Application Is Running

Streamlit is bound to **127.0.0.1:8501** (behind nginx).

```bash
ss -ltnp | grep 8501
```

Expected:
- Listener on `127.0.0.1:8501`
- Owned by a Python process

---

## Verify Application Locally (bypassing nginx)

```bash
curl -I http://127.0.0.1:8501
```

Expected:
```text
HTTP/1.1 200 OK
```

---

## Optional: Verify nginx Reverse Proxy

```bash
sudo systemctl status nginx
```

```bash
curl -I http://localhost
```

Expected:
- HTTP `200` or `302`
- Confirms nginx → Streamlit proxy path

---

## Optional: Simulate Inactivity (End-to-End Test)

1. Close all browsers / tabs using the app
2. Wait for the idle timeout (default: ~1 hour)
3. From another terminal, check instance state:

```bash
aws ec2 describe-instances \
  --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].State.Name" \
  --output text
```

Expected:
```text
stopped
```

This confirms **instance-initiated shutdown** is working correctly.

---

## Success Criteria

- SSH access works
- `idle-shutdown` service is running and enabled
- Logs show ACTIVE when traffic exists and IDLE when not
- nginx access log updates during app usage
- Streamlit responds on `127.0.0.1:8501`
- Instance automatically transitions to `stopped` after inactivity

---

## Notes / Corrections

- ❌ Do **not** rely on Streamlit session endpoints for activity detection
- ❌ Do **not** use network byte counters for idleness
- ✅ nginx `access.log` mtime is the **single source of truth**
- Do **not** manually stop `idle-shutdown.service` except during debugging
- Redeploying the stack reinstalls and re-enables the service automatically
