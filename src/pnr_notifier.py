import base64
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

EMT_URL = "https://railways.easemytrip.com/Train/PnrchkStatus"
RAPIDAPI_URL = "https://irctc1.p.rapidapi.com/api/v3/getPNRStatus"
RAPIDAPI_HOST = "irctc1.p.rapidapi.com"
RESEND_URL = "https://api.resend.com/emails"


def debug_enabled() -> bool:
    return os.getenv("DEBUG_HTTP", "1").strip().lower() not in {"0", "false", "no", "off"}


def debug_http(name: str, response: httpx.Response) -> None:
    if not debug_enabled():
        return
    body = response.text
    if len(body) > 2000:
        body = body[:2000] + "...<truncated>"
    print(
        f"[debug] {name} -> status={response.status_code} url={response.url}\n"
        f"[debug] {name} response: {body}",
        file=sys.stderr,
    )


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ[key] = value


@dataclass
class ProviderResult:
    provider: str
    ok: bool
    data: dict[str, Any] | None = None
    error: str | None = None


@dataclass
class NormalizedStatus:
    pnr_id: str
    pnr: str
    provider: str
    checked_at: str
    chart_prepared: bool
    confirmed: bool
    current_statuses: list[str]
    waiting_numbers: list[int]
    raw_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pnr_id": self.pnr_id,
            "pnr": self.pnr,
            "provider": self.provider,
            "checked_at": self.checked_at,
            "chart_prepared": self.chart_prepared,
            "confirmed": self.confirmed,
            "current_statuses": self.current_statuses,
            "waiting_numbers": self.waiting_numbers,
            "raw_summary": self.raw_summary,
        }


def parse_json_env(name: str, default: Any = None) -> Any:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {name}: {exc}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"pnrs": {}}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"pnrs": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def extract_waiting_number(status: str) -> int | None:
    s = status.upper().replace(" ", "")
    if "WL" not in s:
        return None
    m = re.search(r"WL(\d+)$", s) or re.search(r"WL[^0-9]*(\d+)", s)
    if not m:
        return None
    return int(m.group(1))


def is_confirmed_status(status: str) -> bool:
    u = status.upper()
    return any(k in u for k in ("CNF", "CONFIRM"))


def encrypt_emt_pnr(pnr: str) -> str:
    data = pnr.encode("utf-8")
    padder = sym_padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    key = b"8080808080808080"
    iv = b"8080808080808080"
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ct = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ct).decode("utf-8")


def normalize_emt(entry: dict[str, Any], payload: dict[str, Any]) -> NormalizedStatus:
    passengers = payload.get("passengerList") or []
    statuses = [str(p.get("currentStatus", "")).strip() for p in passengers if p.get("currentStatus")]
    waiting_numbers = [n for n in (extract_waiting_number(s) for s in statuses) if n is not None]

    chart_text = str(payload.get("chartStatus", "")).strip().lower()
    chart_prepared = "prepared" in chart_text and "not prepared" not in chart_text

    confirmed = bool(statuses) and all(is_confirmed_status(s) for s in statuses)
    raw_summary = (
        f"Train {payload.get('trainNumber', '')} {payload.get('trainName', '')} | "
        f"Chart: {payload.get('chartStatus', 'NA')} | Current: {', '.join(statuses) if statuses else 'NA'}"
    )

    return NormalizedStatus(
        pnr_id=str(entry.get("id") or entry.get("pnr")),
        pnr=str(entry.get("pnr", "")),
        provider="easemytrip",
        checked_at=now_iso(),
        chart_prepared=chart_prepared,
        confirmed=confirmed,
        current_statuses=statuses,
        waiting_numbers=waiting_numbers,
        raw_summary=raw_summary,
    )


def normalize_rapidapi(entry: dict[str, Any], payload: dict[str, Any]) -> NormalizedStatus:
    data = payload.get("data", payload)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError(f"rapidapi payload data is not an object (got {type(data).__name__})")

    passengers = (
        data.get("passengerList")
        or data.get("passengers")
        or data.get("passenger_info")
        or data.get("passengerInfo")
        or data.get("PassengerStatus")
        or data.get("passengerDetailsDTO")
        or []
    )

    statuses: list[str] = []
    for p in passengers:
        for key in (
            "current_status",
            "currentStatus",
            "currentStatusDetails",
            "CurrentStatus",
            "seatStts",
            "seatStatus",
            "CurrentStatusNew",
        ):
            if p.get(key):
                statuses.append(str(p[key]).strip())
                break

    if not statuses:
        for key in (
            "current_status",
            "currentStatus",
            "currentStatusDetails",
            "CurrentStatus",
            "seatStts",
            "seatStatus",
            "CurrentStatusNew",
        ):
            if data.get(key):
                statuses.append(str(data[key]).strip())
                break

    waiting_numbers = [n for n in (extract_waiting_number(s) for s in statuses) if n is not None]

    chart_raw: Any | None = None
    for key in ("chart_prepared", "chartStatus", "chart_status", "ChartPrepared", "chartStts"):
        if key in data and data.get(key) is not None:
            chart_raw = data.get(key)
            break
    chart_value = str(chart_raw) if chart_raw is not None else ""
    chart_l = chart_value.lower()
    chart_prepared = chart_l in {"y", "yes", "true", "1", "prepared"} or (
        "prepared" in chart_l and "not" not in chart_l
    )

    confirmed = bool(statuses) and all(is_confirmed_status(s) for s in statuses)
    raw_summary = (
        f"Train {data.get('train_no', data.get('trainNumber', data.get('TrainNo', data.get('trainNum', ''))))} "
        f"{data.get('train_name', data.get('trainName', data.get('TrainName', data.get('trainName', ''))))} | "
        f"Chart: {chart_value or 'NA'} | Current: {', '.join(statuses) if statuses else 'NA'}"
    )

    return NormalizedStatus(
        pnr_id=str(entry.get("id") or entry.get("pnr")),
        pnr=str(entry.get("pnr", "")),
        provider="rapidapi",
        checked_at=now_iso(),
        chart_prepared=chart_prepared,
        confirmed=confirmed,
        current_statuses=statuses,
        waiting_numbers=waiting_numbers,
        raw_summary=raw_summary,
    )


def call_emt(client: httpx.Client, entry: dict[str, Any], headers: dict[str, str]) -> ProviderResult:
    token = str(entry.get("emt_pnr_token") or "").strip()
    pnr = str(entry.get("pnr") or "").strip()
    if not token and not pnr:
        return ProviderResult(provider="easemytrip", ok=False, error="Missing emt_pnr_token/pnr")

    candidates: list[tuple[str, str]] = []
    if token:
        candidates.append(("emt_pnr_token", token))
    else:
        encrypted_pnr = encrypt_emt_pnr(pnr)
        candidates.extend(
            [
                ("pnr_aes_cbc", encrypted_pnr),
                ("pnr_plain", pnr),
            ]
        )

    # Deduplicate while preserving order.
    deduped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, value in candidates:
        if value not in seen:
            seen.add(value)
            deduped.append((label, value))

    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers)

    errors: list[str] = []
    for idx, (label, candidate) in enumerate(deduped, start=1):
        try:
            if debug_enabled():
                print(
                    f"[debug] easemytrip request pnr_id={entry.get('id') or entry.get('pnr')} "
                    f"token_source={label} attempt={idx}/{len(deduped)}",
                    file=sys.stderr,
                )
            resp = client.post(EMT_URL, headers=req_headers, json={"pnrNumber": candidate}, timeout=20)
            debug_http("easemytrip", resp)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                errors.append(f"{label}: Unexpected response type: {type(data).__name__}")
                continue
            if data.get("errorMessage"):
                errors.append(f"{label}: {data.get('errorMessage')}")
                continue
            return ProviderResult(provider="easemytrip", ok=True, data=data)
        except Exception as exc:  # explicit surfacing in event message
            errors.append(f"{label}: {exc}")

    return ProviderResult(provider="easemytrip", ok=False, error=" | ".join(errors))


def call_rapidapi(client: httpx.Client, entry: dict[str, Any]) -> ProviderResult:
    pnr = entry.get("pnr")
    if not pnr:
        return ProviderResult(provider="rapidapi", ok=False, error="Missing plain pnr")

    keys_raw = parse_json_env("RAPIDAPI_KEYS_JSON", default=None)
    api_keys: list[str] = []
    if isinstance(keys_raw, list):
        api_keys.extend(str(k).strip() for k in keys_raw if str(k).strip())

    one_key = os.getenv("RAPIDAPI_KEY", "").strip()
    if one_key:
        api_keys.append(one_key)

    if not api_keys:
        return ProviderResult(provider="rapidapi", ok=False, error="RAPIDAPI_KEYS_JSON/RAPIDAPI_KEY not configured")

    host = RAPIDAPI_HOST
    errors: list[str] = []

    for idx, api_key in enumerate(api_keys, start=1):
        headers = {
            "X-RapidAPI-Key": api_key,
            "X-RapidAPI-Host": host,
            "Content-Type": "application/json",
        }
        try:
            if debug_enabled():
                print(
                    f"[debug] rapidapi request pnr_id={entry.get('id') or entry.get('pnr')} host={host} "
                    f"url={RAPIDAPI_URL} key_index={idx}/{len(api_keys)}",
                    file=sys.stderr,
                )
            resp = client.get(RAPIDAPI_URL, headers=headers, params={"pnrNumber": str(pnr)}, timeout=20)
            debug_http("rapidapi", resp)

            parsed: dict[str, Any] | None = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    parsed = body
            except Exception:
                parsed = None

            msg = str((parsed or {}).get("message", "")).lower()
            key_limited = resp.status_code in {401, 403, 429} or any(
                x in msg for x in ("quota", "limit", "exceeded", "too many")
            )

            if resp.status_code >= 400:
                errors.append(f"key#{idx}: HTTP {resp.status_code}")
                if key_limited and idx < len(api_keys):
                    continue
                resp.raise_for_status()

            if not isinstance(parsed, dict):
                errors.append(f"key#{idx}: Unexpected response type")
                continue

            if parsed.get("status") is False:
                errors.append(f"key#{idx}: {parsed.get('message', 'status=false')}")
                if key_limited and idx < len(api_keys):
                    continue
                continue

            return ProviderResult(provider="rapidapi", ok=True, data=parsed)
        except Exception as exc:
            errors.append(f"key#{idx}: {exc}")
            if idx < len(api_keys):
                continue

    return ProviderResult(provider="rapidapi", ok=False, error=" | ".join(errors))


def check_one_pnr(client: httpx.Client, entry: dict[str, Any], emt_headers: dict[str, str]) -> tuple[NormalizedStatus | None, list[str]]:
    errors: list[str] = []

    emt_res = call_emt(client, entry, emt_headers)
    if emt_res.ok and emt_res.data is not None:
        return normalize_emt(entry, emt_res.data), errors
    errors.append(f"easemytrip: {emt_res.error}")

    rapid_res = call_rapidapi(client, entry)
    if rapid_res.ok and rapid_res.data is not None:
        return normalize_rapidapi(entry, rapid_res.data), errors
    errors.append(f"rapidapi: {rapid_res.error}")

    return None, errors


def movement(old_waiting: list[int], new_waiting: list[int]) -> str | None:
    if not old_waiting or not new_waiting:
        return None
    old_total = sum(old_waiting)
    new_total = sum(new_waiting)
    if new_total < old_total:
        return "up"
    if new_total > old_total:
        return "down"
    return None


def build_events(old: dict[str, Any] | None, new: NormalizedStatus) -> list[dict[str, str]]:
    if not old:
        return []

    events: list[dict[str, str]] = []

    old_waiting = old.get("waiting_numbers", [])
    move = movement(old_waiting, new.waiting_numbers)
    if move == "up":
        events.append(
            {
                "kind": "wl-up",
                "title": f"PNR {new.pnr_id}: Waiting list moved up",
                "body": f"Waiting numbers {old_waiting} -> {new.waiting_numbers}\n{new.raw_summary}",
            }
        )
    elif move == "down":
        events.append(
            {
                "kind": "wl-down",
                "title": f"PNR {new.pnr_id}: Waiting list moved down",
                "body": f"Waiting numbers {old_waiting} -> {new.waiting_numbers}\n{new.raw_summary}",
            }
        )

    if not old.get("confirmed", False) and new.confirmed:
        events.append(
            {
                "kind": "confirmed",
                "title": f"PNR {new.pnr_id}: Ticket confirmed",
                "body": f"Status is now confirmed.\n{new.raw_summary}",
            }
        )

    if not old.get("chart_prepared", False) and new.chart_prepared:
        events.append(
            {
                "kind": "chart-prepared",
                "title": f"PNR {new.pnr_id}: Chart prepared",
                "body": f"Chart is prepared.\n{new.raw_summary}",
            }
        )

    old_statuses = old.get("current_statuses", [])
    if old_statuses != new.current_statuses and not events:
        events.append(
            {
                "kind": "status-changed",
                "title": f"PNR {new.pnr_id}: Status changed",
                "body": f"Current status changed\n{old_statuses} -> {new.current_statuses}\n{new.raw_summary}",
            }
        )

    return events


def notify_ntfy(client: httpx.Client, event: dict[str, str]) -> None:
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        return

    headers = {
        "Title": event["title"],
        "Priority": "default",
        "Tags": event["kind"],
    }
    token = os.getenv("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Authorization"] = f"Bearer {token}"

    resp = client.post(f"https://ntfy.sh/{topic}", headers=headers, content=event["body"], timeout=20)
    debug_http("ntfy", resp)
    resp.raise_for_status()


def notify_resend(client: httpx.Client, event: dict[str, str]) -> None:
    api_key = os.getenv("RESEND_API_KEY")
    from_email = os.getenv("RESEND_FROM_EMAIL")
    to_email = os.getenv("RESEND_TO_EMAIL")
    if not (api_key and from_email and to_email):
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "from": from_email,
        "to": [to_email],
        "subject": event["title"],
        "text": event["body"],
    }
    resp = client.post(RESEND_URL, headers=headers, json=payload, timeout=20)
    debug_http("resend", resp)
    resp.raise_for_status()


def notify_all(client: httpx.Client, event: dict[str, str]) -> None:
    errors: list[str] = []
    for notifier in (notify_ntfy, notify_resend):
        try:
            notifier(client, event)
        except Exception as exc:
            errors.append(str(exc))

    if errors:
        raise RuntimeError("Notification failure: " + " | ".join(errors))


def main() -> int:
    env_file = Path(os.getenv("ENV_FILE", ".env"))
    load_env_file(env_file)

    pnr_entries = parse_json_env("PNR_LIST_JSON", default=[])
    if not isinstance(pnr_entries, list) or not pnr_entries:
        print("PNR_LIST_JSON must be a non-empty JSON array", file=sys.stderr)
        return 2

    emt_headers = parse_json_env("EMT_HEADERS_JSON", default={})
    if not isinstance(emt_headers, dict):
        print("EMT_HEADERS_JSON must be a JSON object", file=sys.stderr)
        return 2

    state_file = Path(os.getenv("STATE_FILE", "state/pnr_state.json"))
    state = load_state(state_file)
    old_pnrs: dict[str, Any] = state.get("pnrs", {})
    new_pnrs: dict[str, Any] = {}

    all_events: list[dict[str, str]] = []

    with httpx.Client() as client:
        for entry in pnr_entries:
            pnr_id = str(entry.get("id") or entry.get("pnr") or "").strip()
            if not pnr_id:
                continue

            status, errors = check_one_pnr(client, entry, emt_headers)
            if not status:
                all_events.append(
                    {
                        "kind": "provider-failure",
                        "title": f"PNR {pnr_id}: all providers failed",
                        "body": "\n".join(errors),
                    }
                )
                old = old_pnrs.get(pnr_id)
                if old:
                    new_pnrs[pnr_id] = old
                continue

            old = old_pnrs.get(pnr_id)
            events = build_events(old, status)
            all_events.extend(events)
            new_pnrs[pnr_id] = status.to_dict()

        for event in all_events:
            notify_all(client, event)
            print(f"notified: {event['title']}")

    state["pnrs"] = new_pnrs
    state["updated_at"] = now_iso()
    save_state(state_file, state)

    print(f"checked {len(new_pnrs)} PNR(s), generated {len(all_events)} event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
