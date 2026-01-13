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

## 1️⃣ Resolve the EC2 Instance ID (programmatic)

```bash
INSTANCE_ID=$(aws ec2 describe-instances \
--filters "Name=tag:Name,Values=HousePlannerStack/HousePlannerEC2" \
--query "Reservations[].Instances[].InstanceId | [0]" \
--output text) && echo "Instance ID: $INSTANCE_ID"
```

---

## 2️⃣ Resolve Availability Zone & Public IP

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

## 3️⃣ Connect to the Instance (EC2 Instance Connect)

> Assumes you have already created a compatible key (EC2 Instance Connect does NOT support ed25519 keys reliably on AL2023.)
```bash
ssh-keygen -t rsa -b 4096 -f ~/.ssh/id_rsa
```

> Assumes Amazon Linux 2023 and EC2 Instance Connect support.

```bash
AZ=$(aws ec2 describe-instances \
--instance-ids "$INSTANCE_ID" \
--query "Reservations[0].Instances[0].Placement.AvailabilityZone" \
--output text)

aws ec2-instance-connect send-ssh-public-key \
--instance-id "$INSTANCE_ID" \
--availability-zone "$AZ" \
--instance-os-user ec2-user \
--ssh-public-key file://~/.ssh/id_rsa.pub 
```

Then SSH:

```bash
ssh ec2-user@$(aws ec2 describe-instances \
--instance-ids "$INSTANCE_ID" \
--query "Reservations[0].Instances[0].PublicIpAddress" \
--output text)
```

---

## 4️⃣ Verify idle-shutdown systemd Service

Once logged into the instance:

```bash
sudo systemctl status idle-shutdown.service
```

Expected:
- `Loaded: loaded`
- `Active: active (running)`
- No error lines

---

## 5️⃣ Verify Service Is Enabled on Boot

```bash
sudo systemctl is-enabled idle-shutdown.service
```

Expected:
```
enabled
```

---

## 6️⃣ Verify idle-shutdown Script Location & Permissions

```bash
ls -l /usr/local/bin/idle_shutdown.sh
```

Expected:
- File exists
- Executable bit set (`-rwxr-xr-x`)

---

## 7️⃣ Inspect idle-shutdown Logs (recent activity)

```bash
sudo journalctl -u idle-shutdown.service --since "1 hour ago"
```

Expected:
- Periodic log entries
- No crash / restart loops

---

## 8️⃣ Confirm idle-shutdown Process Is Running

```bash
ps aux | grep idle_shutdown | grep -v grep
```

Expected:
- One running process

---

## 9️⃣ Verify Streamlit Application Is Running

```bash
ss -ltnp | grep 8501
```

Expected:
- Listener on `0.0.0.0:8501` or `:::8501`
- Owned by Python process

---

## 🔟 Verify Application via Local Curl

```bash
curl -I http://localhost:8501
```

Expected:
```
HTTP/1.1 200 OK
```

---

## 11️⃣ Optional: Simulate Inactivity (manual test)

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

## ✅ Success Criteria

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
