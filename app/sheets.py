"""Google Sheets I/O via a service account: read the source sheet, write the
destination sheet. Auth = a service-account key (JSON string or file path); the
service account's email must be shared on each sheet (Viewer on source, Editor
on destination)."""
from __future__ import annotations

import json

from . import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SheetsError(RuntimeError):
    pass


def _client():
    raw = config.GOOGLE_SERVICE_ACCOUNT_JSON
    if not raw:
        raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as e:  # pragma: no cover
        raise SheetsError(f"Google libraries not installed: {e}") from e
    try:
        info = json.loads(raw) if raw.strip().startswith("{") else json.load(open(raw))
    except (json.JSONDecodeError, OSError) as e:
        raise SheetsError(f"Invalid service-account JSON: {e}") from e
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _open_ws(sheet_id: str, tab: str):
    if not sheet_id:
        raise SheetsError("No sheet ID configured")
    try:
        sh = _client().open_by_key(sheet_id)
        return sh.worksheet(tab) if tab else sh.sheet1
    except Exception as e:  # noqa: BLE001 — surface gspread/API errors cleanly
        raise SheetsError(f"Could not open sheet {sheet_id}: {e}") from e


def read_source_tabs() -> list[tuple[str, list[list[str]]]]:
    """Read the source spreadsheet's tabs as (tab_title, rows).

    GOOGLE_SOURCE_TAB:
      * blank            -> read ALL tabs (e.g. websites / meta ads / other)
      * "A" or "A, B"    -> read only those named tabs
    Each tab is column-mapped independently, so different per-source layouts work.
    """
    if not config.GOOGLE_SOURCE_SHEET_ID:
        raise SheetsError("No source sheet configured")
    try:
        sh = _client().open_by_key(config.GOOGLE_SOURCE_SHEET_ID)
    except Exception as e:  # noqa: BLE001
        raise SheetsError(f"Could not open source sheet: {e}") from e

    cfg = (config.GOOGLE_SOURCE_TAB or "").strip()
    try:
        if cfg:
            names = [t.strip() for t in cfg.split(",") if t.strip()]
            worksheets = [sh.worksheet(n) for n in names]
        else:
            worksheets = sh.worksheets()
        return [(ws.title, ws.get_all_values()) for ws in worksheets]
    except Exception as e:  # noqa: BLE001
        raise SheetsError(f"Could not read source tabs: {e}") from e


def append_dest_rows(header: list[str], rows: list[list]) -> int:
    """Append rows to the destination sheet, writing the header first if empty."""
    ws = _open_ws(config.GOOGLE_DEST_SHEET_ID, config.GOOGLE_DEST_TAB)
    try:
        existing = ws.get_all_values()
        if not existing:
            ws.append_row(header, value_input_option="RAW")
        if rows:
            ws.append_rows(rows, value_input_option="RAW")
    except Exception as e:  # noqa: BLE001
        raise SheetsError(f"Could not write destination sheet: {e}") from e
    return len(rows)
