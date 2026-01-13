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

From the `cdk/` directory:

```bash
source .venv/bin/activate
cdk bootstrap
cdk deploy
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