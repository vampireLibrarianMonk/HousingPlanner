# House Planner Application

This document covers local app usage and feature guidance. It does not include CDK deployment steps.

## Prerequisites

- Python 3.12
- A virtual environment tool (venv is assumed)
- AWS CLI configured if you plan to use AWS-backed features locally

## Install and Run

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

## Environment Configuration

The app loads environment variables from `app/.env` and from your shell.

Required variables for local runs:

- `ORS_API_KEY`
- `GOOGLE_MAPS_API_KEY`
- `WAZE_API_KEY`
- `DOOR_PROFIT_API_KEY`
- `ACCOUNT_ID`
- `STORAGE_BUCKET_PREFIX`
- `HOUSE_PLANNER_OWNER_SUB`

Example `app/.env`:

```bash
ORS_API_KEY=<open-route-service-key>
GOOGLE_MAPS_API_KEY=<google-maps-key>
WAZE_API_KEY=<openwebninja-waze-key>
DOOR_PROFIT_API_KEY=<door-profit-key>
ACCOUNT_ID=<your-aws-account-id>
STORAGE_BUCKET_PREFIX=houseplanner-<your-aws-account-id>
HOUSE_PLANNER_OWNER_SUB=<cognito-user-sub>
WAZE_API_KEY=<waze-api-key>
FCC_API_KEY=<fcc-api-key>
```

## Application Sections

### Home Buying Checklist and Notes

Use this section to track tasks and notes while evaluating a property.

### Document Vetting

Upload HOA PDFs and run red-flag analysis. Follow-up questions are grounded in the document. The query history shows the source document, pages, and quoted text.

### Mortgage Analysis

Model monthly costs and adjust assumptions such as interest rate, taxes, and insurance.

### Locations and Commute

Add locations, compute commute estimates, and inspect route details and travel time assumptions.

### Neighborhood Analysis

Review neighborhood context and overlays powered by the DoorProfit integration.

### Sun and Light

Inspect sun exposure details for the property, including seasonal variability.

### Disaster Risk

Review hazard layers, historical events, and related disclosures.

### Profile Manager

Manage saved profiles and stored property context.

### Assistant

Use the floating assistant for quick help and guidance inside the app.

## Troubleshooting

### Missing environment variables

If the app reports missing environment variables, confirm `app/.env` or exported values contain the required keys.

### Textract or S3 errors

Ensure AWS CLI credentials are configured and that `STORAGE_BUCKET_PREFIX` and `HOUSE_PLANNER_OWNER_SUB` are set. The app will create a per-user bucket if it does not exist.