# Deploy-Check-Addendum.md
## EC2 Instance-Level Verification (Idle Shutdown & Runtime Health)

This addendum validates **instance-local behavior** that cannot be verified via Lambda or API Gateway:
- EC2 Instance Connect access
- idle-shutdown service status
- timer / process health
- Streamlit runtime status

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

## Resolve Availability Zone & Public IP

```bash
aws ec2 describe-instances \
--instance-ids "$INSTANCE_ID" \
--query "Reservations[0].Instances[0].[Placement.AvailabilityZone,PublicIpAddress]" \
--output text
```

Expected:
- AZ present
- Public IP present (instance is in a public subnet)

---

## Connect to the Instance (EC2 Instance Connect)

```bash
ssh -i houseplanner-key.pem ec2-user@$(aws ec2 describe-instances \
--instance-ids "$INSTANCE_ID" \
--query "Reservations[0].Instances[0].PublicIpAddress" \
--output text)
```

---

## Verify idle-shutdown systemd Service

Once logged into the instance:

```bash
sudo systemctl status idle-shutdown.service
```

Example Expected Output:
```bash
● idle-shutdown.service - Idle Shutdown Watchdog
     Loaded: loaded (/etc/systemd/system/idle-shutdown.service; enabled; preset: disabled)
     Active: active (running) since Wed 2026-01-14 17:07:55 UTC; 6min ago
   Main PID: 1727 (bash)
      Tasks: 2 (limit: 2117)
     Memory: 1.2M
        CPU: 38ms
     CGroup: /system.slice/idle-shutdown.service
             ├─1727 bash /usr/local/bin/idle_shutdown.sh
             └─1983 sleep 60
```

---

## Verify Service Is Enabled on Boot

```bash
sudo systemctl is-enabled idle-shutdown.service
```

Expected:
```
enabled
```

---

## Verify idle-shutdown Script Location & Permissions

```bash
ls -l /usr/local/bin/idle_shutdown.sh
```

Expected:
- File exists
- Executable bit set (`-rwxr-xr-x`)

---

## Inspect idle-shutdown Logs (recent activity)

```bash
sudo journalctl -u idle-shutdown.service --since "1 hour ago"
```

Expected:
- Periodic log entries
- No crash / restart loops

---

## Confirm idle-shutdown Process Is Running

```bash
ps aux | grep idle_shutdown | grep -v grep
```

Expected:
- One running process

---

## Verify Streamlit Application Is Running

```bash
ss -ltnp | grep 8501
```

Expected:
- Listener on `0.0.0.0:8501` or `:::8501`
- Owned by Python process

---

## Verify Application via Local Curl

```bash
curl -I http://localhost:8501
```

Expected:
```
HTTP/1.1 200 OK
```

---

## Optional: Simulate Inactivity (manual test)

1. Stop all user interaction
2. Wait for idle timeout (~1 hour)
3. Observe instance shutdown from another terminal:

```bash
aws ec2 describe-instances   --instance-ids "$INSTANCE_ID"   --query "Reservations[0].Instances[0].State.Name"
```

Expected:
```
stopped
```

---

## Success Criteria

- EC2 Instance Connect works
- idle-shutdown service is running and enabled
- Script is executable and logging
- Streamlit app is reachable locally
- Instance shuts down automatically after inactivity

---

## Notes

- Do **not** manually stop the idle-shutdown service except for debugging
- If the service is inactive, redeploying the stack will reinstall and re-enable it
- All instance-level checks assume Amazon Linux 2023 defaults
