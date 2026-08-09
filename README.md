# irctc-pnr-notifier
Notify when your Waiting List moves in IRCTC Trains.

This project runs daily on GitHub Actions, checks your PNRs using multiple providers (with fallback), and sends alerts via `ntfy.sh` and/or Resend email when:
- waiting list moves up or down,
- ticket becomes confirmed,
- chart is prepared.

## Setup

Add repository secrets in **Settings → Secrets and variables → Actions**:

### Required
- `PNR_LIST_JSON`: JSON array of all PNR entries.

### Notification (optional, one or both)
- `NTFY_TOPIC` (repository variable)
- `NTFY_TOKEN` (optional)
- `RESEND_API_KEY`
- `RESEND_FROM_EMAIL` (repository variable)
- `RESEND_TO_EMAIL` (repository variable)

### Provider config
- `EMT_HEADERS_JSON` (optional; JSON object of extra headers if needed)
- `RAPIDAPI_KEYS_JSON` (optional; JSON array of keys for auto-rotation)
- `RAPIDAPI_KEY` (optional single key fallback)

RapidAPI endpoint used:
- `GET https://irctc1.p.rapidapi.com/api/v3/getPNRStatus?pnrNumber=<PNR>`

## PNR secret format

Store all personal values in `PNR_LIST_JSON` only.

```json
[
  {
    "id": "trip-home",
    "pnr": "1234567890",
    "emt_pnr_token": "+TtYbuHlzor9vXDaK68LoQ=="
  },
  {
    "id": "trip-office",
    "pnr": "0987654321"
  }
]
```

- `id`: label used in notifications.
- `pnr`: plain PNR for fallback providers.
- `emt_pnr_token`: encrypted value for EaseMyTrip endpoint (recommended).

## Run locally with uv

```bash
uv run python src/pnr_notifier.py
```

The script auto-loads `.env` from repo root. To use a different env file:

```bash
ENV_FILE=.env.local uv run python src/pnr_notifier.py
```

For key rotation, set either:
- `RAPIDAPI_KEYS_JSON=["key_a","key_b","key_c"]` (preferred), or
- `RAPIDAPI_KEY=single_key`.

Optional repository variable:
- `STATE_FILE` (defaults to `state/pnr_state.json`)

State is persisted in `state/pnr_state.json` and is committed by the workflow after each run.
