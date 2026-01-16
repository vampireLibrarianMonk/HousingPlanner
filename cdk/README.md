# House Planner — DNS & HTTPS Setup (Minimal)

This project uses **Route53 + CloudFront + ACM** to expose a secure browser page that starts the EC2-backed Streamlit app.

This document lists **only what is required**, in the **correct order**.

---

## What You Must Create First (One-Time)

### Option A (Recommended): Register the domain in Route 53

1. Go to **Route 53 → Registered domains**
2. Click **Register domain**
3. Register your domain (e.g. `yourdomain.com`)

AWS will automatically:
- Create the public hosted zone
- Configure nameservers
- Attach the domain to Route 53 DNS

No additional DNS setup is required.

---

### Option B: Use an external registrar (not recommended)

If your domain is registered outside AWS:

1. Create a **public hosted zone** in Route 53 for `yourdomain.com`
2. Copy the hosted zone **NS (nameserver)** records
3. Update the domain’s nameservers at your registrar
4. Wait for DNS propagation

---

## What You Configure in CDK (Before Deploy)

Edit `cdk/house_planner/stack.py`:

```python
domain_name = "planner.yourdomain.com"
hosted_zone_name = "yourdomain.com"
```

These values **must match** the hosted zone you created.

---

## Deploy Order

# 1. Get and store your ip address
```bash
export MY_PUBLIC_IP=$(curl -s https://checkip.amazonaws.com)/32
echo $MY_PUBLIC_IP
```

# 2. Create and insert the qualifier
```bash
QUALIFIER=$(git remote get-url origin | tr -d '\n' | sha256sum | cut -c1-10); \
jq --arg q "$QUALIFIER" '.context["@aws-cdk/core:bootstrapQualifier"]=$q' cdk.json > cdk.json.tmp && mv cdk.json.tmp cdk.json
```

# 3. Create a key pair for administering the ec2 instance
```bash
aws ec2 create-key-pair \
  --key-name houseplanner-key \
  --query 'KeyMaterial' \
  --output text > houseplanner-key.pem

chmod 400 houseplanner-key.pem
```

# 4. Activate the virtual environment and install the python libraries
```bash
source .venv/bin/activate
pip install -r requirements
```

# 5. Clean old context (important)
```bash
cdk context --clear
rm -rf cdk.out
```

# 6. Bootstrap (now works)
```bash
cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/us-east-1
```

# 7. Synth
```bash
cdk synth
```

# 8. Deploy
```bash
cdk deploy \
  -c ssh_cidr=$MY_PUBLIC_IP \
  -c ors_api_key=sk_xxx \
  -c google_maps_api_key=AIza_xxx
```

CDK will automatically:
- Request an ACM certificate (us-east-1)
- Validate it via Route53 DNS
- Create a CloudFront distribution
- Create a Route53 alias record: `planner.yourdomain.com → CloudFront`
- Expose the HTTPS status/start page

---

## After Deployment

Visit:

```
https://planner.yourdomain.com
```

You should see:
- Application status
- A button to start the EC2 instance
- A link to the Streamlit app once EC2 is running

---

## Notes

- You **must** create the Route53 hosted zone **before** running CDK
- ACM certificate creation will fail if DNS is not delegated correctly
- No ALB, no ECS, no containers are involved
- EC2 and Streamlit are unchanged by DNS/HTTPS setup