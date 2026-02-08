# DoorProfit API Key (crime + offenders)

The app uses DoorProfit endpoints:

- `https://api.doorprofit.com/v1/crime?address=...&key=...`
- `https://api.doorprofit.com/v1/offenders?address=...&key=...`

In production (EC2 workspace instances), the Streamlit systemd unit expects the
key to be stored in **AWS Secrets Manager** under this secret name:

- `houseplanner/door_profit_api_key`

The unit file `service/streamlit.service` will read that secret and export it
as the environment variable:

- `DOOR_PROFIT_API_KEY`

## Create/update the secret

```bash
aws secretsmanager create-secret \
  --name houseplanner/door_profit_api_key \
  --secret-string 'dp_xxx' \
  --region us-east-1

# or if it already exists:
aws secretsmanager put-secret-value \
  --secret-id houseplanner/door_profit_api_key \
  --secret-string 'dp_xxx' \
  --region us-east-1
```

## Restart Streamlit on the instance

```bash
sudo systemctl daemon-reload
sudo systemctl restart streamlit.service
sudo journalctl -u streamlit.service -n 100 --no-pager
```

## Local development

For local dev, the repository’s `app/.env` can include:

```env
DOOR_PROFIT_API_KEY=dp_xxx
```

`app/app.py` loads it with `python-dotenv`.
