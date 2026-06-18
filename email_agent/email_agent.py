#!/usr/bin/env python3
"""
Small Gmail-backed email agent.

Examples:
  export OPENAI_API_KEY='your-openai-api-key'

  python3 email_agent.py google-auth
  python3 email_agent.py read --limit 5
  python3 email_agent.py send --to someone@example.com --subject "Hello" --body "Hi there"
  python3 email_agent.py event --title "Planning" --start "2026-04-30 14:00" \
      --end "2026-04-30 14:30" --attendee someone@example.com
  python3 email_agent.py agent "Summarize my latest unread email"

The script uses Google OAuth credentials in credentials.json/token.json for
Gmail, Calendar, Docs, and Sheets. It does not store account passwords.
"""

from __future__ import annotations

import argparse
import atexit
import asyncio
import base64
import contextlib
import email
import html as html_lib
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from a2a.server.agent_execution import RequestContext as A2ARequestContext
    from a2a.server.events import EventQueue as A2AEventQueue
    from a2a.server.tasks import TaskUpdater as A2ATaskUpdater
    from a2a.types import AgentCard as A2AAgentCard

try:
    from a2a.server.agent_execution import AgentExecutor as _AgentExecutorBase
    from a2a.server.apps import A2AStarletteApplication as _A2AStarletteApplication
    from a2a.server.request_handlers import DefaultRequestHandler as _DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore as _InMemoryTaskStore
    from a2a.server.tasks import TaskUpdater as _TaskUpdater
    from a2a.types import AgentCard as _AgentCard
    from a2a.types import DataPart as _DataPart
    from a2a.types import Part as _Part
    from a2a.types import TextPart as _TextPart
except ImportError as exc:
    A2A_IMPORT_ERROR: ImportError | None = exc
    _AgentExecutorBase = object
    _A2AStarletteApplication = None
    _DefaultRequestHandler = None
    _InMemoryTaskStore = None
    _TaskUpdater = None
    _AgentCard = None
    _DataPart = None
    _Part = None
    _TextPart = None
else:
    A2A_IMPORT_ERROR = None


def _agent_network_root() -> Path | None:
    start = Path(__file__).resolve().parent
    for candidate in [start, *start.parents]:
        if (candidate / "agent_directory_service").is_dir():
            return candidate
        nested = candidate / "AgenticNetwork"
        if (nested / "agent_directory_service").is_dir():
            return nested
    return None


def _default_email_a2a_card_path() -> Path:
    script_dir = Path(__file__).resolve().parent
    local_card = script_dir / "email_agent.card.json"
    if local_card.exists():
        return local_card
    root = _agent_network_root()
    if root is not None:
        return root / "agent_directory_service" / "configs" / "agent_cards" / "email_agent.card.json"
    return (
        script_dir
        / "AgenticNetwork"
        / "agent_directory_service"
        / "configs"
        / "agent_cards"
        / "email_agent.card.json"
    )


DEFAULT_EMAIL = "agenticweb12@gmail.com"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_MODEL = "openai/gpt-oss-120b"
GOOGLE_WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
GOOGLE_CALENDAR_SCOPES = GOOGLE_WORKSPACE_SCOPES
DEFAULT_GOOGLE_CALENDAR_ID = "primary"
DEFAULT_GOOGLE_TOKEN = "token.json"
DEFAULT_GOOGLE_CREDENTIALS = "credentials.json"
DEFAULT_GOOGLE_CALENDAR_TOKEN = DEFAULT_GOOGLE_TOKEN
DEFAULT_GOOGLE_CALENDAR_CREDENTIALS = DEFAULT_GOOGLE_CREDENTIALS
DEFAULT_AGENT_STATUS_FILE = ".agent_status.json"
DEFAULT_TOOL_TOKEN_USAGE_LOG = Path(__file__).resolve().parent / "tool_token_usage.jsonl"
DEFAULT_NOTICE_LIST_FILE = "notice_list.txt"
DEFAULT_VISIBLE_FILES_DIR = "visible_files"
DEFAULT_ADS_SERVER_ADDR = "localhost:8888"
DEFAULT_ADS_BACKEND = "cli"
DEFAULT_EMAIL_A2A_CARD = _default_email_a2a_card_path()
DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS = 180
EMAIL_ADS_ROUTING_SKILLS: list[dict[str, Any]] = [
    {
        "name": "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search",
        "id": 10306,
    },
    {
        "name": "natural_language_processing/natural_language_generation/summarization",
        "id": 10202,
    },
    {
        "name": "retrieval_augmented_generation/retrieval_of_information/document_retrieval",
        "id": 60103,
    },
]
MAX_ATTACHMENT_PREVIEW_CHARS = 8000
EMAIL_STATUS_LOG_FALSE_VALUES = {"0", "false", "no", "off"}
REMOTE_AGENT_TOOL_NAMES = {"discover_ads_agents", "send_a2a_request"}
TEXT_ATTACHMENT_EXTENSIONS = {
    ".csv",
    ".eml",
    ".html",
    ".htm",
    ".ics",
    ".json",
    ".log",
    ".md",
    ".pub",
    ".rtf",
    ".text",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
EMAIL_ADDRESS_RE = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
LOG_REDACT_KEY_PARTS = {
    "authorization",
    "client_secret",
    "credential",
    "password",
    "refresh_token",
    "token",
    "api_key",
}
DEFAULT_LOG_VALUE_CHARS = 900

LOG_TOKEN_COUNT_KEYS = {
    "cached_input_tokens",
    "candidates_token_count",
    "completion_token_count",
    "completion_tokens",
    "input_tokens",
    "output_tokens",
    "prompt_token_count",
    "prompt_tokens",
    "total_token_count",
    "total_tokens",
}


def _email_status(message: str, **details: Any) -> None:
    if os.getenv("EMAIL_AGENT_STATUS_LOGS", "1").strip().lower() in EMAIL_STATUS_LOG_FALSE_VALUES:
        return
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    rendered = " ".join(
        f"{key}={_log_render(value)}"
        for key, value in details.items()
        if value is not None and value != ""
    )
    suffix = f" {rendered}" if rendered else ""
    print(f"[email-agent] {timestamp} {message}{suffix}", file=sys.stderr, flush=True)


def _log_render(value: Any) -> str:
    sanitized = _log_sanitize(value)
    if isinstance(sanitized, str):
        rendered = sanitized.replace("\n", "\\n")
    else:
        rendered = json.dumps(sanitized, default=str, sort_keys=True)
    max_chars = int(os.getenv("EMAIL_AGENT_LOG_MAX_CHARS", str(DEFAULT_LOG_VALUE_CHARS)))
    if len(rendered) > max_chars:
        return rendered[:max_chars] + "...[truncated]"
    return rendered


def _log_sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if key_text not in LOG_TOKEN_COUNT_KEYS and any(part in lowered for part in LOG_REDACT_KEY_PARTS):
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = _log_sanitize(item, depth=depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        sanitized_items = [_log_sanitize(item, depth=depth + 1) for item in items[:20]]
        if len(items) > 20:
            sanitized_items.append(f"... {len(items) - 20} more")
        return sanitized_items
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _coerce_token_count(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _raw_usage_dict(usage: Any) -> dict[str, Any]:
    if not usage:
        return {}
    if isinstance(usage, dict):
        return usage
    for method_name in ("model_dump", "dict"):
        method = getattr(usage, method_name, None)
        if callable(method):
            try:
                dumped = method()
            except TypeError:
                continue
            if isinstance(dumped, dict):
                return dumped
    return {}


def _first_token_count(raw: dict[str, Any], *names: str) -> int | None:
    for name in names:
        value = _coerce_token_count(raw.get(name))
        if value is not None:
            return value
    return None


def _response_token_usage(response: Any) -> dict[str, Any]:
    usage = _object_get(response, "usage") or _object_get(response, "usage_metadata")
    raw = _raw_usage_dict(usage)
    if not raw:
        return {"usage_available": False}

    input_tokens = _first_token_count(raw, "input_tokens", "prompt_tokens", "prompt_token_count")
    output_tokens = _first_token_count(
        raw,
        "output_tokens",
        "completion_tokens",
        "completion_token_count",
        "candidates_token_count",
    )
    total_tokens = _first_token_count(raw, "total_tokens", "total_token_count")
    input_details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    cached_input_tokens = (
        _coerce_token_count(input_details.get("cached_tokens"))
        if isinstance(input_details, dict)
        else None
    )
    return {
        "usage_available": True,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }


def _model_provider(model: str) -> str:
    normalized = model.lower()
    if normalized.startswith(("gemini-", "models/gemini")):
        return "gemini"
    return "openai"


def _tool_token_usage_log_path() -> Path | None:
    configured = os.getenv("EMAIL_AGENT_TOOL_TOKEN_USAGE_LOG")
    if configured is not None:
        stripped = configured.strip()
        if stripped.lower() in EMAIL_STATUS_LOG_FALSE_VALUES:
            return None
        if stripped:
            return Path(stripped).expanduser()
    return DEFAULT_TOOL_TOKEN_USAGE_LOG


def _record_tool_call_token_usage(
    *,
    model: str,
    response: Any,
    tool: str,
    call_id: str,
    step: int,
    tool_call_count: int,
) -> dict[str, Any]:
    usage = _response_token_usage(response)
    event = {
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "agent": "email-agent",
        "event": "tool_call_token_usage",
        "usage_scope": "model_response_that_requested_tool",
        "model": model,
        "provider": _model_provider(model),
        "response_id": _object_get(response, "id", ""),
        "step": step,
        "tool_call_count": tool_call_count,
        "tool": tool,
        "call_id": call_id,
        **usage,
    }
    _email_status(
        "Agent tool call token usage",
        model=model,
        tool=tool,
        call_id=call_id,
        step=step,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        usage_available=usage.get("usage_available"),
    )
    log_path = _tool_token_usage_log_path()
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_log_sanitize(event), default=str, sort_keys=True) + "\n")
        except Exception as exc:
            _email_status("Tool token usage log write failed", error=f"{type(exc).__name__}: {exc}")
    return usage


@dataclass(frozen=True)
class EmailAdsPublishedRecord:
    record_id: str
    agent_id: str
    backend: str
    backend_ref: str | None = None
    raw_record: dict[str, Any] | None = None


@dataclass(frozen=True)
class EmailConfig:
    email_address: str
    timezone_name: str = DEFAULT_TIMEZONE

    @classmethod
    def from_env(
        cls,
        email_address: str | None = None,
        timezone_name: str | None = None,
    ) -> "EmailConfig":
        email_address = email_address or os.getenv("EMAIL_AGENT_EMAIL", DEFAULT_EMAIL)

        return cls(
            email_address=email_address,
            timezone_name=timezone_name
            or os.getenv("EMAIL_AGENT_TIMEZONE", DEFAULT_TIMEZONE),
        )


class EmailAgent:
    def __init__(self, config: EmailConfig):
        self.config = config

    def read_emails(
        self,
        folder: str = "INBOX",
        limit: int = 5,
        criteria: str = "ALL",
        include_body: bool = True,
        include_attachments: bool = False,
        attachment_dir: Path | str = "attachments",
        mark_read: bool = False,
    ) -> list[dict[str, Any]]:
        """Read newest matching messages through the Gmail API."""
        _email_status(
            "Reading Gmail messages",
            folder=folder,
            limit=max(1, min(limit, 100)),
            include_body=include_body,
            include_attachments=include_attachments,
            mark_read=mark_read,
        )
        service = self._gmail_service()
        query = _gmail_query_from_criteria(criteria)
        label_ids = _gmail_label_ids_from_folder(folder)
        request: dict[str, Any] = {
            "userId": "me",
            "maxResults": max(1, min(limit, 100)),
        }
        if query:
            request["q"] = query
        if label_ids:
            request["labelIds"] = label_ids

        response = service.users().messages().list(**request).execute()
        message_refs = response.get("messages", []) or []
        _email_status("Gmail search returned messages", count=len(message_refs))
        messages: list[dict[str, Any]] = []

        for index, ref in enumerate(message_refs, start=1):
            _email_status("Fetching Gmail message", index=index, total=len(message_refs))
            message_id = ref["id"]
            gmail_message = (
                service.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
            item = _format_gmail_message(
                service=service,
                gmail_message=gmail_message,
                include_body=include_body,
                include_attachments=include_attachments,
                attachment_dir=Path(attachment_dir),
            )

            if mark_read:
                service.users().messages().modify(
                    userId="me",
                    id=message_id,
                    body={"removeLabelIds": ["UNREAD"]},
                ).execute()

            messages.append(item)

        _email_status("Finished reading Gmail messages", count=len(messages))
        return messages

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: str | None = None,
        attachments: list[Path] | None = None,
        high_importance: bool = False,
    ) -> str:
        """Send an email through the Gmail API."""
        if not to:
            raise ValueError("At least one recipient is required.")
        body, html = _coerce_email_body_and_html(body, html)

        _email_status(
            "Sending Gmail email",
            recipient_count=len(to),
            cc_count=len(cc or []),
            bcc_count=len(bcc or []),
            attachment_count=len(attachments or []),
            high_importance=high_importance,
        )
        msg = self._build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            attachments=self.resolve_visible_attachment_paths(attachments or []),
            high_importance=high_importance,
        )
        sent = (
            self._gmail_service()
            .users()
            .messages()
            .send(userId="me", body={"raw": _gmail_raw_message(msg)})
            .execute()
        )
        message_id = sent.get("id", msg["Message-ID"])
        _email_status("Gmail email sent", message_id=message_id)
        return message_id

    def load_notice_recipients(self, recipient_file: Path | str | None = None) -> list[str]:
        """Load configured notice recipients."""
        path = _notice_list_path(recipient_file)
        _email_status("Loading notice recipients", path=str(path))
        recipients = load_recipients_from_file(path)
        if not recipients:
            raise ValueError(f"No recipients found in {path}.")
        _email_status("Notice recipients loaded", path=str(path), count=len(recipients))
        return recipients

    def send_group_email(
        self,
        *,
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: str | None = None,
        attachments: list[Path] | None = None,
        recipient_file: Path | str | None = None,
        high_importance: bool = False,
        recipient_source: str | None = None,
        to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send one email using configured group notice recipients."""
        if recipient_source:
            _normalize_recipient_source(recipient_source)
        explicit_recipients = _normalize_addresses(to)
        if explicit_recipients:
            _email_status(
                "Ignoring explicit recipients for group email; using notice list",
                explicit_recipient_count=len(explicit_recipients),
            )
        recipients = self.load_notice_recipients(recipient_file)
        source_label = str(_notice_list_path(recipient_file))
        if not recipients:
            raise ValueError("Group email requires at least one recipient.")

        visible_attachments = self.resolve_visible_attachment_paths(attachments or [])
        message_id = self.send_email(
            to=recipients,
            cc=cc,
            bcc=bcc,
            subject=subject,
            body=body,
            html=html,
            attachments=visible_attachments,
            high_importance=high_importance,
        )
        return {
            "message_id": message_id,
            "recipient_source": "notice_list",
            "recipient_source_detail": source_label,
            "recipient_count": len(recipients),
            "to": recipients,
            "subject": subject,
            "attachments": [_visible_file_relative_path(path) for path in visible_attachments],
            "high_importance": high_importance,
        }

    def write_email_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: str | None = None,
        attachments: list[Path] | None = None,
        high_importance: bool = False,
    ) -> str:
        """Create a Gmail draft."""
        if not to:
            raise ValueError("At least one recipient is required.")
        body, html = _coerce_email_body_and_html(body, html)

        _email_status(
            "Creating Gmail draft",
            recipient_count=len(to),
            cc_count=len(cc or []),
            bcc_count=len(bcc or []),
            high_importance=high_importance,
        )
        msg = self._build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            attachments=self.resolve_visible_attachment_paths(attachments or []),
            high_importance=high_importance,
        )
        draft = (
            self._gmail_service()
            .users()
            .drafts()
            .create(userId="me", body={"message": {"raw": _gmail_raw_message(msg)}})
            .execute()
        )
        draft_id = draft.get("id", "")
        _email_status("Gmail draft created", draft_id=draft_id)
        return draft_id

    def create_calendar_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str] | None = None,
        description: str = "",
        location: str = "",
        send_invite: bool = True,
        use_google_api: bool = False,
        allow_ics_fallback: bool = True,
    ) -> dict[str, Any]:
        """
        Create a calendar event when possible.

        When use_google_api is True, this creates the event directly in Google
        Calendar. Otherwise it creates or sends a standard .ics invitation
        through the Gmail API.
        """
        attendees = attendees or []
        _email_status(
            "Creating calendar event",
            attendee_count=len(attendees),
            send_invite=send_invite,
            use_google_api=use_google_api,
            allow_ics_fallback=allow_ics_fallback,
        )

        google_api_error = ""
        if use_google_api:
            _email_status("Trying Google Calendar API event creation")
            try:
                google_result = self._create_google_calendar_event(
                    title=title,
                    start=start,
                    end=end,
                    attendees=attendees,
                    description=description,
                    location=location,
                )
            except Exception as exc:
                google_result = None
                google_api_error = f"{type(exc).__name__}: {exc}"
                _email_status("Google Calendar API event creation failed", error=google_api_error)
            if google_result:
                _email_status("Google Calendar event created", event_id=google_result.get("event_id"))
                return google_result
            if not allow_ics_fallback:
                raise RuntimeError(
                    "Could not create the event in Google Calendar. "
                    f"{google_api_error or 'Google Calendar API is not configured.'}"
                )

        event_uid = f"{uuid.uuid4()}@email-agent"
        ics_attendees = attendees or ([self.config.email_address] if send_invite else [])
        ics_text = build_ics_event(
            organizer=self.config.email_address,
            uid=event_uid,
            title=title,
            start=start,
            end=end,
            attendees=ics_attendees,
            description=description,
            location=location,
            method="REQUEST" if send_invite else "PUBLISH",
        )

        if send_invite:
            _email_status("Sending calendar invite email", attendee_count=len(ics_attendees))
            message_id = self._send_calendar_invite(
                to=attendees,
                title=title,
                start=start,
                end=end,
                description=description,
                location=location,
                ics_text=ics_text,
            )
            result = {
                "mode": "ics_email",
                "created_in_google_calendar": False,
                "event_uid": event_uid,
                "message_id": message_id,
                "recipients": ", ".join(ics_attendees),
            }
            if google_api_error:
                result["google_api_error"] = google_api_error
            _email_status("Calendar invite email sent", message_id=message_id)
            return result

        output_dir = Path("calendar_events")
        output_dir.mkdir(exist_ok=True)
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in title)
        event_file = output_dir / f"{safe_name or 'event'}-{event_uid.split('@')[0]}.ics"
        event_file.write_text(ics_text, encoding="utf-8")
        result = {
            "mode": "ics_file",
            "created_in_google_calendar": False,
            "event_uid": event_uid,
            "path": str(event_file),
        }
        if google_api_error:
            result["google_api_error"] = google_api_error
        _email_status("Local ICS event file written", path=str(event_file))
        return result

    def list_calendar_events(
        self,
        start: datetime,
        end: datetime,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Read events from Google Calendar."""
        _email_status(
            "Listing Google Calendar events",
            start=start.isoformat(),
            end=end.isoformat(),
            max_results=max_results,
        )
        service = self._google_calendar_service()
        events_result = (
            service.events()
            .list(
                calendarId=os.getenv("GOOGLE_CALENDAR_ID", DEFAULT_GOOGLE_CALENDAR_ID),
                timeMin=_rfc3339_utc(start),
                timeMax=_rfc3339_utc(end),
                maxResults=max(1, min(max_results, 100)),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = [_format_google_event(item) for item in events_result.get("items", [])]
        _email_status("Google Calendar events listed", count=len(events))
        return events

    def update_calendar_event(
        self,
        event_id: str,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        attendees: list[str] | None = None,
        description: str | None = None,
        location: str | None = None,
        replace_attendees: bool = False,
        send_updates: bool = True,
    ) -> dict[str, Any]:
        """Patch an existing Google Calendar event by event ID."""
        event_id = event_id.strip()
        if not event_id:
            raise ValueError("event_id is required.")
        if start and end and end <= start:
            raise ValueError("Event end time must be after the start time.")

        event_body: dict[str, Any] = {}
        if title is not None and title.strip():
            event_body["summary"] = title.strip()
        if description is not None:
            event_body["description"] = description
        if location is not None:
            event_body["location"] = location
        if start is not None:
            event_body["start"] = {
                "dateTime": start.isoformat(),
                "timeZone": self.config.timezone_name,
            }
        if end is not None:
            event_body["end"] = {
                "dateTime": end.isoformat(),
                "timeZone": self.config.timezone_name,
            }
        if replace_attendees:
            event_body["attendees"] = [
                {"email": attendee}
                for attendee in _normalize_addresses(attendees or [])
            ]
        if not event_body:
            raise ValueError("At least one event field must be provided to update.")

        _email_status(
            "Updating Google Calendar event",
            event_id=event_id,
            changed_fields=sorted(event_body.keys()),
            send_updates=send_updates,
        )
        service = self._google_calendar_service()
        updated = (
            service.events()
            .patch(
                calendarId=os.getenv("GOOGLE_CALENDAR_ID", DEFAULT_GOOGLE_CALENDAR_ID),
                eventId=event_id,
                body=event_body,
                sendUpdates="all" if send_updates else "none",
            )
            .execute()
        )
        result = {
            "mode": "google_calendar_api",
            "updated_in_google_calendar": True,
            "event": _format_google_event(updated),
        }
        _email_status("Google Calendar event updated", event_id=event_id)
        return result

    def delete_calendar_event(
        self,
        event_id: str,
        *,
        send_updates: bool = True,
    ) -> dict[str, Any]:
        """Delete an existing Google Calendar event by event ID."""
        event_id = event_id.strip()
        if not event_id:
            raise ValueError("event_id is required.")

        _email_status("Deleting Google Calendar event", event_id=event_id, send_updates=send_updates)
        service = self._google_calendar_service()
        (
            service.events()
            .delete(
                calendarId=os.getenv("GOOGLE_CALENDAR_ID", DEFAULT_GOOGLE_CALENDAR_ID),
                eventId=event_id,
                sendUpdates="all" if send_updates else "none",
            )
            .execute()
        )
        result = {
            "mode": "google_calendar_api",
            "deleted_from_google_calendar": True,
            "event_id": event_id,
        }
        _email_status("Google Calendar event deleted", event_id=event_id)
        return result

    def authorize_google_calendar(self) -> dict[str, Any]:
        """Run OAuth setup and write token.json for Google Workspace access."""
        return self.authorize_google_workspace()

    def authorize_google_workspace(self) -> dict[str, Any]:
        """Run OAuth setup and write token.json for Gmail, Calendar, Docs, and Sheets."""
        _email_status("Authorizing Google Workspace")
        credentials = self._google_credentials(interactive=True)
        _email_status("Google Workspace authorized", token_path=str(_calendar_token_path()))
        return {
            "ok": True,
            "token_path": str(_calendar_token_path()),
            "scopes": list(credentials.scopes or GOOGLE_WORKSPACE_SCOPES),
        }

    def calendar_status(self) -> dict[str, Any]:
        """Return local Google Workspace setup state without starting OAuth."""
        return self.google_workspace_status()

    def google_workspace_status(self) -> dict[str, Any]:
        """Return local Google Workspace setup state without starting OAuth."""
        _email_status("Checking Google Workspace status")
        dependency_error = ""
        try:
            _import_google_dependencies()
        except ImportError as exc:
            dependency_error = str(exc)

        token_path = _calendar_token_path()
        credentials_path = _calendar_credentials_path()
        status: dict[str, Any] = {
            "dependencies_installed": not dependency_error,
            "dependency_error": dependency_error,
            "token_path": str(token_path),
            "token_exists": token_path.exists(),
            "credentials_path": str(credentials_path),
            "credentials_exists": credentials_path.exists(),
            "calendar_id": os.getenv("GOOGLE_CALENDAR_ID", DEFAULT_GOOGLE_CALENDAR_ID),
            "scopes": GOOGLE_WORKSPACE_SCOPES,
            "token_scopes": _stored_token_scopes(token_path) if token_path.exists() else [],
            "missing_scopes": _missing_google_scopes(token_path) if token_path.exists() else [],
        }

        if dependency_error or not token_path.exists():
            return status

        try:
            credentials = self._google_credentials(interactive=False)
            status["authorized"] = bool(credentials and credentials.valid)
        except Exception as exc:
            status["authorized"] = False
            status["auth_error"] = f"{type(exc).__name__}: {exc}"
        _email_status(
            "Google Workspace status checked",
            authorized=status.get("authorized"),
            token_exists=status.get("token_exists"),
            credentials_exists=status.get("credentials_exists"),
        )
        return status

    def list_google_docs(self, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
        _email_status("Listing Google Docs", max_results=max_results, has_query=bool(query))
        service = self._google_service("drive", "v3")
        drive_query = "mimeType='application/vnd.google-apps.document' and trashed=false"
        if query:
            drive_query += f" and name contains '{_drive_query_escape(query)}'"
        result = (
            service.files()
            .list(
                q=drive_query,
                pageSize=max(1, min(max_results, 100)),
                fields="files(id,name,mimeType,modifiedTime,webViewLink)",
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        docs = result.get("files", []) or []
        _email_status("Google Docs listed", count=len(docs))
        return docs

    def read_google_doc(self, document_id: str) -> dict[str, Any]:
        _email_status("Reading Google Doc", document_id=document_id)
        document = (
            self._google_service("docs", "v1")
            .documents()
            .get(documentId=document_id)
            .execute()
        )
        result = {
            "document_id": document_id,
            "title": document.get("title", ""),
            "text": _extract_google_doc_text(document),
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
        }
        _email_status("Google Doc read", document_id=document_id)
        return result

    def create_google_doc(self, title: str, content: str = "") -> dict[str, Any]:
        _email_status("Creating Google Doc", has_content=bool(content))
        service = self._google_service("docs", "v1")
        document = service.documents().create(body={"title": title}).execute()
        document_id = document["documentId"]
        if content:
            service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
            ).execute()
        result = {
            "document_id": document_id,
            "title": title,
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
        }
        _email_status("Google Doc created", document_id=document_id)
        return result

    def append_google_doc(self, document_id: str, text: str) -> dict[str, Any]:
        _email_status("Appending to Google Doc", document_id=document_id, chars=len(text))
        service = self._google_service("docs", "v1")
        document = service.documents().get(documentId=document_id).execute()
        end_index = _google_doc_end_index(document)
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"location": {"index": end_index}, "text": text}}]},
        ).execute()
        result = {
            "document_id": document_id,
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
            "appended_chars": len(text),
        }
        _email_status("Google Doc append completed", document_id=document_id, chars=len(text))
        return result

    def replace_google_doc_text(
        self,
        document_id: str,
        find_text: str,
        replace_text: str,
    ) -> dict[str, Any]:
        _email_status("Replacing text in Google Doc", document_id=document_id)
        response = (
            self._google_service("docs", "v1")
            .documents()
            .batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "replaceAllText": {
                                "containsText": {"text": find_text, "matchCase": True},
                                "replaceText": replace_text,
                            }
                        }
                    ]
                },
            )
            .execute()
        )
        occurrences = 0
        for reply in response.get("replies", []) or []:
            occurrences += reply.get("replaceAllText", {}).get("occurrencesChanged", 0)
        result = {
            "document_id": document_id,
            "occurrences_changed": occurrences,
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
        }
        _email_status("Google Doc text replacement completed", document_id=document_id, occurrences=occurrences)
        return result

    def list_google_sheets(self, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
        _email_status("Listing Google Sheets", max_results=max_results, has_query=bool(query))
        service = self._google_service("drive", "v3")
        drive_query = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
        if query:
            drive_query += f" and name contains '{_drive_query_escape(query)}'"
        result = (
            service.files()
            .list(
                q=drive_query,
                pageSize=max(1, min(max_results, 100)),
                fields="files(id,name,mimeType,modifiedTime,webViewLink)",
                orderBy="modifiedTime desc",
            )
            .execute()
        )
        sheets = result.get("files", []) or []
        _email_status("Google Sheets listed", count=len(sheets))
        return sheets

    def read_google_sheet(self, spreadsheet_id: str, range_name: str = "A1:Z50") -> dict[str, Any]:
        _email_status("Reading Google Sheet", spreadsheet_id=spreadsheet_id, range=range_name)
        values = (
            self._google_service("sheets", "v4")
            .spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        result = {
            "spreadsheet_id": spreadsheet_id,
            "range": values.get("range", range_name),
            "values": values.get("values", []),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }
        _email_status("Google Sheet read", spreadsheet_id=spreadsheet_id, row_count=len(result["values"]))
        return result

    def create_google_sheet(
        self,
        title: str,
        sheet_name: str = "Sheet1",
        values: list[list[Any]] | None = None,
    ) -> dict[str, Any]:
        _email_status("Creating Google Sheet", sheet_name=sheet_name, row_count=len(values or []))
        service = self._google_service("sheets", "v4")
        spreadsheet = (
            service.spreadsheets()
            .create(body={"properties": {"title": title}, "sheets": [{"properties": {"title": sheet_name}}]})
            .execute()
        )
        spreadsheet_id = spreadsheet["spreadsheetId"]
        if values:
            self.write_google_sheet(spreadsheet_id, f"{sheet_name}!A1", values)
        result = {
            "spreadsheet_id": spreadsheet_id,
            "title": title,
            "url": spreadsheet.get(
                "spreadsheetUrl",
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            ),
        }
        _email_status("Google Sheet created", spreadsheet_id=spreadsheet_id)
        return result

    def write_google_sheet(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        _email_status(
            "Writing Google Sheet range",
            spreadsheet_id=spreadsheet_id,
            range=range_name,
            row_count=len(values),
        )
        result = (
            self._google_service("sheets", "v4")
            .spreadsheets()
            .values()
            .update(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption=value_input_option,
                body={"values": values},
            )
            .execute()
        )
        output = {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": result.get("updatedRange", range_name),
            "updated_cells": result.get("updatedCells", 0),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }
        _email_status("Google Sheet range written", spreadsheet_id=spreadsheet_id, updated_cells=output["updated_cells"])
        return output

    def append_google_sheet(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
        _email_status(
            "Appending Google Sheet rows",
            spreadsheet_id=spreadsheet_id,
            range=range_name,
            row_count=len(values),
        )
        result = (
            self._google_service("sheets", "v4")
            .spreadsheets()
            .values()
            .append(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueInputOption=value_input_option,
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )
        updates = result.get("updates", {})
        output = {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": updates.get("updatedRange", range_name),
            "updated_cells": updates.get("updatedCells", 0),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }
        _email_status("Google Sheet rows appended", spreadsheet_id=spreadsheet_id, updated_cells=output["updated_cells"])
        return output

    def list_visible_files(
        self,
        *,
        directory: str = "",
        query: str = "",
        recursive: bool = True,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """List files and folders under the local visible_files sandbox."""
        root = _visible_files_root()
        start = _resolve_visible_dir_path(directory)
        if not start.exists():
            raise FileNotFoundError(f"Visible directory not found: {_visible_file_relative_path(start)}")
        if not start.is_dir():
            raise NotADirectoryError(f"Visible path is not a directory: {_visible_file_relative_path(start)}")

        lowered_query = query.strip().lower()
        iterator = start.rglob("*") if recursive else start.iterdir()
        results: list[dict[str, Any]] = []
        for path in sorted(iterator, key=lambda item: _visible_file_relative_path(item).lower()):
            relative_path = _visible_file_relative_path(path)
            if lowered_query and lowered_query not in relative_path.lower() and lowered_query not in path.name.lower():
                continue
            results.append(_visible_file_summary(path, root=root))
            if len(results) >= max(1, min(max_results, 500)):
                break
        return results

    def read_visible_file(self, path: str, *, max_chars: int = MAX_ATTACHMENT_PREVIEW_CHARS) -> dict[str, Any]:
        """Read text from a file under visible_files, with binary files summarized safely."""
        visible_path = _resolve_visible_file_path(path)
        if not visible_path.exists():
            raise FileNotFoundError(f"Visible file not found: {_visible_file_relative_path(visible_path)}")
        if not visible_path.is_file() or visible_path.is_symlink():
            raise ValueError(f"Visible path is not a regular file: {_visible_file_relative_path(visible_path)}")

        data = visible_path.read_bytes()
        content_type = mimetypes.guess_type(str(visible_path))[0] or "application/octet-stream"
        preview, note = _attachment_text_preview(
            data=data,
            filename=visible_path.name,
            content_type=content_type,
            charset=None,
            max_chars=max(1, min(max_chars, 50000)),
        )
        result = _visible_file_summary(visible_path)
        if preview:
            result["text"] = preview
        if note:
            result["read_note"] = note
        return result

    def write_visible_file(
        self,
        path: str,
        content: str,
        *,
        mode: str = "overwrite",
    ) -> dict[str, Any]:
        """Write UTF-8 text to a file under visible_files."""
        visible_path = _resolve_visible_file_path(path)
        if visible_path.exists() and (not visible_path.is_file() or visible_path.is_symlink()):
            raise ValueError(f"Visible path is not a regular file: {_visible_file_relative_path(visible_path)}")
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"overwrite", "append", "create_new"}:
            raise ValueError("mode must be overwrite, append, or create_new.")
        if normalized_mode == "create_new" and visible_path.exists():
            raise FileExistsError(f"Visible file already exists: {_visible_file_relative_path(visible_path)}")

        visible_path.parent.mkdir(parents=True, exist_ok=True)
        if normalized_mode == "append":
            with visible_path.open("a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            visible_path.write_text(content, encoding="utf-8")
        return {
            "relative_path": _visible_file_relative_path(visible_path),
            "size_bytes": visible_path.stat().st_size,
            "mode": normalized_mode,
        }

    def resolve_visible_attachment_paths(self, paths: Iterable[Path | str]) -> list[Path]:
        """Resolve attachment names against visible_files and reject anything outside it."""
        attachments: list[Path] = []
        for value in paths:
            visible_path = _resolve_visible_file_path(value)
            if not visible_path.exists():
                raise FileNotFoundError(f"Visible attachment not found: {_visible_file_relative_path(visible_path)}")
            if not visible_path.is_file() or visible_path.is_symlink():
                raise ValueError(f"Visible attachment is not a regular file: {_visible_file_relative_path(visible_path)}")
            attachments.append(visible_path)
        return attachments

    def discover_ads_agents(
        self,
        *,
        skill: str = "",
        query: str = "",
        limit: int = 10,
        include_self: bool = False,
    ) -> list[dict[str, Any]]:
        """Discover live agents registered in ADS using the standalone CLI path."""
        return discover_ads_agents(
            skill=skill,
            query=query,
            limit=limit,
            include_self=include_self,
        )

    def send_a2a_request_to_ads_agent(
        self,
        *,
        agent_ref: str,
        skill: str = "",
        message: str = "",
        data: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS,
        confirmed: bool = True,
    ) -> dict[str, Any]:
        """Resolve an ADS-discovered agent and send it one A2A request."""
        return asyncio.run(
            send_a2a_request_to_ads_agent(
                agent_ref=agent_ref,
                skill=skill,
                message=message,
                data=data or {},
                timeout_seconds=timeout_seconds,
                confirmed=confirmed,
            )
        )

    def _add_attachment(self, msg: EmailMessage, attachment: Path) -> None:
        if not attachment.exists():
            raise FileNotFoundError(f"Attachment not found: {attachment}")

        content_type, _ = mimetypes.guess_type(str(attachment))
        if content_type:
            maintype, subtype = content_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"

        msg.add_attachment(
            attachment.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=attachment.name,
        )

    def _build_email_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: str | None = None,
        attachments: list[Path] | None = None,
        high_importance: bool = False,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.config.email_address
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.config.email_address.split("@")[-1])
        if high_importance:
            msg["Importance"] = "High"
            msg["Priority"] = "urgent"
            msg["X-Priority"] = "1 (Highest)"
            msg["X-MSMail-Priority"] = "High"

        msg.set_content(body or "")
        if html:
            msg.add_alternative(html, subtype="html")

        for attachment in attachments or []:
            self._add_attachment(msg, attachment)

        return msg

    def _send_calendar_invite(
        self,
        to: list[str],
        title: str,
        start: datetime,
        end: datetime,
        description: str,
        location: str,
        ics_text: str,
    ) -> str:
        recipients = to or [self.config.email_address]
        msg = EmailMessage()
        msg["From"] = self.config.email_address
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = f"Calendar invite: {title}"
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.config.email_address.split("@")[-1])

        details = [
            f"Event: {title}",
            f"Start: {start.isoformat()}",
            f"End: {end.isoformat()}",
        ]
        if location:
            details.append(f"Location: {location}")
        if description:
            details.extend(["", description])

        msg.set_content("\n".join(details))
        msg.add_alternative(
            ics_text,
            subtype="calendar",
            params={"method": "REQUEST", "charset": "UTF-8"},
        )
        msg.add_attachment(
            ics_text.encode("utf-8"),
            maintype="text",
            subtype="calendar",
            filename="invite.ics",
            params={"method": "REQUEST"},
        )

        sent = (
            self._gmail_service()
            .users()
            .messages()
            .send(userId="me", body={"raw": _gmail_raw_message(msg)})
            .execute()
        )
        return sent.get("id", msg["Message-ID"])

    def _gmail_service(self) -> Any:
        return self._google_service("gmail", "v1")

    def _google_calendar_service(self) -> Any:
        return self._google_service("calendar", "v3")

    def _google_service(self, api_name: str, api_version: str) -> Any:
        _email_status("Creating Google API service", api=f"{api_name}/{api_version}")
        credentials = self._google_credentials(interactive=False)
        _, _, _, build = _import_google_dependencies()
        return build(api_name, api_version, credentials=credentials)

    def _google_calendar_credentials(self, interactive: bool) -> Any:
        return self._google_credentials(interactive)

    def _google_credentials(self, interactive: bool) -> Any:
        Credentials, Request, InstalledAppFlow, _ = _import_google_dependencies()
        token_path = _calendar_token_path()
        credentials_path = _calendar_credentials_path()
        credentials = None
        _email_status("Loading Google credentials", token_path=str(token_path), interactive=interactive)

        if token_path.exists():
            missing_scopes = _missing_google_scopes(token_path)
            if missing_scopes:
                _email_status("Google token is missing required scopes", missing_scope_count=len(missing_scopes))
                if not interactive:
                    raise RuntimeError(
                        "Existing Google token is missing required scopes. "
                        "Run: python3 email_agent.py google-auth. "
                        f"Missing: {', '.join(missing_scopes)}"
                    )
                credentials = None
            else:
                credentials = Credentials.from_authorized_user_file(
                    str(token_path),
                    GOOGLE_WORKSPACE_SCOPES,
                )

        if credentials and not _credentials_have_scopes(credentials):
            if not interactive:
                raise RuntimeError(
                    "Existing Google token is missing required Gmail/Calendar/Docs/Sheets scopes. "
                    "Run: python3 email_agent.py google-auth"
                )
            credentials = None

        if token_path.exists() and credentials is None and not _missing_google_scopes(token_path):
            credentials = Credentials.from_authorized_user_file(
                str(token_path),
                GOOGLE_WORKSPACE_SCOPES,
            )

        if credentials and credentials.valid:
            _email_status("Using valid Google credentials")
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            _email_status("Refreshing expired Google credentials")
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
            _email_status("Google credentials refreshed", token_path=str(token_path))
            return credentials

        if not interactive:
            raise RuntimeError(
                "Google Workspace is not authorized. Run: "
                "python3 email_agent.py google-auth"
            )

        if not credentials_path.exists():
            raise RuntimeError(
                f"Missing Google OAuth client file: {credentials_path}. "
                "Create a Desktop OAuth client in Google Cloud Console, download it, "
                "and save it here as credentials.json or set GOOGLE_CREDENTIALS."
            )

        _email_status("Starting Google OAuth local server", credentials_path=str(credentials_path))
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path),
            GOOGLE_WORKSPACE_SCOPES,
        )
        credentials = flow.run_local_server(port=0, open_browser=False, prompt="consent")
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        _email_status("Google OAuth token saved", token_path=str(token_path))
        return credentials

    def _create_google_calendar_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        description: str,
        location: str,
    ) -> dict[str, Any]:
        if end <= start:
            raise ValueError("Event end time must be after the start time.")

        _email_status("Creating Google Calendar event through API", attendee_count=len(attendees))
        service = self._google_calendar_service()
        event_body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {
                "dateTime": start.isoformat(),
                "timeZone": self.config.timezone_name,
            },
            "end": {
                "dateTime": end.isoformat(),
                "timeZone": self.config.timezone_name,
            },
        }
        if attendees:
            event_body["attendees"] = [{"email": attendee} for attendee in attendees]

        created = (
            service.events()
            .insert(
                calendarId=os.getenv("GOOGLE_CALENDAR_ID", DEFAULT_GOOGLE_CALENDAR_ID),
                body=event_body,
                sendUpdates="all" if attendees else "none",
            )
            .execute()
        )
        result = {
            "mode": "google_calendar_api",
            "created_in_google_calendar": True,
            "event_id": created.get("id", ""),
            "html_link": created.get("htmlLink", ""),
            "summary": created.get("summary", title),
            "start": created.get("start", {}),
            "end": created.get("end", {}),
        }
        _email_status("Google Calendar API event created", event_id=result["event_id"])
        return result


def _calendar_token_path() -> Path:
    return Path(
        os.getenv(
            "GOOGLE_TOKEN",
            os.getenv("GOOGLE_CALENDAR_TOKEN", DEFAULT_GOOGLE_TOKEN),
        )
    )


def _calendar_credentials_path() -> Path:
    return Path(
        os.getenv(
            "GOOGLE_CREDENTIALS",
            os.getenv("GOOGLE_CALENDAR_CREDENTIALS", DEFAULT_GOOGLE_CREDENTIALS),
        )
    )


def _import_google_calendar_dependencies() -> tuple[Any, Any, Any, Any]:
    return _import_google_dependencies()


def _import_google_dependencies() -> tuple[Any, Any, Any, Any]:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=FutureWarning, module=r"google\..*")
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
    except ImportError as exc:
        raise ImportError(
            "Missing Google API packages. Install them with: "
            "python3 -m pip install google-api-python-client "
            "google-auth-oauthlib google-auth-httplib2"
        ) from exc

    return Credentials, Request, InstalledAppFlow, build


def _credentials_have_scopes(credentials: Any) -> bool:
    if hasattr(credentials, "has_scopes"):
        return bool(credentials.has_scopes(GOOGLE_WORKSPACE_SCOPES))
    granted = set(credentials.scopes or [])
    return set(GOOGLE_WORKSPACE_SCOPES).issubset(granted)


def _stored_token_scopes(token_path: Path) -> list[str]:
    if not token_path.exists():
        return []
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    scopes = data.get("scopes")
    if isinstance(scopes, list):
        return [str(scope) for scope in scopes]
    if isinstance(scopes, str):
        return scopes.split()

    scope = data.get("scope")
    if isinstance(scope, str):
        return scope.split()
    return []


def _missing_google_scopes(token_path: Path) -> list[str]:
    stored = set(_stored_token_scopes(token_path))
    if not stored:
        return list(GOOGLE_WORKSPACE_SCOPES)
    return [scope for scope in GOOGLE_WORKSPACE_SCOPES if scope not in stored]


def _rfc3339_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_google_event(item: dict[str, Any]) -> dict[str, Any]:
    start = item.get("start", {})
    end = item.get("end", {})
    attendees = item.get("attendees", []) or []
    return {
        "id": item.get("id", ""),
        "status": item.get("status", ""),
        "summary": item.get("summary", "(No title)"),
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "location": item.get("location", ""),
        "description": item.get("description", ""),
        "html_link": item.get("htmlLink", ""),
        "attendees": [
            {
                "email": attendee.get("email", ""),
                "response_status": attendee.get("responseStatus", ""),
            }
            for attendee in attendees
        ],
    }


def _gmail_raw_message(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def _gmail_query_from_criteria(criteria: str) -> str:
    value = (criteria or "").strip()
    upper = value.upper()
    if not value or upper == "ALL":
        return ""
    if upper == "UNSEEN":
        return "is:unread"
    if upper == "SEEN":
        return "-is:unread"

    from_match = re.fullmatch(r'FROM\s+"?([^"]+)"?', value, flags=re.IGNORECASE)
    if from_match:
        return f"from:{from_match.group(1)}"

    subject_match = re.fullmatch(r'SUBJECT\s+"?([^"]+)"?', value, flags=re.IGNORECASE)
    if subject_match:
        return f"subject:{subject_match.group(1)}"

    return value


def _gmail_label_ids_from_folder(folder: str) -> list[str]:
    normalized = (folder or "").strip().upper()
    if normalized in {"", "ALL", "ALL MAIL", "[GMAIL]/ALL MAIL"}:
        return []
    common_labels = {
        "INBOX",
        "SENT",
        "DRAFT",
        "DRAFTS",
        "TRASH",
        "SPAM",
        "STARRED",
        "IMPORTANT",
        "UNREAD",
    }
    if normalized == "DRAFTS":
        return ["DRAFT"]
    if normalized in common_labels:
        return [normalized]
    return [folder]


def _format_gmail_message(
    service: Any,
    gmail_message: dict[str, Any],
    include_body: bool,
    include_attachments: bool,
    attachment_dir: Path,
) -> dict[str, Any]:
    payload = gmail_message.get("payload", {}) or {}
    headers = _gmail_headers(payload)
    item: dict[str, Any] = {
        "uid": gmail_message.get("id", ""),
        "thread_id": gmail_message.get("threadId", ""),
        "from": _decode_mime_header(headers.get("from", "")),
        "to": _decode_mime_header(headers.get("to", "")),
        "date": _decode_mime_header(headers.get("date", "")),
        "subject": _decode_mime_header(headers.get("subject", "")),
        "snippet": gmail_message.get("snippet", ""),
        "label_ids": gmail_message.get("labelIds", []),
    }

    if include_body:
        item["body"] = _gmail_payload_body(payload)

    if include_attachments:
        item["attachments"] = _extract_gmail_attachments(
            service=service,
            message_id=gmail_message.get("id", ""),
            payload=payload,
            subject=item["subject"],
            output_dir=attachment_dir,
        )

    return item


def _gmail_headers(payload: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in payload.get("headers", []) or []:
        name = str(header.get("name", "")).lower()
        if name:
            headers[name] = str(header.get("value", ""))
    return headers


def _gmail_payload_body(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in _walk_gmail_parts(payload):
        filename = part.get("filename")
        if filename:
            continue
        mime_type = part.get("mimeType", "")
        data = (part.get("body", {}) or {}).get("data")
        if not data:
            continue
        text = _decode_bytes(_gmail_b64decode(data))
        if mime_type == "text/plain":
            plain_parts.append(text)
        elif mime_type == "text/html":
            html_parts.append(_html_to_text(text))

    if plain_parts:
        return "\n".join(part.strip() for part in plain_parts if part.strip())
    return "\n".join(part.strip() for part in html_parts if part.strip())


def _extract_gmail_attachments(
    service: Any,
    message_id: str,
    payload: dict[str, Any],
    subject: str,
    output_dir: Path,
    max_preview_chars: int = MAX_ATTACHMENT_PREVIEW_CHARS,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    message_dir_name = _safe_filename(f"{message_id}-{subject}") or message_id
    message_dir = output_dir / message_dir_name

    for index, part in enumerate(_walk_gmail_parts(payload), start=1):
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if not filename and not body.get("attachmentId"):
            continue

        content_type = part.get("mimeType", "application/octet-stream")
        safe_filename = _safe_attachment_filename(
            filename or f"attachment-{index}{_extension_for_content_type(content_type)}"
        )

        if body.get("attachmentId"):
            attachment_payload = (
                service.users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=body["attachmentId"])
                .execute()
            )
            data = _gmail_b64decode(attachment_payload.get("data", ""))
        else:
            data = _gmail_b64decode(body.get("data", ""))

        message_dir.mkdir(parents=True, exist_ok=True)
        output_path = _unique_path(message_dir / safe_filename)
        output_path.write_bytes(data)

        attachment: dict[str, Any] = {
            "filename": filename or safe_filename,
            "content_type": content_type,
            "size_bytes": len(data),
            "path": str(output_path),
        }
        preview, note = _attachment_text_preview(
            data=data,
            filename=filename or safe_filename,
            content_type=content_type,
            charset=None,
            max_chars=max_preview_chars,
        )
        if preview:
            attachment["text_preview"] = preview
        if note:
            attachment["read_note"] = note
        attachments.append(attachment)

    return attachments


def _walk_gmail_parts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield payload
    for part in payload.get("parts", []) or []:
        yield from _walk_gmail_parts(part)


def _gmail_b64decode(value: str) -> bytes:
    if not value:
        return b""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _drive_query_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _extract_google_doc_text(document: dict[str, Any]) -> str:
    parts: list[str] = []
    for element in document.get("body", {}).get("content", []) or []:
        _append_doc_structural_text(element, parts)
    return "".join(parts).strip()


def _append_doc_structural_text(element: dict[str, Any], parts: list[str]) -> None:
    paragraph = element.get("paragraph")
    if paragraph:
        for item in paragraph.get("elements", []) or []:
            text_run = item.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
        return

    table = element.get("table")
    if table:
        for row in table.get("tableRows", []) or []:
            row_parts: list[str] = []
            for cell in row.get("tableCells", []) or []:
                cell_parts: list[str] = []
                for content in cell.get("content", []) or []:
                    _append_doc_structural_text(content, cell_parts)
                row_parts.append("".join(cell_parts).strip())
            if row_parts:
                parts.append("\t".join(row_parts) + "\n")


def _google_doc_end_index(document: dict[str, Any]) -> int:
    content = document.get("body", {}).get("content", []) or []
    if not content:
        return 1
    return max(1, int(content[-1].get("endIndex", 2)) - 1)


AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "read_email",
        "description": "Read recent email messages through the Gmail API, optionally saving and previewing attachments.",
        "parameters": {
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "description": "Mailbox folder to read, usually INBOX.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Newest matching messages to read. Keep this small.",
                    "minimum": 1,
                    "maximum": 25,
                },
                "criteria": {
                    "type": "string",
                    "description": "Gmail search query, or simple aliases like ALL, UNSEEN, or FROM \"person@example.com\".",
                },
                "include_body": {
                    "type": "boolean",
                    "description": "Whether to include message body text. The agent normally sets this true so email content is available for reasoning.",
                },
                "include_attachments": {
                    "type": "boolean",
                    "description": "Whether to save attachments and include readable text previews when possible.",
                },
                "mark_read": {
                    "type": "boolean",
                    "description": "Whether to mark fetched messages as read.",
                },
            },
            "required": [
                "folder",
                "limit",
                "criteria",
                "include_body",
                "include_attachments",
                "mark_read",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_email_draft",
        "description": "Create a Gmail draft for review without sending it.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient email addresses.",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC email addresses, or an empty array.",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC email addresses, or an empty array.",
                },
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "html": {
                    "type": "string",
                    "description": "Optional HTML body alternative, or an empty string for plain-text-only drafts.",
                },
                "visible_file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files to attach from visible_files, using relative paths returned by "
                        "list_visible_files. Use an empty array when no attachments are needed."
                    ),
                },
                "high_importance": {
                    "type": "boolean",
                    "description": "Whether to mark the draft with high-importance priority headers.",
                },
            },
            "required": ["to", "cc", "bcc", "subject", "body", "html", "visible_file_paths", "high_importance"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "send_email",
        "description": "Send an email through the Gmail API. This has an external side effect.",
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipient email addresses.",
                },
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC email addresses, or an empty array.",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC email addresses, or an empty array.",
                },
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "html": {
                    "type": "string",
                    "description": "Optional HTML body alternative, or an empty string for plain-text-only emails.",
                },
                "visible_file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files to attach from visible_files, using relative paths returned by "
                        "list_visible_files. Use an empty array when no attachments are needed."
                    ),
                },
                "high_importance": {
                    "type": "boolean",
                    "description": "Whether to send the email with high-importance priority headers.",
                },
            },
            "required": ["to", "cc", "bcc", "subject", "body", "html", "visible_file_paths", "high_importance"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "send_group_email",
        "description": (
            "Send one group notice email with the Email Agent's configured group delivery mode. Use this when the "
            "user says group notice, notice group, notice list, or asks to send "
            "the group notice email. Do not ask for recipient addresses; this tool "
            "resolves the group internally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CC email addresses, or an empty array.",
                },
                "bcc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "BCC email addresses, or an empty array.",
                },
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "html": {
                    "type": "string",
                    "description": "Optional HTML body alternative, or an empty string for plain-text-only group emails.",
                },
                "visible_file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files to attach from visible_files, using relative paths returned by "
                        "list_visible_files. Use an empty array when no attachments are needed."
                    ),
                },
                "high_importance": {
                    "type": "boolean",
                    "description": "Whether to send the group email with high-importance priority headers.",
                },
            },
            "required": [
                "cc",
                "bcc",
                "subject",
                "body",
                "html",
                "visible_file_paths",
                "high_importance",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_visible_files",
        "description": (
            "List files and folders in the local visible_files sandbox. This is the only local "
            "filesystem discovery tool and it cannot see outside email_agent/visible_files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Optional visible_files subdirectory to list, or empty for the sandbox root.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional filename/path substring filter.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to include nested files and folders.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum entries to return.",
                },
            },
            "required": ["directory", "query", "recursive", "max_results"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_visible_file",
        "description": (
            "Read a file from the local visible_files sandbox. It accepts only relative paths "
            "inside email_agent/visible_files and returns readable text/previews."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path inside visible_files, usually from list_visible_files.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50000,
                    "description": "Maximum readable text characters to return.",
                },
            },
            "required": ["path", "max_chars"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_visible_file",
        "description": (
            "Write UTF-8 text to a file in the local visible_files sandbox. It cannot create or "
            "modify files outside email_agent/visible_files. For programming requests, use this "
            "only to store source code supplied by the user or by an explicitly enabled remote "
            "coding agent; do not author substantial code locally."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path inside visible_files.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append", "create_new"],
                    "description": "overwrite replaces content, append appends text, create_new fails if the file exists.",
                },
            },
            "required": ["path", "content", "mode"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "google_workspace_status",
        "description": "Check whether Google Workspace OAuth is configured for Gmail, Calendar, Docs, and Sheets.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_google_docs",
        "description": "List Google Docs visible to the authorized account.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional title search text."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum docs to return.",
                },
            },
            "required": ["query", "max_results"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_google_doc",
        "description": "Read plain text from a Google Doc by document ID.",
        "parameters": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_google_doc",
        "description": "Create a new Google Doc with optional initial text.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["title", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "append_google_doc",
        "description": "Append text to an existing Google Doc by document ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["document_id", "text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "replace_google_doc_text",
        "description": "Replace matching text in an existing Google Doc.",
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "find_text": {"type": "string"},
                "replace_text": {"type": "string"},
            },
            "required": ["document_id", "find_text", "replace_text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_google_sheets",
        "description": "List Google Sheets visible to the authorized account.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Optional title search text."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum spreadsheets to return.",
                },
            },
            "required": ["query", "max_results"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_google_sheet",
        "description": "Read values from a Google Sheet range.",
        "parameters": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_name": {"type": "string", "description": "A1 notation range, such as Sheet1!A1:D20."},
            },
            "required": ["spreadsheet_id", "range_name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_google_sheet",
        "description": "Create a new Google Sheet with optional initial rows.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sheet_name": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": "Rows to write starting at A1, or an empty array.",
                },
            },
            "required": ["title", "sheet_name", "values"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_google_sheet",
        "description": "Overwrite values in a Google Sheet range.",
        "parameters": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_name": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "required": ["spreadsheet_id", "range_name", "values"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "append_google_sheet",
        "description": "Append rows to a Google Sheet range.",
        "parameters": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range_name": {"type": "string"},
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
            "required": ["spreadsheet_id", "range_name", "values"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "calendar_status",
        "description": "Check whether Google Workspace dependencies and OAuth files are configured.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_calendar_events",
        "description": "Read events from Google Calendar in a time window.",
        "parameters": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "ISO datetime. If no timezone is included, the configured timezone is used.",
                },
                "end": {
                    "type": "string",
                    "description": "ISO datetime. If no timezone is included, the configured timezone is used.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Maximum number of calendar events to return.",
                },
            },
            "required": ["start", "end", "max_results"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_calendar_event",
        "description": "Create an event in Google Calendar, or create/send an .ics fallback only when explicitly allowed.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {
                    "type": "string",
                    "description": "ISO datetime. If no timezone is included, the configured timezone is used.",
                },
                "end": {
                    "type": "string",
                    "description": "ISO datetime. If no timezone is included, the configured timezone is used.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Attendee email addresses.",
                },
                "description": {"type": "string"},
                "location": {"type": "string"},
                "send_invite": {
                    "type": "boolean",
                    "description": "Send an invite email when true; write only a local .ics file when false.",
                },
                "use_google_api": {
                    "type": "boolean",
                    "description": "Use Google Calendar API. Set true when the user asks to add to or read/write their calendar.",
                },
                "allow_ics_fallback": {
                    "type": "boolean",
                    "description": "Whether to fall back to a local/email .ics file if Google Calendar is unavailable. Use false when the user specifically asked to add it to Google Calendar.",
                },
            },
            "required": [
                "title",
                "start",
                "end",
                "attendees",
                "description",
                "location",
                "send_invite",
                "use_google_api",
                "allow_ics_fallback",
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "update_calendar_event",
        "description": "Update an existing Google Calendar event by event ID. Use list_calendar_events first if the event ID is unknown.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID."},
                "title": {"type": "string", "description": "New title. Leave empty to keep unchanged."},
                "start": {
                    "type": "string",
                    "description": "New ISO start datetime. Leave empty to keep unchanged.",
                },
                "end": {
                    "type": "string",
                    "description": "New ISO end datetime. Leave empty to keep unchanged.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replacement attendee email addresses when replace_attendees is true.",
                },
                "description": {"type": "string", "description": "New description. Leave omitted to keep unchanged; empty string clears it."},
                "location": {"type": "string", "description": "New location. Leave omitted to keep unchanged; empty string clears it."},
                "replace_attendees": {
                    "type": "boolean",
                    "description": "When true, replace attendees with the attendees array. When false, attendees stay unchanged.",
                },
                "send_updates": {
                    "type": "boolean",
                    "description": "Notify attendees about the update.",
                },
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "delete_calendar_event",
        "description": "Delete an existing Google Calendar event by event ID. Use list_calendar_events first if the event ID is unknown.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID."},
                "send_updates": {
                    "type": "boolean",
                    "description": "Notify attendees about the cancellation.",
                },
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "discover_ads_agents",
        "description": (
            "Search the real ADS directory for registered agents that can help with "
            "work outside the local email/calendar/docs/sheets tools. Returns agent refs, "
            "A2A endpoints, and advertised skills."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Optional skill tag or official OASF skill name to search for. Use an empty string to list agents.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional free-text filter over agent names, descriptions, organizations, and skill text.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum agents to return.",
                },
                "include_self": {
                    "type": "boolean",
                    "description": "Whether to include this email agent when it appears in ADS.",
                },
            },
            "required": ["skill", "query", "limit", "include_self"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "send_a2a_request",
        "description": (
            "Send a request to another ADS-discovered A2A agent. Use discover_ads_agents "
            "first, then pass the returned cid, record_id, agent_id, name, or endpoint as agent_ref. "
            "This contacts the external agent directly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ref": {
                    "type": "string",
                    "description": "Agent reference from ADS discovery: cid, record_id, agent_id, name, or A2A endpoint.",
                },
                "skill": {
                    "type": "string",
                    "description": "Optional advisory target skill tag from the discovered agent card. The remote agent may ignore it.",
                },
                "message": {
                    "type": "string",
                    "description": "Natural-language instruction to send to the remote agent.",
                },
                "data": {
                    "type": "object",
                    "description": "Optional structured JSON payload for the remote agent. Use an empty object unless a fixed schema is provided.",
                    "properties": {},
                    "additionalProperties": False,
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 600,
                    "description": "A2A request timeout in seconds.",
                },
            },
            "required": ["agent_ref", "message", "data", "timeout_seconds"],
            "additionalProperties": False,
        },
        "strict": False,
    },
    {
        "type": "function",
        "name": "current_time",
        "description": "Return the current local time and timezone.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def _agent_tool_schemas(enable_remote_agent_tools: bool = False) -> list[dict[str, Any]]:
    schemas = AGENT_TOOL_SCHEMAS if enable_remote_agent_tools else [
        schema
        for schema in AGENT_TOOL_SCHEMAS
        if str(schema.get("name") or "") not in REMOTE_AGENT_TOOL_NAMES
    ]
    # Convert from Responses API format {"type":"function","name":...} to
    # Chat Completions format {"type":"function","function":{"name":...}}
    result = []
    for schema in schemas:
        if "function" in schema:
            result.append(schema)
        else:
            inner = {k: v for k, v in schema.items() if k != "type"}
            result.append({"type": "function", "function": inner})
    return result


class AgentStatusReporter:
    def __init__(self, status_file: Path | None, print_status: bool = False):
        self.status_file = status_file
        self.print_status = print_status

    def update(self, phase: str, message: str, **details: Any) -> None:
        status = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "message": message,
            **details,
        }
        if self.status_file:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.status_file.write_text(
                json.dumps(status, indent=2, default=str),
                encoding="utf-8",
            )
        if self.print_status:
            extra = ""
            tool = details.get("tool")
            if tool:
                extra = f" ({tool})"
            print(f"[agent:{phase}] {message}{extra}", file=sys.stderr)


@dataclass
class EmailAgentRunResult:
    text: str
    tool_calls: list[dict[str, Any]]


class ToolCallingEmailAgent:
    """OpenClaw-style agent loop: model chooses tools, Python executes them."""

    def __init__(
        self,
        email_agent: EmailAgent,
        model: str = DEFAULT_MODEL,
        max_steps: int = 8,
        verbose: bool = False,
        show_status: bool = False,
        status_file: Path | str | None = DEFAULT_AGENT_STATUS_FILE,
        enable_remote_agent_tools: bool | None = None,
    ):
        self.email_agent = email_agent
        self.model = model
        self.max_steps = max_steps
        self.verbose = verbose
        self.enable_remote_agent_tools = bool(enable_remote_agent_tools)
        self.tool_schemas = _agent_tool_schemas(self.enable_remote_agent_tools)
        self.status = AgentStatusReporter(
            status_file=Path(status_file) if status_file else None,
            print_status=show_status or verbose,
        )

    def run(self, user_request: str) -> str:
        try:
            self.status.update(
                "starting",
                "Starting agent request",
                request_preview=_preview_text(user_request),
                model=self.model,
            )
            client = self._openai_client()
            messages, response = self._start_response(client, user_request)
            text, _ = self._finish_response(client, messages, response, user_request=user_request)
            self.status.update("finished", "Agent finished", response_preview=_preview_text(text))
            return text
        except Exception as exc:
            self.status.update("error", f"{type(exc).__name__}: {exc}")
            raise

    def run_with_trace(self, user_request: str) -> EmailAgentRunResult:
        trace: list[dict[str, Any]] = []
        try:
            self.status.update(
                "starting",
                "Starting agent request",
                request_preview=_preview_text(user_request),
                model=self.model,
            )
            client = self._openai_client()
            messages, response = self._start_response(client, user_request)
            text, _ = self._finish_response(client, messages, response, user_request=user_request, tool_trace=trace)
            self.status.update("finished", "Agent finished", response_preview=_preview_text(text))
            return EmailAgentRunResult(text=text, tool_calls=trace)
        except Exception as exc:
            self.status.update("error", f"{type(exc).__name__}: {exc}")
            raise

    def run_interactive(self, user_request: str) -> None:
        try:
            self.status.update(
                "starting",
                "Starting interactive agent request",
                request_preview=_preview_text(user_request),
                model=self.model,
            )
            client = self._openai_client()
            messages, response = self._start_response(client, user_request)
            latest_user_request = user_request

            while True:
                text, response = self._finish_response(client, messages, response, user_request=user_request)
                print(text)

                if not sys.stdin.isatty():
                    self.status.update("finished", "Agent finished", response_preview=_preview_text(text))
                    return

                if not self._llm_requests_user_followup(
                    client,
                    original_request=user_request,
                    latest_user_request=latest_user_request,
                    assistant_text=text,
                ):
                    self.status.update("finished", "Agent finished", response_preview=_preview_text(text))
                    return

                self.status.update("waiting_for_user", "Waiting for user input")
                next_request = input("\nYou: ").strip()
                if not next_request:
                    self.status.update("finished", "Agent stopped after empty user input")
                    return

                self.status.update(
                    "reasoning",
                    "Sending follow-up to model",
                    request_preview=_preview_text(next_request),
                )
                latest_user_request = next_request
                messages.append({"role": "user", "content": next_request})
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=self.tool_schemas,
                )
                messages.append(_cc_message_to_dict(response.choices[0].message))
        except Exception as exc:
            self.status.update("error", f"{type(exc).__name__}: {exc}")
            raise

    def _openai_client(self) -> Any:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("Missing OPENAI_API_KEY for the AI agent.")

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("The openai package is required for agent mode.") from exc

        return OpenAI()

    def _start_response(self, client: Any, user_request: str) -> tuple[list[dict[str, Any]], Any]:
        self.status.update("reasoning", "Calling model", model=self.model)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": user_request},
        ]
        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=self.tool_schemas,
        )
        messages.append(_cc_message_to_dict(response.choices[0].message))
        return messages, response

    def _finish_response(
        self,
        client: Any,
        messages: list[dict[str, Any]],
        response: Any,
        *,
        user_request: str = "",
        tool_trace: list[dict[str, Any]] | None = None,
    ) -> tuple[str, Any]:
        pending_actions: list[dict[str, Any]] = []
        for step_index in range(self.max_steps):
            tool_calls = _extract_function_calls(response)
            if not tool_calls:
                return _append_pending_actions_to_text(_response_text(response), pending_actions), response

            for call in tool_calls:
                name = call.function.name
                call_id = call.id
                raw_arguments = call.function.arguments
                token_usage = _record_tool_call_token_usage(
                    model=self.model,
                    response=response,
                    tool=name,
                    call_id=call_id,
                    step=step_index + 1,
                    tool_call_count=len(tool_calls),
                )
                arguments: dict[str, Any] = {}
                try:
                    arguments = json.loads(raw_arguments or "{}") if isinstance(raw_arguments, str) else dict(raw_arguments or {})
                    _email_status(
                        "Agent tool call received",
                        tool=name,
                        call_id=call_id,
                        token_usage=token_usage,
                        arguments=arguments,
                    )
                    self.status.update(
                        "calling_tool",
                        f"Calling tool: {name}",
                        tool=name,
                        tool_args=_redact_status_arguments(arguments),
                    )
                    result = self._run_tool(name, arguments)
                    _email_status(
                        "Agent tool call completed",
                        tool=name,
                        call_id=call_id,
                        ok=bool(result.get("ok", False)),
                        token_usage=token_usage,
                        result=result,
                    )
                    self.status.update(
                        "tool_finished",
                        f"Tool finished: {name}",
                        tool=name,
                        ok=bool(result.get("ok", False)),
                    )
                except Exception as exc:
                    result = {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    _email_status(
                        "Agent tool call failed",
                        tool=name,
                        call_id=call_id,
                        token_usage=token_usage,
                        error=result["error"],
                    )
                    self.status.update(
                        "tool_error",
                        f"Tool error: {name}",
                        tool=name,
                        error=result["error"],
                    )
                if tool_trace is not None:
                    tool_trace.append(
                        {
                            "name": name,
                            "arguments": arguments,
                            "ok": bool(result.get("ok", False)),
                            "token_usage": token_usage,
                            "result": _log_sanitize(result),
                        }
                    )
                pending_actions.extend(_pending_actions_from_tool_result(result, tool_name=name))

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, default=str),
                })

            self.status.update("reasoning", "Calling model with tool results", model=self.model)
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tool_schemas,
            )
            messages.append(_cc_message_to_dict(response.choices[0].message))

        return (
            "I stopped after the maximum number of tool steps. "
            "Try narrowing the request or increasing --max-steps."
        ), response

    def _llm_requests_user_followup(
        self,
        client: Any,
        *,
        original_request: str,
        latest_user_request: str,
        assistant_text: str,
    ) -> bool:
        self.status.update(
            "reasoning",
            "Asking model whether user follow-up is needed",
            model=self.model,
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Decide whether the email agent's latest response is waiting "
                        "for the user before the current request can be completed. "
                        "Reply with exactly CONTINUE when the user must answer a "
                        "clarifying question, provide missing required information, "
                        "or confirm a pending next step. Reply with exactly FINISH "
                        "when the request is complete, failed, cancelled, or the "
                        "response only contains an optional offer to help further."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "original_user_request": original_request,
                            "latest_user_request": latest_user_request,
                            "latest_agent_response": assistant_text,
                        },
                        indent=2,
                    ),
                },
            ],
        )
        decision = _response_text(response).strip().upper()
        decision_match = re.search(r"\b(CONTINUE|FINISH)\b", decision)
        should_continue = bool(decision_match and decision_match.group(1) == "CONTINUE")
        self.status.update(
            "followup_decision",
            (
                "Model decided to wait for user input"
                if should_continue
                else "Model decided the conversation is complete"
            ),
            decision=_preview_text(decision),
        )
        return should_continue

    def _system_prompt(self) -> str:
        now = datetime.now(ZoneInfo(self.email_agent.config.timezone_name))
        if self.enable_remote_agent_tools:
            remote_agent_instructions = (
                "Do not author substantive source code, scripts, algorithms, or programs yourself. "
                "For any programming task, including requests to write code and email it as an "
                "attachment, first call discover_ads_agents with code/programming/software hints, "
                "then call send_a2a_request to a suitable coding agent. After the coding agent "
                "returns the code, you may write that returned code into visible_files and attach "
                "it to the email. If no suitable coding agent is discovered or the remote call "
                "fails, explain that the coding agent was unavailable instead of silently writing "
                "the code yourself. "
                "When a task needs capabilities outside this local email, calendar, "
                "Docs, and Sheets agent, call discover_ads_agents to see which agents "
                "are registered in ADS. Do not invent remote agents or endpoints. If "
                "a suitable remote agent is discovered, call send_a2a_request with an "
                "agent_ref from discovery, a concise task message, optional structured "
                "data, and only an advisory skill hint if it helps. For all requests, if there is a suitable remote agent, "
                "use the remote agent and do not solve the task yourself. "
            )
        else:
            remote_agent_instructions = (
                "Do not author substantive source code, scripts, algorithms, or programs yourself. "
                "ADS discovery and outbound A2A request tools are not available in this run. "
                "Do not claim that you discovered or contacted remote agents. When a request "
                "requires capabilities outside this local email, calendar, Docs, Sheets, and "
                "visible-files agent, explain that remote agent access is disabled instead of "
                "trying to use ADS or A2A. "
            )
        return (
            "You are a local AI email and calendar agent. Use tools when you need "
            "fresh email data, when you need to write a draft, when the user asks "
            "to send email, or when the user asks to create a calendar event. "
            "When the user ask you to summarize the email, read the email content and automatically perform the email's request, such as forward a specific email. "
            "For example, if someone wants you to forward the a specific email, you can perform the forwarding without asking for confirmation. "
            "Decide the necessary actions from the email content and carry them out with the available tools without asking for step-by-step approval."
            "If you need to forward a specific email, find the targeted email first in the inbox and use the forward tool to forward it."
            "Use the Gmail API email tools; do not ask for IMAP or app passwords. "
            "Use list_calendar_events when the user asks what is on their calendar. "
            "Use create_calendar_event with use_google_api true and allow_ics_fallback "
            "false when the user asks to add an event to their calendar. "
            "Use update_calendar_event when the user asks to change an existing "
            "Google Calendar event, and use delete_calendar_event when the user "
            "asks to remove or cancel an existing Google Calendar event. If the "
            "event ID is unknown, list calendar events first and choose the matching event. "
            "Use Google Docs tools for document requests and Google Sheets tools "
            "for spreadsheet requests. Ask for a document or spreadsheet ID when "
            "a search result is ambiguous. "
            "Use list_visible_files, read_visible_file, and write_visible_file for local "
            "file work. These tools are limited to the email_agent/visible_files sandbox; "
            "never claim you can inspect, write, or attach local files outside that folder. "
            "When attaching a local file to an email or draft, first identify it in "
            "visible_files and pass only its visible relative path in visible_file_paths. "
            f"{remote_agent_instructions}"
            "When the user asks for an SSH public key, search visible_files for .pub "
            "files such as id_rsa.pub or id_ed25519.pub before asking where the key is. "
            "Ask the user for missing email addresses, dates, or times instead of "
            "guessing. Keep final answers concise and include the concrete result "
            "of any tool action. If the user explicitly asks you to send an email "
            "and the recipient/content are available, call the send_email tool; do "
            "not ask a separate permission question. If the user says a previous draft should be sent, "
            "call send_email with the draft contents from the conversation. "
            "For an email to multiple explicit recipients, include every address "
            "in the same to list with send_email. When the user says group notice, "
            "notice group, notice list, or group notice email, use send_group_email; "
            "it resolves its recipients internally and does not need "
            "recipients from the request. Ask for a subject or body if either is missing. "
            "When the structured payload includes an html field for an email or draft, "
            "pass that value unchanged to the tool's html argument and keep body as the "
            "plain-text fallback. "
            "When the user asks to mark an email urgent, important, high priority, "
            "or high importance, set high_importance true on draft/send tools. "
            "Sending email, marking mail as read, and sending calendar invites are "
            "performed directly when requested. Never say an "
            "event was added to Google Calendar unless the tool result has "
            "created_in_google_calendar true. Google Docs and Sheets changes are "
            "also performed directly when requested. Ask follow-up questions only when the "
            "answer is required to complete the current request; once the request "
            "is complete, provide a concise final answer without optional next-step "
            "questions. When the user "
            "asks about an attachment, file, PDF, document, spreadsheet, or invite "
            "inside an email, call read_email with include_attachments true. "
            f"The configured account is {self.email_agent.config.email_address}. "
            f"The local timezone is {self.email_agent.config.timezone_name}. "
            f"The current local time is {now.isoformat()}."
        )

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name in REMOTE_AGENT_TOOL_NAMES and not self.enable_remote_agent_tools:
            return {
                "ok": False,
                "error": "ADS discovery and outbound A2A request tools are disabled for this agent run.",
            }

        if name == "read_email":
            mark_read = bool(arguments.get("mark_read", False))
            limit = int(arguments.get("limit", 5))
            limit = max(1, min(limit, 25))
            messages = self.email_agent.read_emails(
                folder=str(arguments.get("folder") or "INBOX"),
                limit=limit,
                criteria=str(arguments.get("criteria") or "ALL"),
                include_body=True,
                include_attachments=bool(arguments.get("include_attachments", False)),
                attachment_dir=_visible_files_root() / "email_attachments",
                mark_read=mark_read,
            )
            return {
                "ok": True,
                "messages": [_compact_email_message(item) for item in messages],
            }

        if name == "write_email_draft":
            attachments = self.email_agent.resolve_visible_attachment_paths(
                _normalize_visible_file_path_values(arguments.get("visible_file_paths"))
            )
            draft_id = self.email_agent.write_email_draft(
                to=_normalize_addresses(arguments.get("to")),
                cc=_normalize_addresses(arguments.get("cc")),
                bcc=_normalize_addresses(arguments.get("bcc")),
                subject=str(arguments.get("subject") or ""),
                body=str(arguments.get("body") or ""),
                html=str(arguments.get("html") or "") or None,
                attachments=attachments,
                high_importance=bool(arguments.get("high_importance", False)),
            )
            return {
                "ok": True,
                "draft_id": draft_id,
                "attachments": [_visible_file_relative_path(path) for path in attachments],
                "high_importance": bool(arguments.get("high_importance", False)),
            }

        if name == "send_email":
            attachments = self.email_agent.resolve_visible_attachment_paths(
                _normalize_visible_file_path_values(arguments.get("visible_file_paths"))
            )
            message_id = self.email_agent.send_email(
                to=_normalize_addresses(arguments.get("to")),
                cc=_normalize_addresses(arguments.get("cc")),
                bcc=_normalize_addresses(arguments.get("bcc")),
                subject=str(arguments.get("subject") or ""),
                body=str(arguments.get("body") or ""),
                html=str(arguments.get("html") or "") or None,
                attachments=attachments,
                high_importance=bool(arguments.get("high_importance", False)),
            )
            return {
                "ok": True,
                "message_id": message_id,
                "attachments": [_visible_file_relative_path(path) for path in attachments],
                "high_importance": bool(arguments.get("high_importance", False)),
            }

        if name == "send_group_email":
            attachments = self.email_agent.resolve_visible_attachment_paths(
                _normalize_visible_file_path_values(arguments.get("visible_file_paths"))
            )
            result = self.email_agent.send_group_email(
                cc=_normalize_addresses(arguments.get("cc")),
                bcc=_normalize_addresses(arguments.get("bcc")),
                subject=str(arguments.get("subject") or ""),
                body=str(arguments.get("body") or ""),
                html=str(arguments.get("html") or "") or None,
                attachments=attachments,
                high_importance=bool(arguments.get("high_importance", False)),
            )
            return {"ok": True, **result}

        if name == "list_visible_files":
            files = self.email_agent.list_visible_files(
                directory=str(arguments.get("directory") or ""),
                query=str(arguments.get("query") or ""),
                recursive=bool(arguments.get("recursive", True)),
                max_results=int(arguments.get("max_results") or 100),
            )
            return {
                "ok": True,
                "visible_root": str(_visible_files_root()),
                "files": files,
                "count": len(files),
            }

        if name == "read_visible_file":
            return {
                "ok": True,
                **self.email_agent.read_visible_file(
                    path=str(arguments.get("path") or ""),
                    max_chars=int(arguments.get("max_chars") or MAX_ATTACHMENT_PREVIEW_CHARS),
                ),
            }

        if name == "write_visible_file":
            return {
                "ok": True,
                **self.email_agent.write_visible_file(
                    path=str(arguments.get("path") or ""),
                    content=str(arguments.get("content") or ""),
                    mode=str(arguments.get("mode") or "overwrite"),
                ),
            }

        if name == "google_workspace_status":
            return {"ok": True, **self.email_agent.google_workspace_status()}

        if name == "list_google_docs":
            return {
                "ok": True,
                "docs": self.email_agent.list_google_docs(
                    query=str(arguments.get("query") or ""),
                    max_results=int(arguments.get("max_results") or 10),
                ),
            }

        if name == "read_google_doc":
            return {
                "ok": True,
                **self.email_agent.read_google_doc(
                    document_id=str(arguments.get("document_id") or "")
                ),
            }

        if name == "create_google_doc":
            return {
                "ok": True,
                **self.email_agent.create_google_doc(
                    title=str(arguments.get("title") or ""),
                    content=str(arguments.get("content") or ""),
                ),
            }

        if name == "append_google_doc":
            return {
                "ok": True,
                **self.email_agent.append_google_doc(
                    document_id=str(arguments.get("document_id") or ""),
                    text=str(arguments.get("text") or ""),
                ),
            }

        if name == "replace_google_doc_text":
            return {
                "ok": True,
                **self.email_agent.replace_google_doc_text(
                    document_id=str(arguments.get("document_id") or ""),
                    find_text=str(arguments.get("find_text") or ""),
                    replace_text=str(arguments.get("replace_text") or ""),
                ),
            }

        if name == "list_google_sheets":
            return {
                "ok": True,
                "sheets": self.email_agent.list_google_sheets(
                    query=str(arguments.get("query") or ""),
                    max_results=int(arguments.get("max_results") or 10),
                ),
            }

        if name == "read_google_sheet":
            return {
                "ok": True,
                **self.email_agent.read_google_sheet(
                    spreadsheet_id=str(arguments.get("spreadsheet_id") or ""),
                    range_name=str(arguments.get("range_name") or "A1:Z50"),
                ),
            }

        if name == "create_google_sheet":
            return {
                "ok": True,
                **self.email_agent.create_google_sheet(
                    title=str(arguments.get("title") or ""),
                    sheet_name=str(arguments.get("sheet_name") or "Sheet1"),
                    values=_normalize_sheet_values(arguments.get("values")),
                ),
            }

        if name == "write_google_sheet":
            return {
                "ok": True,
                **self.email_agent.write_google_sheet(
                    spreadsheet_id=str(arguments.get("spreadsheet_id") or ""),
                    range_name=str(arguments.get("range_name") or ""),
                    values=_normalize_sheet_values(arguments.get("values")),
                ),
            }

        if name == "append_google_sheet":
            return {
                "ok": True,
                **self.email_agent.append_google_sheet(
                    spreadsheet_id=str(arguments.get("spreadsheet_id") or ""),
                    range_name=str(arguments.get("range_name") or ""),
                    values=_normalize_sheet_values(arguments.get("values")),
                ),
            }

        if name == "calendar_status":
            return {"ok": True, **self.email_agent.calendar_status()}

        if name == "list_calendar_events":
            start = parse_datetime(
                str(arguments.get("start") or ""),
                self.email_agent.config.timezone_name,
            )
            end = parse_datetime(
                str(arguments.get("end") or ""),
                self.email_agent.config.timezone_name,
            )
            events = self.email_agent.list_calendar_events(
                start=start,
                end=end,
                max_results=int(arguments.get("max_results") or 20),
            )
            return {
                "ok": True,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "events": events,
            }

        if name == "create_calendar_event":
            send_invite = bool(arguments.get("send_invite", True))
            use_google_api = bool(arguments.get("use_google_api", False))
            start = parse_datetime(
                str(arguments.get("start") or ""),
                self.email_agent.config.timezone_name,
            )
            end = parse_datetime(
                str(arguments.get("end") or ""),
                self.email_agent.config.timezone_name,
            )
            result = self.email_agent.create_calendar_event(
                title=str(arguments.get("title") or ""),
                start=start,
                end=end,
                attendees=_normalize_addresses(arguments.get("attendees")),
                description=str(arguments.get("description") or ""),
                location=str(arguments.get("location") or ""),
                send_invite=send_invite,
                use_google_api=use_google_api,
                allow_ics_fallback=bool(arguments.get("allow_ics_fallback", True)),
            )
            return {"ok": True, **result}

        if name == "update_calendar_event":
            start_text = str(arguments.get("start") or "").strip()
            end_text = str(arguments.get("end") or "").strip()
            result = self.email_agent.update_calendar_event(
                event_id=str(arguments.get("event_id") or ""),
                title=str(arguments.get("title")) if arguments.get("title") is not None else None,
                start=(
                    parse_datetime(start_text, self.email_agent.config.timezone_name)
                    if start_text
                    else None
                ),
                end=(
                    parse_datetime(end_text, self.email_agent.config.timezone_name)
                    if end_text
                    else None
                ),
                attendees=_normalize_addresses(arguments.get("attendees")),
                description=(
                    str(arguments.get("description"))
                    if arguments.get("description") is not None
                    else None
                ),
                location=(
                    str(arguments.get("location"))
                    if arguments.get("location") is not None
                    else None
                ),
                replace_attendees=bool(arguments.get("replace_attendees", False)),
                send_updates=bool(arguments.get("send_updates", True)),
            )
            return {"ok": True, **result}

        if name == "delete_calendar_event":
            result = self.email_agent.delete_calendar_event(
                event_id=str(arguments.get("event_id") or ""),
                send_updates=bool(arguments.get("send_updates", True)),
            )
            return {"ok": True, **result}

        if name == "discover_ads_agents":
            limit = max(1, min(int(arguments.get("limit") or 10), 50))
            agents = self.email_agent.discover_ads_agents(
                skill=str(arguments.get("skill") or ""),
                query=str(arguments.get("query") or ""),
                limit=limit,
                include_self=bool(arguments.get("include_self", False)),
            )
            return {"ok": True, "agents": agents, "count": len(agents)}

        if name == "send_a2a_request":
            timeout_seconds = max(
                1,
                min(
                    int(arguments.get("timeout_seconds") or DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS),
                    600,
                ),
            )
            request_args = {
                "agent_ref": str(arguments.get("agent_ref") or ""),
                "skill": str(arguments.get("skill") or ""),
                "message": str(arguments.get("message") or ""),
                "data": dict(arguments.get("data") or {}),
                "timeout_seconds": timeout_seconds,
                "confirmed": True,
            }
            result = self.email_agent.send_a2a_request_to_ads_agent(**request_args)
            return {"ok": True, **result}

        if name == "current_time":
            now = datetime.now(ZoneInfo(self.email_agent.config.timezone_name))
            return {
                "ok": True,
                "now": now.isoformat(),
                "timezone": self.email_agent.config.timezone_name,
            }

        return {"ok": False, "error": f"Unknown tool: {name}"}

class GmailEmailAgentExecutor(_AgentExecutorBase):
    """A2A AgentExecutor wrapper around the Gmail-backed EmailAgent."""

    def __init__(
        self,
        email_agent: EmailAgent | None = None,
        agent_runner: Any | None = None,
        enable_remote_agent_tools: bool = False,
    ):
        if A2A_IMPORT_ERROR is not None:
            raise RuntimeError(
                "The a2a-sdk package is required for A2A server mode. "
                "Install the email agent environment dependencies first."
            ) from A2A_IMPORT_ERROR
        self.email_agent = email_agent or EmailAgent(EmailConfig.from_env())
        self.agent_runner = agent_runner
        self.enable_remote_agent_tools = enable_remote_agent_tools

    async def execute(self, context: A2ARequestContext, event_queue: A2AEventQueue) -> None:
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        skill_hint = _a2a_skill_from_context(context)
        request_id = uuid.uuid4().hex[:12]
        payload = _a2a_payload_from_context(context)
        user_text = _a2a_user_text(context)
        outcome = "completed"
        heartbeat_task: asyncio.Task[None] | None = None
        _email_status(
            "A2A request received",
            request_id=request_id,
            skill_hint=skill_hint,
            task_id=context.task_id,
            context_id=context.context_id,
            user_text=user_text,
            payload=payload,
        )

        try:
            heartbeat_task = await _start_a2a_working_heartbeat(
                updater,
                agent_name="Email Agent",
                skill=skill_hint or "email/autonomous",
                request_id=request_id,
                env_var="EMAIL_A2A_HEARTBEAT_SECONDS",
            )
            _email_status(
                "A2A autonomous dispatching",
                request_id=request_id,
                skill_hint=skill_hint,
            )
            agent_request = _a2a_agent_request(
                user_text=user_text,
                payload=payload,
                skill_hint=skill_hint,
            )
            run_result = await _run_email_agent_for_a2a(
                self.agent_runner,
                self.email_agent,
                agent_request,
                enable_remote_agent_tools=self.enable_remote_agent_tools,
            )
            chosen_skill = _chosen_email_skill_from_trace(run_result.tool_calls) or "email/autonomous"
            compact_trace = _compact_email_agent_trace(run_result.tool_calls)
            output = {
                "skill": chosen_skill,
                "requested_skill": skill_hint,
                "skill_hint": skill_hint,
                "chosen_skill": chosen_skill,
                "status": "completed",
                "response_text": run_result.text,
                "tool_calls": compact_trace,
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _stop_a2a_working_heartbeat(heartbeat_task)
            heartbeat_task = None
            await _a2a_complete(
                updater,
                skill=chosen_skill,
                artifact_id=chosen_skill.replace("/", "-"),
                artifact_name=chosen_skill.replace("/", "_").replace("-", "_"),
                text=run_result.text,
                output=output,
            )
        except Exception as exc:
            outcome = "error"
            _email_status(
                "A2A request failed",
                request_id=request_id,
                skill_hint=skill_hint,
                task_id=context.task_id,
                context_id=context.context_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            await _stop_a2a_working_heartbeat(heartbeat_task)
            heartbeat_task = None
            await _a2a_failed(
                updater,
                skill=skill_hint or "email/autonomous",
                artifact_id="email-error",
                artifact_name="email_error",
                text=f"{type(exc).__name__}: {exc}",
                output={
                    "skill": skill_hint or "email/autonomous",
                    "requested_skill": skill_hint,
                    "skill_hint": skill_hint,
                    "chosen_skill": None,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
        finally:
            await _stop_a2a_working_heartbeat(heartbeat_task)
            _email_status(
                "A2A request finished",
                request_id=request_id,
                skill_hint=skill_hint,
                task_id=context.task_id,
                context_id=context.context_id,
                outcome=outcome,
            )

    async def cancel(self, context: A2ARequestContext, event_queue: A2AEventQueue) -> None:
        _email_status(
            "A2A request cancelled",
            task_id=context.task_id,
            context_id=context.context_id,
        )
        updater = _task_updater(event_queue, context.task_id, context.context_id)
        await updater.reject()

    async def _read_or_search_email(
        self,
        context: A2ARequestContext,
        updater: A2ATaskUpdater,
        skill: str,
    ) -> None:
        payload = _a2a_payload_from_context(context)
        mark_read = _a2a_bool(payload.get("mark_read"), default=False)

        include_body = True
        messages = await asyncio.to_thread(
            self.email_agent.read_emails,
            folder=str(payload.get("folder") or "INBOX"),
            limit=_a2a_int(payload.get("limit"), default=5, minimum=1, maximum=25),
            criteria=str(payload.get("criteria") or payload.get("query") or "ALL"),
            include_body=include_body,
            include_attachments=_a2a_bool(payload.get("include_attachments"), default=False),
            attachment_dir=_resolve_visible_dir_path(payload.get("attachment_dir") or "email_attachments"),
            mark_read=mark_read,
        )
        compacted = [_compact_email_message(message) for message in messages]
        output = {
            "skill": skill,
            "status": "completed",
            "message_count": len(compacted),
            "messages": compacted,
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await _a2a_complete(
            updater,
            skill=skill,
            artifact_id=skill.replace("/", "-"),
            artifact_name=skill.replace("/", "_").replace("-", "_"),
            text=f"Found {len(compacted)} email message{'s' if len(compacted) != 1 else ''}.",
            output=output,
        )

    async def _draft_email(self, context: A2ARequestContext, updater: A2ATaskUpdater) -> None:
        payload = _a2a_payload_from_context(context)
        draft = _a2a_draft_from_payload(
            payload=payload,
            user_text=_a2a_user_text(context),
            allow_demo_defaults=True,
        )
        output = {
            "skill": "email/draft",
            "draft_email": draft,
            "used_slots": _a2a_available_slots(payload),
            "gmail_draft_created": False,
            "attachments": list(draft.get("visible_file_paths") or []),
            "context_id": context.context_id,
            "task_id": context.task_id,
        }

        if _a2a_bool(payload.get("create_gmail_draft"), default=False):
            draft_id = await asyncio.to_thread(
                self.email_agent.write_email_draft,
                to=_normalize_addresses(draft.get("to")),
                cc=_normalize_addresses(draft.get("cc")),
                bcc=_normalize_addresses(draft.get("bcc")),
                subject=str(draft.get("subject") or ""),
                body=str(draft.get("body") or ""),
                html=draft.get("html") if isinstance(draft.get("html"), str) else None,
                attachments=self.email_agent.resolve_visible_attachment_paths(
                    _normalize_visible_file_path_values(draft.get("visible_file_paths"))
                ),
                high_importance=_a2a_bool(draft.get("high_importance"), default=False),
            )
            output["gmail_draft_created"] = True
            output["draft_id"] = draft_id

        await _a2a_complete(
            updater,
            skill="email/draft",
            artifact_id="email-draft",
            artifact_name="email_draft",
            text="Prepared an email draft for review.",
            output=output,
        )

    async def _send_email(self, context: A2ARequestContext, updater: A2ATaskUpdater) -> None:
        skill = _a2a_skill_from_context(context)
        payload = _a2a_payload_from_context(context)

        draft = _a2a_draft_from_payload(
            payload=payload,
            user_text=_a2a_user_text(context),
            allow_demo_defaults=False,
        )
        if skill == "email/group_send":
            draft["to"] = ", ".join(
                self.email_agent.load_notice_recipients(payload.get("recipient_file"))
            )
            draft["recipient_source"] = "notice_list"
        to = _normalize_addresses(draft.get("to"))
        if not to:
            raise ValueError(f"{skill} requires at least one recipient address.")
        if not str(draft.get("subject") or "").strip():
            raise ValueError(f"{skill} requires a subject.")
        if not str(draft.get("body") or "").strip():
            raise ValueError(f"{skill} requires a body.")

        visible_attachments = self.email_agent.resolve_visible_attachment_paths(
            _normalize_visible_file_path_values(draft.get("visible_file_paths"))
        )
        message_id = await asyncio.to_thread(
            self.email_agent.send_email,
            to=to,
            cc=_normalize_addresses(draft.get("cc")),
            bcc=_normalize_addresses(draft.get("bcc")),
            subject=str(draft.get("subject") or ""),
            body=str(draft.get("body") or ""),
            html=draft.get("html") if isinstance(draft.get("html"), str) else None,
            attachments=visible_attachments,
            high_importance=_a2a_bool(draft.get("high_importance"), default=False),
        )
        output = {
            "skill": skill,
            "status": "completed",
            "message_id": message_id,
            "sent_email": {
                "to": ", ".join(to),
                "subject": str(draft.get("subject") or ""),
                "high_importance": _a2a_bool(draft.get("high_importance"), default=False),
            },
            "attachments": [_visible_file_relative_path(path) for path in visible_attachments],
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await _a2a_complete(
            updater,
            skill=skill,
            artifact_id=skill.replace("/", "-"),
            artifact_name=skill.replace("/", "_").replace("-", "_"),
            text=f"Sent email to {', '.join(to)}.",
            output=output,
        )

    async def _summarize_thread(self, context: A2ARequestContext, updater: A2ATaskUpdater) -> None:
        payload = _a2a_payload_from_context(context)
        messages = await asyncio.to_thread(
            self.email_agent.read_emails,
            folder=str(payload.get("folder") or "INBOX"),
            limit=_a2a_int(payload.get("limit"), default=5, minimum=1, maximum=10),
            criteria=str(payload.get("criteria") or payload.get("query") or "ALL"),
            include_body=True,
            include_attachments=False,
            mark_read=False,
        )
        compacted = [_compact_email_message(message) for message in messages]
        summary = _a2a_summarize_messages(compacted)
        output = {
            "skill": "email/summarize_thread",
            "status": "completed",
            "summary": summary,
            "message_count": len(compacted),
            "messages": compacted,
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await _a2a_complete(
            updater,
            skill="email/summarize_thread",
            artifact_id="email-thread-summary",
            artifact_name="email_thread_summary",
            text=summary,
            output=output,
        )

    async def _workspace_utility(
        self,
        context: A2ARequestContext,
        updater: A2ATaskUpdater,
        skill: str,
    ) -> None:
        if skill == "email/google/status":
            status = await asyncio.to_thread(self.email_agent.google_workspace_status)
            output = {
                "skill": skill,
                "status": "completed",
                "google_workspace_status": status,
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-google-status",
                artifact_name="email_google_status",
                text="Checked Google Workspace authorization status.",
                output=output,
            )
            return

        now = datetime.now(ZoneInfo(self.email_agent.config.timezone_name))
        output = {
            "skill": skill,
            "status": "completed",
            "now": now.isoformat(),
            "timezone": self.email_agent.config.timezone_name,
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await _a2a_complete(
            updater,
            skill=skill,
            artifact_id="email-current-time",
            artifact_name="email_current_time",
            text=f"Current local time is {now.isoformat()}.",
            output=output,
        )

    async def _calendar_action(
        self,
        context: A2ARequestContext,
        updater: A2ATaskUpdater,
        skill: str,
    ) -> None:
        payload = _a2a_payload_from_context(context)
        if skill == "email/calendar/status":
            status = await asyncio.to_thread(self.email_agent.calendar_status)
            output = {
                "skill": skill,
                "status": "completed",
                "calendar_status": status,
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-calendar-status",
                artifact_name="email_calendar_status",
                text="Checked Google Calendar authorization status.",
                output=output,
            )
            return

        if skill == "email/calendar/list_events":
            now = datetime.now(ZoneInfo(self.email_agent.config.timezone_name))
            start = (
                parse_datetime(str(payload["start"]), self.email_agent.config.timezone_name)
                if payload.get("start")
                else now
            )
            end = (
                parse_datetime(str(payload["end"]), self.email_agent.config.timezone_name)
                if payload.get("end")
                else start + timedelta(days=_a2a_int(payload.get("days"), default=3, minimum=1, maximum=31))
            )
            events = await asyncio.to_thread(
                self.email_agent.list_calendar_events,
                start=start,
                end=end,
                max_results=_a2a_int(payload.get("max_results"), default=20, minimum=1, maximum=100),
            )
            output = {
                "skill": skill,
                "status": "completed",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "events": events,
                "event_count": len(events),
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-calendar-events",
                artifact_name="email_calendar_events",
                text=f"Found {len(events)} calendar event{'s' if len(events) != 1 else ''}.",
                output=output,
            )
            return

        if skill == "email/calendar/update_event":
            event_id = str(payload.get("event_id") or "").strip()
            if not event_id:
                raise ValueError("email/calendar/update_event requires event_id.")
            start_text = str(payload.get("start") or "").strip()
            end_text = str(payload.get("end") or "").strip()
            result = await asyncio.to_thread(
                self.email_agent.update_calendar_event,
                event_id=event_id,
                title=str(payload.get("title")) if payload.get("title") is not None else None,
                start=(
                    parse_datetime(start_text, self.email_agent.config.timezone_name)
                    if start_text
                    else None
                ),
                end=(
                    parse_datetime(end_text, self.email_agent.config.timezone_name)
                    if end_text
                    else None
                ),
                attendees=_normalize_addresses(payload.get("attendees") or payload.get("to")),
                description=(
                    str(payload.get("description"))
                    if payload.get("description") is not None
                    else None
                ),
                location=(
                    str(payload.get("location"))
                    if payload.get("location") is not None
                    else None
                ),
                replace_attendees=_a2a_bool(payload.get("replace_attendees"), default=False),
                send_updates=_a2a_bool(payload.get("send_updates"), default=True),
            )
            output = {
                "skill": skill,
                "status": "completed",
                "calendar_event": result,
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-calendar-event-updated",
                artifact_name="email_calendar_event_updated",
                text="Updated calendar event.",
                output=output,
            )
            return

        if skill == "email/calendar/delete_event":
            event_id = str(payload.get("event_id") or "").strip()
            if not event_id:
                raise ValueError("email/calendar/delete_event requires event_id.")
            result = await asyncio.to_thread(
                self.email_agent.delete_calendar_event,
                event_id=event_id,
                send_updates=_a2a_bool(payload.get("send_updates"), default=True),
            )
            output = {
                "skill": skill,
                "status": "completed",
                "calendar_event": result,
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-calendar-event-deleted",
                artifact_name="email_calendar_event_deleted",
                text="Deleted calendar event.",
                output=output,
            )
            return

        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("email/calendar/create_event requires title.")
        if not payload.get("start") or not payload.get("end"):
            raise ValueError("email/calendar/create_event requires start and end datetimes.")
        result = await asyncio.to_thread(
            self.email_agent.create_calendar_event,
            title=title,
            start=parse_datetime(str(payload["start"]), self.email_agent.config.timezone_name),
            end=parse_datetime(str(payload["end"]), self.email_agent.config.timezone_name),
            attendees=_normalize_addresses(payload.get("attendees") or payload.get("to")),
            description=str(payload.get("description") or ""),
            location=str(payload.get("location") or ""),
            send_invite=_a2a_bool(payload.get("send_invite"), default=True),
            use_google_api=_a2a_bool(payload.get("use_google_api"), default=False),
            allow_ics_fallback=_a2a_bool(payload.get("allow_ics_fallback"), default=True),
        )
        output = {
            "skill": skill,
            "status": "completed",
            "calendar_event": result,
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await _a2a_complete(
            updater,
            skill=skill,
            artifact_id="email-calendar-event",
            artifact_name="email_calendar_event",
            text="Created calendar event or invite.",
            output=output,
        )

    async def _docs_action(
        self,
        context: A2ARequestContext,
        updater: A2ATaskUpdater,
        skill: str,
    ) -> None:
        payload = _a2a_payload_from_context(context)
        if skill == "email/docs/list":
            docs = await asyncio.to_thread(
                self.email_agent.list_google_docs,
                query=str(payload.get("query") or ""),
                max_results=_a2a_int(payload.get("max_results"), default=10, minimum=1, maximum=100),
            )
            output = {
                "skill": skill,
                "status": "completed",
                "docs": docs,
                "doc_count": len(docs),
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-docs-list",
                artifact_name="email_docs_list",
                text=f"Found {len(docs)} Google Doc{'s' if len(docs) != 1 else ''}.",
                output=output,
            )
            return

        if skill == "email/docs/read":
            document_id = str(payload.get("document_id") or "").strip()
            if not document_id:
                raise ValueError("email/docs/read requires document_id.")
            doc = await asyncio.to_thread(self.email_agent.read_google_doc, document_id=document_id)
            output = {
                "skill": skill,
                "status": "completed",
                "doc": doc,
                "context_id": context.context_id,
                "task_id": context.task_id,
            }
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-doc-read",
                artifact_name="email_doc_read",
                text=f"Read Google Doc: {doc.get('title', document_id)}.",
                output=output,
            )
            return

        if skill == "email/docs/create":
            result = await asyncio.to_thread(
                self.email_agent.create_google_doc,
                title=_a2a_required_str(payload, "title", skill),
                content=str(payload.get("content") or ""),
            )
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-doc-created",
                artifact_name="email_doc_created",
                text=f"Created Google Doc: {result.get('title', '')}.",
                output={
                    "skill": skill,
                    "status": "completed",
                    "doc": result,
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
            return

        if skill == "email/docs/append":
            result = await asyncio.to_thread(
                self.email_agent.append_google_doc,
                document_id=_a2a_required_str(payload, "document_id", skill),
                text=_a2a_required_str(payload, "text", skill),
            )
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-doc-appended",
                artifact_name="email_doc_appended",
                text="Appended text to Google Doc.",
                output={
                    "skill": skill,
                    "status": "completed",
                    "doc": result,
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
            return

        if skill == "email/docs/replace_text":
            result = await asyncio.to_thread(
                self.email_agent.replace_google_doc_text,
                document_id=_a2a_required_str(payload, "document_id", skill),
                find_text=_a2a_required_str(payload, "find_text", skill),
                replace_text=str(payload.get("replace_text") or ""),
            )
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-doc-text-replaced",
                artifact_name="email_doc_text_replaced",
                text="Replaced text in Google Doc.",
                output={
                    "skill": skill,
                    "status": "completed",
                    "doc": result,
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
            return

        to = _normalize_addresses(payload.get("to") or payload.get("recipients"))
        if not to:
            raise ValueError("email/docs/create_and_email requires at least one recipient in to.")
        doc = await asyncio.to_thread(
            self.email_agent.create_google_doc,
            title=_a2a_required_str(payload, "title", skill),
            content=str(payload.get("content") or ""),
        )
        subject = str(payload.get("subject") or f"Google Doc: {doc.get('title', payload.get('title', 'Document'))}")
        body = str(payload.get("body") or "").strip()
        if not body:
            body = (
                "Hi,\n\n"
                "I created a Google Doc for you:\n"
                f"{doc.get('url', '')}\n\n"
                "Best"
            )
        message_id = await asyncio.to_thread(
            self.email_agent.send_email,
            to=to,
            cc=_normalize_addresses(payload.get("cc")),
            bcc=_normalize_addresses(payload.get("bcc")),
            subject=subject,
            body=body,
            high_importance=_a2a_bool(payload.get("high_importance"), default=False),
        )
        await _a2a_complete(
            updater,
            skill=skill,
            artifact_id="email-doc-created-and-emailed",
            artifact_name="email_doc_created_and_emailed",
            text=f"Created Google Doc and emailed the link to {', '.join(to)}.",
            output={
                "skill": skill,
                "status": "completed",
                "doc": doc,
                "message_id": message_id,
                "sent_email": {"to": ", ".join(to), "subject": subject},
                "context_id": context.context_id,
                "task_id": context.task_id,
            },
        )

    async def _sheets_action(
        self,
        context: A2ARequestContext,
        updater: A2ATaskUpdater,
        skill: str,
    ) -> None:
        payload = _a2a_payload_from_context(context)
        if skill == "email/sheets/list":
            sheets = await asyncio.to_thread(
                self.email_agent.list_google_sheets,
                query=str(payload.get("query") or ""),
                max_results=_a2a_int(payload.get("max_results"), default=10, minimum=1, maximum=100),
            )
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-sheets-list",
                artifact_name="email_sheets_list",
                text=f"Found {len(sheets)} Google Sheet{'s' if len(sheets) != 1 else ''}.",
                output={
                    "skill": skill,
                    "status": "completed",
                    "sheets": sheets,
                    "sheet_count": len(sheets),
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
            return

        if skill == "email/sheets/read":
            result = await asyncio.to_thread(
                self.email_agent.read_google_sheet,
                spreadsheet_id=_a2a_required_str(payload, "spreadsheet_id", skill),
                range_name=str(payload.get("range_name") or payload.get("range") or "A1:Z50"),
            )
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-sheet-read",
                artifact_name="email_sheet_read",
                text=f"Read Google Sheet range {result.get('range', '')}.",
                output={
                    "skill": skill,
                    "status": "completed",
                    "sheet": result,
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
            return

        if skill == "email/sheets/create":
            result = await asyncio.to_thread(
                self.email_agent.create_google_sheet,
                title=_a2a_required_str(payload, "title", skill),
                sheet_name=str(payload.get("sheet_name") or "Sheet1"),
                values=_normalize_sheet_values(payload.get("values")),
            )
            text = f"Created Google Sheet: {result.get('title', '')}."
        elif skill == "email/sheets/write":
            result = await asyncio.to_thread(
                self.email_agent.write_google_sheet,
                spreadsheet_id=_a2a_required_str(payload, "spreadsheet_id", skill),
                range_name=_a2a_required_str({"range_name": payload.get("range_name") or payload.get("range")}, "range_name", skill),
                values=_normalize_sheet_values(payload.get("values")),
            )
            text = f"Wrote {result.get('updated_cells', 0)} cells to Google Sheet."
        else:
            result = await asyncio.to_thread(
                self.email_agent.append_google_sheet,
                spreadsheet_id=_a2a_required_str(payload, "spreadsheet_id", skill),
                range_name=_a2a_required_str({"range_name": payload.get("range_name") or payload.get("range")}, "range_name", skill),
                values=_normalize_sheet_values(payload.get("values")),
            )
            text = f"Appended {result.get('updated_cells', 0)} cells to Google Sheet."

        await _a2a_complete(
            updater,
            skill=skill,
            artifact_id=skill.replace("/", "-"),
            artifact_name=skill.replace("/", "_").replace("-", "_"),
            text=text,
            output={
                "skill": skill,
                "status": "completed",
                "sheet": result,
                "context_id": context.context_id,
                "task_id": context.task_id,
            },
        )


def load_email_a2a_agent_card(
    card_path: Path | str = DEFAULT_EMAIL_A2A_CARD,
) -> A2AAgentCard:
    _require_a2a()
    assert _AgentCard is not None
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return _AgentCard.model_validate(json.load(fh))


def create_email_a2a_app(
    agent_card: A2AAgentCard,
    email_agent: EmailAgent | None = None,
    agent_runner: Any | None = None,
    enable_remote_agent_tools: bool = False,
) -> Any:
    _require_a2a()
    assert _A2AStarletteApplication is not None
    assert _DefaultRequestHandler is not None
    assert _InMemoryTaskStore is not None
    request_handler = _DefaultRequestHandler(
        agent_executor=GmailEmailAgentExecutor(
            email_agent,
            agent_runner=agent_runner,
            enable_remote_agent_tools=enable_remote_agent_tools,
        ),
        task_store=_InMemoryTaskStore(),
    )
    return _A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()


def _with_advertised_url(agent_card: A2AAgentCard, advertised_url: str | None) -> A2AAgentCard:
    if not advertised_url:
        return agent_card
    normalized_url = advertised_url.rstrip("/") + "/"
    return agent_card.model_copy(update={"url": normalized_url})


def _agent_card_json(agent_card: A2AAgentCard) -> dict[str, Any]:
    if hasattr(agent_card, "model_dump"):
        return agent_card.model_dump(mode="json", by_alias=True, exclude_none=True)
    raise TypeError("A2A Agent Card object does not support model_dump().")


def _validate_ads_agent_card(agent_card: A2AAgentCard) -> None:
    if not getattr(agent_card, "name", ""):
        raise ValueError("Agent Card requires name before ADS publication.")
    if not getattr(agent_card, "url", ""):
        raise ValueError("Agent Card requires url before ADS publication.")
    if not (getattr(agent_card, "skills", None) or []):
        raise ValueError("Agent Card requires at least one skill before ADS publication.")


def _agent_card_skill_tags(agent_card: A2AAgentCard) -> list[str]:
    tags: list[str] = []
    for skill in getattr(agent_card, "skills", None) or []:
        skill_id = getattr(skill, "id", "")
        if skill_id:
            tags.append(skill_id)
        tags.extend(getattr(skill, "tags", None) or [])
    return sorted(set(tags))


def _ads_skill_policy(skill: Any) -> dict[str, Any]:
    return {
        "risk_level": "low",
        "action_type": "read",
    }


def _email_agent_card_to_oasf_record(agent_card: A2AAgentCard) -> dict[str, Any]:
    _validate_ads_agent_card(agent_card)
    card_data = _agent_card_json(agent_card)
    provider = getattr(agent_card, "provider", None)
    organization = getattr(provider, "organization", None) if provider else None
    name = getattr(agent_card, "name", "")
    version = getattr(agent_card, "version", "")
    agent_id = name.lower().replace(" ", "-")
    custom_tags = _agent_card_skill_tags(agent_card)
    skill_policies = {
        getattr(skill, "id", ""): _ads_skill_policy(skill)
        for skill in getattr(agent_card, "skills", None) or []
        if getattr(skill, "id", "")
    }
    return {
        "schema_version": "demo-v2",
        "record_type": "agntcy.oasf.demo.agent_record",
        "record_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-directory-service:{name}:{version}")),
        "agent_id": agent_id,
        "name": name,
        "organization": organization or "unknown",
        "description": getattr(agent_card, "description", ""),
        "interaction": {
            "type": "a2a_agent_card",
            "endpoint": getattr(agent_card, "url", ""),
            "preferred_transport": (
                getattr(agent_card, "preferred_transport", None) or "JSONRPC"
            ).lower(),
            "agent_card": card_data,
        },
        "discovery": {
            "official_oasf_skills": [],
            "custom_skill_tags": custom_tags,
            "custom_skill_note": (
                "Custom skill tags are demo-specific when no local official "
                "OASF taxonomy mapping is known."
            ),
            "domains": sorted({tag.split("/")[0] for tag in custom_tags if "/" in tag}),
            "input_modes": getattr(agent_card, "default_input_modes", None) or [],
            "output_modes": getattr(agent_card, "default_output_modes", None) or [],
        },
        "auth": {
            "security": getattr(agent_card, "security", None) or [],
            "security_schemes": getattr(agent_card, "security_schemes", None) or {},
            "required_scopes": [],
        },
        "policy": {
            "risk_level": "low",
            "action_type": "read",
            "skill_policies": skill_policies,
        },
        "metadata": {
            "source": "standalone_email_agent",
            "demo_specific_custom_skills": custom_tags,
        },
    }


def _ads_record_name(provider_url: str, agent_id: str) -> str:
    if provider_url.startswith(("http://", "https://")):
        return provider_url.rstrip("/")
    return f"https://example.invalid/agents/{agent_id}"


def _ads_routing_skills_for_card(agent_card: A2AAgentCard) -> list[dict[str, Any]]:
    agent_id = getattr(agent_card, "name", "").lower().replace(" ", "-")
    if agent_id == "email-agent":
        return [dict(skill) for skill in EMAIL_ADS_ROUTING_SKILLS]
    return []


def _email_agent_card_to_ads_record(agent_card: A2AAgentCard) -> dict[str, Any]:
    base_record = _email_agent_card_to_oasf_record(agent_card)
    card_data = base_record["interaction"]["agent_card"]
    provider = getattr(agent_card, "provider", None)
    provider_url = getattr(provider, "url", "") if provider else ""
    agent_id = base_record["agent_id"]
    custom_tags = base_record["discovery"]["custom_skill_tags"]
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "schema_version": "1.0.0",
        "name": _ads_record_name(provider_url, agent_id),
        "version": getattr(agent_card, "version", ""),
        "authors": [base_record.get("organization") or "AgenticNetwork"],
        "description": getattr(agent_card, "description", ""),
        "created_at": created_at,
        "skills": _ads_routing_skills_for_card(agent_card),
        "domains": [],
        "locators": [
            {
                "type": "source_code",
                "urls": [provider_url or getattr(agent_card, "url", "")],
                "annotations": {
                    "a2a.url": getattr(agent_card, "url", ""),
                },
            }
        ],
        "modules": [
            {
                "name": "integration/a2a",
                "data": {
                    "card_schema_version": getattr(agent_card, "protocol_version", None)
                    or "0.3.0",
                    "card_data": card_data,
                },
            }
        ],
        "annotations": {
            "a2a.url": getattr(agent_card, "url", ""),
            "agentic_network.agent_id": agent_id,
            "agentic_network.record_id": base_record["record_id"],
            "agentic_network.display_name": getattr(agent_card, "name", ""),
            "agentic_network.organization": base_record.get("organization", "unknown"),
            "agentic_network.custom_skill_tags": ",".join(custom_tags),
        },
    }


def _run_ads_dirctl_command(
    args: list[str],
    *,
    ads_url: str | None,
) -> subprocess.CompletedProcess[str]:
    server_address = (
        ads_url
        or os.getenv("ADS_SERVER_ADDR")
        or os.getenv("ADS_URL")
        or os.getenv("ADS_SERVER_ADDRESS")
        or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
        or DEFAULT_ADS_SERVER_ADDR
    )
    if not server_address:
        raise RuntimeError("Real ADS is not configured. Set ADS_SERVER_ADDR or --ads-url.")

    dirctl_path = os.getenv("DIRCTL_BIN", "dirctl")
    command = [dirctl_path, "--server-addr", server_address, *args]
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "dirctl is not installed or not on PATH. Install the AGNTCY dirctl "
            "binary, or set DIRCTL_BIN to its location."
        ) from exc
    if completed.returncode != 0:
        output = completed.stderr.strip() or completed.stdout.strip()
        detail = f"\n{output}" if output else ""
        raise RuntimeError(
            f"dirctl command failed with exit code {completed.returncode}: "
            f"{' '.join(command)}{detail}"
        )
    return completed


def _try_ads_dirctl_command(
    args: list[str],
    *,
    ads_url: str | None,
    errors: list[str],
) -> subprocess.CompletedProcess[str] | None:
    try:
        return _run_ads_dirctl_command(args, ads_url=ads_url)
    except RuntimeError as exc:
        errors.append(str(exc))
        _email_status("ADS command failed", command=" ".join(args), error=_preview_text(exc))
        return None


def _cids_from_routing_output(raw: str) -> list[str]:
    if not raw.strip():
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("items", "records", "results", "record_refs", "recordRefs", "refs"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    items = data if isinstance(data, list) else [data]
    cids: list[str] = []
    for item in items:
        cid = _cid_from_ads_item(item)
        if cid:
            cids.append(cid)
    return _dedupe_strings(cids)


def _cid_from_ads_item(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return None
    for key in ("cid", "id"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    record_ref = item.get("record_ref") or item.get("recordRef") or item.get("ref")
    if isinstance(record_ref, dict):
        value = record_ref.get("cid") or record_ref.get("id")
        return value if isinstance(value, str) else None
    record = item.get("record")
    if isinstance(record, dict):
        value = record.get("cid") or record.get("id")
        return value if isinstance(value, str) else None
    return None


def _record_from_ads_pull_output(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise json.JSONDecodeError("dirctl pull returned non-object JSON", raw, 0)
    for key in ("record", "data"):
        value = data.get(key)
        if isinstance(value, dict):
            if "data" in value and isinstance(value["data"], dict):
                return value["data"]
            return value
    return data


def _card_data_from_ads_record(record: dict[str, Any]) -> dict[str, Any] | None:
    interaction = record.get("interaction")
    if isinstance(interaction, dict) and isinstance(interaction.get("agent_card"), dict):
        return interaction["agent_card"]
    for module in record.get("modules", []) or []:
        if not isinstance(module, dict):
            continue
        data = module.get("data")
        if isinstance(data, dict) and isinstance(data.get("card_data"), dict):
            return data["card_data"]
    return None


def _a2a_url_from_ads_record(
    record: dict[str, Any],
    card_data: dict[str, Any] | None,
) -> str:
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    url = annotations.get("a2a.url")
    if isinstance(url, str) and url:
        return url

    interaction = record.get("interaction")
    if isinstance(interaction, dict) and isinstance(interaction.get("endpoint"), str):
        return interaction["endpoint"]

    for locator in record.get("locators", []) or []:
        if not isinstance(locator, dict):
            continue
        locator_annotations = locator.get("annotations")
        if isinstance(locator_annotations, dict):
            url = locator_annotations.get("a2a.url")
            if isinstance(url, str) and url:
                return url

    if card_data and isinstance(card_data.get("url"), str):
        return card_data["url"]
    return ""


def _ads_record_to_agent_summary(
    record: dict[str, Any],
    *,
    cid: str | None = None,
) -> dict[str, Any]:
    card_data = _card_data_from_ads_record(record) or {}
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    endpoint = _a2a_url_from_ads_record(record, card_data)
    name = str(
        card_data.get("name")
        or annotations.get("agentic_network.display_name")
        or record.get("name")
        or "Unknown Agent"
    )
    agent_id = str(
        annotations.get("agentic_network.agent_id")
        or record.get("agent_id")
        or name.lower().replace(" ", "-")
    )
    record_id = str(
        annotations.get("agentic_network.record_id")
        or record.get("record_id")
        or cid
        or agent_id
    )
    organization = str(
        annotations.get("agentic_network.organization")
        or record.get("organization")
        or (
            card_data.get("provider", {}).get("organization")
            if isinstance(card_data.get("provider"), dict)
            else ""
        )
        or "unknown"
    )
    skills = _skill_summaries_from_card_data(card_data)
    custom_tags = _custom_skill_tags_from_ads_record(record, skills)

    return {
        "agent_id": agent_id,
        "record_id": record_id,
        "cid": cid or "",
        "name": name,
        "organization": organization,
        "description": str(card_data.get("description") or record.get("description") or ""),
        "endpoint": endpoint,
        "source_card_url": f"{endpoint.rstrip('/')}/.well-known/agent-card.json" if endpoint else "",
        "skills": skills,
        "custom_skill_tags": custom_tags,
        "official_routing_skills": [
            item
            for item in record.get("skills", []) or []
            if isinstance(item, dict)
        ],
    }


def _skill_summaries_from_card_data(card_data: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for skill in card_data.get("skills", []) or []:
        if not isinstance(skill, dict):
            continue
        summaries.append(
            {
                "id": str(skill.get("id") or ""),
                "name": str(skill.get("name") or ""),
                "description": str(skill.get("description") or ""),
                "tags": [str(tag) for tag in skill.get("tags", []) or []],
                "examples": [str(example) for example in skill.get("examples", []) or []],
            }
        )
    return summaries


def _custom_skill_tags_from_ads_record(
    record: dict[str, Any],
    skills: list[dict[str, Any]],
) -> list[str]:
    tags: list[str] = []
    annotations = record.get("annotations") if isinstance(record.get("annotations"), dict) else {}
    annotated_tags = annotations.get("agentic_network.custom_skill_tags")
    if isinstance(annotated_tags, str):
        tags.extend(tag.strip() for tag in annotated_tags.split(",") if tag.strip())
    discovery = record.get("discovery")
    if isinstance(discovery, dict):
        tags.extend(str(tag) for tag in discovery.get("custom_skill_tags", []) or [])
    for skill in skills:
        if skill.get("id"):
            tags.append(str(skill["id"]))
        tags.extend(str(tag) for tag in skill.get("tags", []) or [])
    return sorted(set(tags))


def _dedupe_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _ads_skill_hints(skill: str) -> list[str]:
    if not skill or not skill.startswith("email/"):
        return []
    return [str(item["name"]) for item in EMAIL_ADS_ROUTING_SKILLS]


def _looks_like_ads_cid(value: str) -> bool:
    return value.startswith(("bafy", "bae", "bag", "zd", "Qm"))


def _pull_ads_record(cid: str, *, ads_url: str | None = None) -> dict[str, Any]:
    result = _run_ads_dirctl_command(["pull", cid, "--output", "json"], ads_url=ads_url)
    try:
        return _record_from_ads_pull_output(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"dirctl pull {cid} did not return a valid ADS record.") from exc


def _ads_search_cids(
    *,
    skill: str,
    limit: int,
    ads_url: str | None = None,
) -> list[str]:
    errors: list[str] = []
    cids: list[str] = []
    completed_any = False
    search_terms = _dedupe_strings([skill, *_ads_skill_hints(skill)])

    def collect(args: list[str]) -> None:
        nonlocal completed_any
        completed = _try_ads_dirctl_command(args, ads_url=ads_url, errors=errors)
        if completed is None:
            return
        completed_any = True
        try:
            cids.extend(_cids_from_routing_output(completed.stdout))
        except json.JSONDecodeError as exc:
            errors.append(f"Could not parse ADS output from {' '.join(args)}: {exc}")

    if search_terms:
        for search_skill in search_terms:
            collect(["routing", "search", "--skill", search_skill, "--limit", str(limit), "--output", "json"])
            if not cids:
                collect(["routing", "list", "--skill", search_skill, "--output", "json"])
            if not cids:
                collect(["search", "--skill", search_skill, "--limit", str(limit), "--output", "json"])
    else:
        collect(["routing", "search", "--limit", str(limit), "--output", "json"])
        if not cids:
            collect(["routing", "list", "--output", "json"])
        if not cids:
            collect(["search", "--limit", str(limit), "--output", "json"])

    if search_terms and not cids:
        collect(["routing", "search", "--limit", str(limit), "--output", "json"])
        if not cids:
            collect(["routing", "list", "--output", "json"])

    if not completed_any and errors:
        raise RuntimeError(f"ADS discovery failed: {errors[0]}")
    return _dedupe_strings(cids)[:limit]


def _agent_summary_matches(
    summary: dict[str, Any],
    *,
    skill: str,
    query: str,
) -> bool:
    haystack_parts = [
        summary.get("agent_id", ""),
        summary.get("record_id", ""),
        summary.get("name", ""),
        summary.get("organization", ""),
        summary.get("description", ""),
        summary.get("endpoint", ""),
        " ".join(summary.get("custom_skill_tags", []) or []),
    ]
    for skill_item in summary.get("skills", []) or []:
        if isinstance(skill_item, dict):
            haystack_parts.extend(
                [
                    skill_item.get("id", ""),
                    skill_item.get("name", ""),
                    skill_item.get("description", ""),
                    " ".join(skill_item.get("tags", []) or []),
                    " ".join(skill_item.get("examples", []) or []),
                ]
            )
    for official in summary.get("official_routing_skills", []) or []:
        if isinstance(official, dict):
            haystack_parts.extend([str(official.get("name", "")), str(official.get("id", ""))])
    haystack = " ".join(str(part).lower() for part in haystack_parts)
    if skill and skill.lower() not in haystack:
        return False
    if query and query.lower() not in haystack:
        return False
    return True


def _current_agent_urls() -> set[str]:
    urls = set()
    env_url = os.getenv("EMAIL_AGENT_A2A_URL")
    if env_url:
        urls.add(env_url.rstrip("/"))
    try:
        if DEFAULT_EMAIL_A2A_CARD.exists():
            card = load_email_a2a_agent_card(DEFAULT_EMAIL_A2A_CARD)
            url = getattr(card, "url", "")
            if url:
                urls.add(str(url).rstrip("/"))
    except Exception:
        pass
    return urls


def _is_current_agent_summary(summary: dict[str, Any]) -> bool:
    endpoint = str(summary.get("endpoint") or "").rstrip("/")
    return bool(endpoint and endpoint in _current_agent_urls())


def discover_ads_agents(
    *,
    skill: str = "",
    query: str = "",
    limit: int = 10,
    include_self: bool = False,
    ads_url: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 50))
    skill = skill.strip()
    query = query.strip()
    _email_status(
        "Discovering agents from ADS",
        skill=skill or "any",
        query=query,
        limit=limit,
    )
    cids = _ads_search_cids(skill=skill, limit=max(limit * 3, limit), ads_url=ads_url)
    summaries: list[dict[str, Any]] = []
    for cid in cids:
        try:
            summary = _ads_record_to_agent_summary(_pull_ads_record(cid, ads_url=ads_url), cid=cid)
        except Exception as exc:
            _email_status("Skipping ADS record that could not be parsed", cid=cid, error=_preview_text(exc))
            continue
        if not include_self and _is_current_agent_summary(summary):
            continue
        if not _agent_summary_matches(summary, skill=skill, query=query):
            continue
        summaries.append(summary)

    deduped: dict[str, dict[str, Any]] = {}
    for summary in summaries:
        key = str(summary.get("cid") or summary.get("record_id") or summary.get("endpoint"))
        if key:
            deduped[key] = summary
    result = list(deduped.values())[:limit]
    _email_status("ADS discovery completed", count=len(result))
    return result


def _resolve_ads_agent(
    agent_ref: str,
    *,
    ads_url: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    agent_ref = agent_ref.strip()
    if not agent_ref:
        raise ValueError("agent_ref is required.")

    if _looks_like_ads_cid(agent_ref):
        record = _pull_ads_record(agent_ref, ads_url=ads_url)
        summary = _ads_record_to_agent_summary(record, cid=agent_ref)
        card_data = _card_data_from_ads_record(record)
        if card_data is None:
            raise RuntimeError(f"ADS record {agent_ref} does not contain an embedded A2A Agent Card.")
        return record, summary, card_data

    candidates = discover_ads_agents(limit=50, include_self=True, ads_url=ads_url)
    normalized_ref = agent_ref.rstrip("/").lower()
    for summary in candidates:
        aliases = {
            str(summary.get("cid") or "").lower(),
            str(summary.get("record_id") or "").lower(),
            str(summary.get("agent_id") or "").lower(),
            str(summary.get("name") or "").lower(),
            str(summary.get("endpoint") or "").rstrip("/").lower(),
            str(summary.get("source_card_url") or "").rstrip("/").lower(),
        }
        if normalized_ref in aliases:
            cid = str(summary.get("cid") or "")
            if not cid:
                raise RuntimeError(f"Resolved {agent_ref!r}, but the ADS result did not include a CID.")
            record = _pull_ads_record(cid, ads_url=ads_url)
            card_data = _card_data_from_ads_record(record)
            if card_data is None:
                raise RuntimeError(f"ADS record for {agent_ref!r} does not contain an embedded A2A Agent Card.")
            return record, _ads_record_to_agent_summary(record, cid=cid), card_data

    raise KeyError(f"Agent not found in ADS: {agent_ref}")


async def send_a2a_request_to_ads_agent(
    *,
    agent_ref: str,
    skill: str = "",
    message: str = "",
    data: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_REMOTE_A2A_TIMEOUT_SECONDS,
    ads_url: str | None = None,
    confirmed: bool = True,
) -> dict[str, Any]:
    skill_hint = skill.strip() or None
    if not message.strip():
        raise ValueError("message is required.")
    _, summary, card_data = _resolve_ads_agent(agent_ref, ads_url=ads_url)
    if _is_current_agent_summary(summary):
        raise RuntimeError("Refusing to send a remote A2A request to this same email agent.")

    try:
        import httpx
        from a2a.client import ClientConfig as SdkClientConfig
        from a2a.client import ClientFactory as SdkClientFactory
        from a2a.types import (
            AgentCard,
            DataPart,
            Message,
            Part,
            Role,
            TextPart,
            TransportProtocol,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Remote A2A requests require the a2a-sdk and httpx packages."
        ) from exc

    card = AgentCard.model_validate(card_data)
    context_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    part_metadata = {"skill": skill_hint, "requested_skill": skill_hint, "skill_hint": skill_hint} if skill_hint else {}
    parts = [
        Part(root=TextPart(text=message, metadata=part_metadata or None)),
    ]
    if data:
        parts.append(Part(root=DataPart(data=data, metadata=part_metadata or None)))
    preauthorized_metadata = (
        {"confirmed": True}
        if confirmed
        else {}
    )
    message_metadata = {
        "target_agent_id": summary.get("agent_id"),
        "requested_task_id": task_id,
        "user_goal": message,
        **preauthorized_metadata,
    }
    if skill_hint:
        message_metadata.update(
            {"skill": skill_hint, "requested_skill": skill_hint, "skill_hint": skill_hint}
        )

    sdk_message = Message(
        role=Role.user,
        message_id=str(uuid.uuid4()),
        context_id=context_id,
        task_id=None,
        parts=parts,
        metadata=message_metadata,
    )

    _email_status(
        "Sending remote A2A request",
        agent=summary.get("agent_id"),
        skill_hint=skill_hint,
        endpoint=summary.get("endpoint"),
    )
    async with httpx.AsyncClient(timeout=timeout_seconds) as http_client:
        client_config = SdkClientConfig(
            streaming=True,
            polling=False,
            httpx_client=http_client,
            supported_transports=[TransportProtocol.jsonrpc],
            use_client_preference=True,
            accepted_output_modes=list(getattr(card, "default_output_modes", None) or []),
        )
        sdk_client = SdkClientFactory(client_config).create(card)
        response = None
        async for event in sdk_client.send_message(sdk_message):
            _email_status(
                "Remote A2A stream status",
                agent=summary.get("agent_id"),
                skill_hint=skill_hint,
                **_a2a_stream_event_status(event),
            )
            response = event
        if response is None:
            raise RuntimeError("A2A request completed without a response event.")
    result = _summarize_a2a_response(response)
    result["agent"] = {
        key: summary.get(key)
        for key in ("agent_id", "record_id", "cid", "name", "organization", "endpoint")
    }
    result["skill"] = skill_hint
    result["requested_skill"] = skill_hint
    result["skill_hint"] = skill_hint
    result["confirmed"] = confirmed
    for action in result.get("pending_actions", []) or []:
        if isinstance(action, dict):
            action.setdefault("agent", result["agent"])
            if skill_hint:
                action.setdefault("skill", skill_hint)
    _email_status(
        "Remote A2A request completed",
        agent=summary.get("agent_id"),
        state=result.get("state"),
    )
    return result


def _summarize_a2a_response(response: Any) -> dict[str, Any]:
    if isinstance(response, tuple) and response:
        response = response[0]
    root = getattr(response, "root", response)
    error = getattr(root, "error", None)
    if error is not None:
        code = getattr(error, "code", "")
        error_message = getattr(error, "message", str(error))
        raise RuntimeError(f"A2A error {code}: {error_message}")

    result = getattr(root, "result", root)
    status = getattr(result, "status", None)
    state = _a2a_value(getattr(status, "state", None)) or (
        "completed" if hasattr(result, "parts") else "unknown"
    )

    messages: list[dict[str, Any]] = []
    if hasattr(result, "parts"):
        messages.append(_summarize_a2a_message(result))
    for message in getattr(result, "history", None) or []:
        messages.append(_summarize_a2a_message(message))
    status_message = getattr(status, "message", None)
    if status_message is not None:
        messages.append(_summarize_a2a_message(status_message))

    artifacts = [
        _summarize_a2a_artifact(artifact)
        for artifact in getattr(result, "artifacts", None) or []
    ]
    structured_output = _structured_output_from_a2a_result(result)
    text_parts = [
        part.get("text", "")
        for message_item in messages
        for part in message_item.get("parts", [])
        if part.get("kind") == "text" and part.get("text")
    ]
    summary = {
        "state": state,
        "task_id": getattr(result, "id", None) or getattr(result, "task_id", None),
        "context_id": getattr(result, "context_id", None),
        "text": "\n".join(text_parts),
        "messages": messages,
        "artifacts": artifacts,
        "structured_output": structured_output,
    }
    pending_actions = _a2a_pending_actions_from_summary(summary)
    summary["requires_follow_up"] = bool(pending_actions)
    summary["pending_actions"] = pending_actions
    return summary


def _a2a_stream_event_status(event: Any) -> dict[str, Any]:
    task = event
    update = None
    if isinstance(event, tuple) and event:
        task = event[0]
        update = event[1] if len(event) > 1 else None

    status = getattr(update, "status", None) or getattr(task, "status", None)
    state = _a2a_value(getattr(status, "state", None))
    event_kind = _a2a_value(getattr(update, "kind", None) or getattr(task, "kind", None))
    task_id = (
        getattr(update, "task_id", None)
        or getattr(update, "taskId", None)
        or getattr(task, "id", None)
        or getattr(task, "task_id", None)
        or getattr(task, "taskId", None)
    )
    context_id = (
        getattr(update, "context_id", None)
        or getattr(update, "contextId", None)
        or getattr(task, "context_id", None)
        or getattr(task, "contextId", None)
    )
    final = getattr(update, "final", None)
    if final is None:
        final = state in {"completed", "failed", "canceled", "rejected", "input-required", "auth-required"}
    return {
        "task_id": task_id,
        "context_id": context_id,
        "state": state or None,
        "final": bool(final),
        "event_kind": event_kind or None,
    }


def _summarize_a2a_message(message: Any) -> dict[str, Any]:
    return {
        "message_id": getattr(message, "message_id", ""),
        "role": _a2a_value(getattr(message, "role", "")),
        "parts": [_summarize_a2a_part(part) for part in getattr(message, "parts", []) or []],
        "metadata": getattr(message, "metadata", None) or {},
    }


def _summarize_a2a_artifact(artifact: Any) -> dict[str, Any]:
    return {
        "artifact_id": getattr(artifact, "artifact_id", ""),
        "name": getattr(artifact, "name", "artifact"),
        "parts": [_summarize_a2a_part(part) for part in getattr(artifact, "parts", []) or []],
        "metadata": getattr(artifact, "metadata", None) or {},
    }


def _summarize_a2a_part(part: Any) -> dict[str, Any]:
    root = getattr(part, "root", part)
    metadata = getattr(root, "metadata", None) or getattr(part, "metadata", None) or {}
    if hasattr(root, "text"):
        return {
            "kind": "text",
            "content_type": "text/plain",
            "text": str(getattr(root, "text", "")),
            "metadata": metadata,
        }
    if hasattr(root, "data"):
        return {
            "kind": "data",
            "content_type": "application/json",
            "data": getattr(root, "data", None) or {},
            "metadata": metadata,
        }
    file_obj = getattr(root, "file", None)
    if file_obj is not None:
        return {
            "kind": "file",
            "content_type": getattr(file_obj, "mime_type", None)
            or getattr(file_obj, "mimeType", None)
            or "application/octet-stream",
            "uri": getattr(file_obj, "uri", ""),
            "name": getattr(file_obj, "name", ""),
            "metadata": metadata,
        }
    return {
        "kind": type(root).__name__,
        "content_type": "application/octet-stream",
        "metadata": metadata,
    }


def _structured_output_from_a2a_result(result: Any) -> dict[str, Any] | None:
    for artifact in getattr(result, "artifacts", None) or []:
        for part in getattr(artifact, "parts", []) or []:
            root = getattr(part, "root", part)
            data = getattr(root, "data", None)
            if isinstance(data, dict):
                return data
    status = getattr(result, "status", None)
    message = getattr(status, "message", None)
    metadata = getattr(message, "metadata", None) if message is not None else None
    if isinstance(metadata, dict) and isinstance(metadata.get("structured_output"), dict):
        return metadata["structured_output"]
    return None


def _a2a_pending_actions_from_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return []


def _a2a_summary_payloads(summary: dict[str, Any]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    structured_output = summary.get("structured_output")
    if isinstance(structured_output, dict):
        payloads.append(structured_output)

    for collection_name in ("artifacts", "messages"):
        for item in summary.get(collection_name, []) or []:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            if isinstance(metadata, dict) and isinstance(metadata.get("structured_output"), dict):
                payloads.append(metadata["structured_output"])
            for part in item.get("parts", []) or []:
                if isinstance(part, dict) and isinstance(part.get("data"), dict):
                    payloads.append(part["data"])
    return payloads


def _a2a_state_needs_follow_up(state: str) -> bool:
    normalized = state.strip().lower().replace("_", "-").replace(" ", "-")
    return normalized in {"input-required", "auth-required", "working", "submitted"}


def _pending_actions_from_tool_result(
    result: dict[str, Any],
    *,
    tool_name: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for action in result.get("pending_actions", []) or []:
        if isinstance(action, dict):
            item = dict(action)
            item.setdefault("source_tool", tool_name)
            actions.append(item)
    single = result.get("pending_action")
    if isinstance(single, dict):
        item = dict(single)
        item.setdefault("source_tool", tool_name)
        actions.append(item)
    return actions


def _pending_action_follow_up_lines(actions: list[dict[str, Any]]) -> list[str]:
    return [_format_pending_action_line(action) for action in actions if isinstance(action, dict)]


def _append_pending_actions_to_text(text: str, actions: list[dict[str, Any]]) -> str:
    lines = _pending_action_follow_up_lines(actions)
    if not lines:
        return text
    block = "Pending action:\n" + "\n".join(f"- {line}" for line in lines)
    if block in text:
        return text
    return f"{text.rstrip()}\n\n{block}" if text.strip() else block


def _format_pending_action_line(action: dict[str, Any]) -> str:
    agent = action.get("agent") if isinstance(action.get("agent"), dict) else {}
    agent_name = str(agent.get("name") or agent.get("agent_id") or "Remote agent")
    skill = str(action.get("skill") or "").strip()
    raw_action_type = str(action.get("type") or "follow_up_required")
    action_type = raw_action_type.replace("_", " ")
    reason = str(action.get("reason") or action.get("text") or "").strip()
    next_step = str(action.get("next_step") or "").strip()
    prefix = f"{agent_name} is waiting for {action_type}"
    if skill:
        prefix += f" for {skill}"
    pieces = [prefix]
    if reason:
        pieces.append(reason)
    if next_step:
        pieces.append(next_step)
    return ". ".join(piece.rstrip(".") for piece in pieces if piece) + "."


def _a2a_value(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value))


def publish_email_a2a_agent_card_to_ads(
    agent_card: A2AAgentCard,
    *,
    ads_url: str | None = None,
    ads_backend: str | None = DEFAULT_ADS_BACKEND,
) -> EmailAdsPublishedRecord:
    from ads_utils.publisher import DirectoryPublisher, DirectoryUnavailable

    _email_status(
        "Publishing Email Agent card to ADS",
        ads_url=ads_url or "env",
        agent_url=agent_card.url,
    )
    try:
        result = DirectoryPublisher(server_addr=ads_url).publish(agent_card)
    except DirectoryUnavailable as exc:
        raise RuntimeError(str(exc)) from exc
    published = EmailAdsPublishedRecord(
        record_id=result.record_id,
        agent_id=result.agent_id,
        backend="real",
        backend_ref=result.backend_ref,
    )
    _email_status("Email Agent card published to ADS", backend=published.backend, record=result.backend_ref)
    return published


def unpublish_email_a2a_agent_card_from_ads(
    published: EmailAdsPublishedRecord | str | None,
    *,
    ads_url: str | None = None,
) -> bool:
    if published is None:
        return False
    cid = published if isinstance(published, str) else published.backend_ref
    if not cid:
        _email_status("Skipping ADS unpublish", reason="missing published CID")
        return False

    from ads_utils.publisher import DirectoryPublisher

    _email_status("Unpublishing Email Agent from ADS", cid=cid)
    try:
        DirectoryPublisher(server_addr=ads_url).unpublish(cid)
    except Exception as exc:
        _email_status("Failed to unpublish Email Agent from ADS", cid=cid, error=_preview_text(exc))
        return False

    _email_status("Email Agent unpublished from ADS", cid=cid)
    return True


def _require_a2a() -> None:
    if A2A_IMPORT_ERROR is not None:
        raise RuntimeError(
            "A2A server mode requires the a2a-sdk, starlette, and uvicorn packages. "
            "Install the email agent environment dependencies before running serve-a2a."
        ) from A2A_IMPORT_ERROR


def _task_updater(
    event_queue: A2AEventQueue,
    task_id: str,
    context_id: str,
) -> A2ATaskUpdater:
    _require_a2a()
    assert _TaskUpdater is not None
    return _TaskUpdater(event_queue, task_id, context_id)


def _heartbeat_interval_seconds(env_var: str, default: float = 20.0) -> float:
    raw = os.getenv(env_var)
    if raw is None:
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default


async def _start_a2a_working_heartbeat(
    updater: A2ATaskUpdater,
    *,
    agent_name: str,
    skill: str,
    request_id: str,
    env_var: str,
) -> asyncio.Task[None]:
    interval = _heartbeat_interval_seconds(env_var)
    started = time.monotonic()

    async def emit() -> None:
        elapsed_s = round(time.monotonic() - started, 1)
        await _a2a_working_update(
            updater,
            text=f"{agent_name} is working on {skill}.",
            metadata={
                "progress_event": {
                    "kind": "heartbeat",
                    "agent": agent_name,
                    "skill": skill,
                    "request_id": request_id,
                    "elapsed_s": elapsed_s,
                }
            },
        )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(interval)
            await emit()

    await emit()
    return asyncio.create_task(heartbeat())


async def _stop_a2a_working_heartbeat(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _a2a_working_update(
    updater: A2ATaskUpdater,
    *,
    text: str,
    metadata: dict[str, Any],
) -> None:
    from a2a.types import TaskState

    message = updater.new_agent_message(
        [_text_part(text)],
        metadata=metadata,
    )
    await updater.update_status(TaskState.working, message=message, final=False)


def _data_part(data: dict[str, Any]) -> Any:
    _require_a2a()
    assert _DataPart is not None
    assert _Part is not None
    return _Part(root=_DataPart(data=data))


def _text_part(text: str) -> Any:
    _require_a2a()
    assert _Part is not None
    assert _TextPart is not None
    return _Part(root=_TextPart(text=text))


def _is_data_part(value: Any) -> bool:
    return _DataPart is not None and isinstance(value, _DataPart)


def _a2a_skill_from_context(context: A2ARequestContext) -> str | None:
    for metadata in _a2a_metadata_sources(context):
        skill = metadata.get("skill") or metadata.get("requested_skill") or metadata.get("skill_hint")
        if skill:
            return str(skill)
    return None


def _a2a_agent_request(
    *,
    user_text: str,
    payload: dict[str, Any],
    skill_hint: str | None,
) -> str:
    clean_payload = {
        key: value
        for key, value in payload.items()
        if key not in {"skill", "requested_skill", "skill_hint", "target_agent_id", "user_goal"}
    }
    return (
        "Handle this A2A request autonomously. The caller's natural-language "
        "request and structured payload are authoritative. The skill hint, if "
        "present, is advisory only; ignore it when it conflicts with the user's "
        "request.\n\n"
        f"Skill hint: {skill_hint or '(none)'}\n"
        f"User request:\n{user_text or '(no text supplied)'}\n\n"
        "Structured payload:\n"
        f"{json.dumps(clean_payload, indent=2, sort_keys=True, default=str)}"
    )


_EMAIL_TOOL_TO_SKILL = {
    "read_email": "email/read",
    "write_email_draft": "email/draft",
    "send_email": "email/send",
    "send_group_email": "email/group_send",
    "list_visible_files": "email/files/list",
    "read_visible_file": "email/files/read",
    "write_visible_file": "email/files/write",
    "google_workspace_status": "email/google/status",
    "list_google_docs": "email/docs/list",
    "read_google_doc": "email/docs/read",
    "create_google_doc": "email/docs/create",
    "append_google_doc": "email/docs/append",
    "replace_google_doc_text": "email/docs/replace_text",
    "list_google_sheets": "email/sheets/list",
    "read_google_sheet": "email/sheets/read",
    "create_google_sheet": "email/sheets/create",
    "write_google_sheet": "email/sheets/write",
    "append_google_sheet": "email/sheets/append",
    "calendar_status": "email/calendar/status",
    "list_calendar_events": "email/calendar/list_events",
    "create_calendar_event": "email/calendar/create_event",
    "update_calendar_event": "email/calendar/update_event",
    "delete_calendar_event": "email/calendar/delete_event",
    "current_time": "email/current_time",
    "discover_ads_agents": "email/ads_discovery",
    "send_a2a_request": "email/a2a_request",
}


def _chosen_email_skill_from_trace(tool_calls: list[dict[str, Any]]) -> str | None:
    for call in reversed(tool_calls):
        name = str(call.get("name") or "")
        if name in _EMAIL_TOOL_TO_SKILL:
            return _EMAIL_TOOL_TO_SKILL[name]
    return None


def _compact_email_agent_trace(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for call in tool_calls:
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        compact.append(
            {
                "name": call.get("name"),
                "ok": call.get("ok"),
                "args_keys": sorted((call.get("arguments") or {}).keys())
                if isinstance(call.get("arguments"), dict)
                else [],
                "result_keys": sorted(result.keys()) if isinstance(result, dict) else [],
                "chosen_skill": _EMAIL_TOOL_TO_SKILL.get(str(call.get("name") or "")),
            }
        )
    return compact


async def _run_email_agent_for_a2a(
    runner: Any | None,
    email_agent: EmailAgent,
    agent_request: str,
    *,
    enable_remote_agent_tools: bool = False,
) -> EmailAgentRunResult:
    if runner is None:
        tool_agent = ToolCallingEmailAgent(
            email_agent,
            enable_remote_agent_tools=enable_remote_agent_tools,
        )
        return await asyncio.to_thread(tool_agent.run_with_trace, agent_request)
    result = runner(agent_request)
    if hasattr(result, "__await__"):
        result = await result
    if isinstance(result, EmailAgentRunResult):
        return result
    if isinstance(result, dict):
        return EmailAgentRunResult(
            text=str(result.get("text") or result.get("response_text") or ""),
            tool_calls=list(result.get("tool_calls") or []),
        )
    return EmailAgentRunResult(text=str(result), tool_calls=[])


def _a2a_user_text(context: A2ARequestContext) -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


def _a2a_payload_from_context(context: A2ARequestContext) -> dict[str, Any]:
    payload: dict[str, Any] = dict(context.metadata or {})
    if context.message:
        message_metadata = getattr(context.message, "metadata", None)
        if isinstance(message_metadata, dict):
            payload.update(message_metadata)
        for part in context.message.parts:
            root = part.root
            part_metadata = getattr(root, "metadata", None) or getattr(part, "metadata", None)
            if isinstance(part_metadata, dict):
                payload.update(part_metadata)
            if _is_data_part(root) and isinstance(root.data, dict):
                payload.update(root.data)
    return payload


def _a2a_metadata_sources(context: A2ARequestContext) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    if isinstance(context.metadata, dict):
        sources.append(context.metadata)
    if context.message:
        message_metadata = getattr(context.message, "metadata", None)
        if isinstance(message_metadata, dict):
            sources.append(message_metadata)
        for part in context.message.parts:
            root = part.root
            part_metadata = getattr(root, "metadata", None) or getattr(part, "metadata", None)
            if isinstance(part_metadata, dict):
                sources.append(part_metadata)
    return sources


def _a2a_available_slots(payload: dict[str, Any]) -> list[dict[str, str]]:
    slots = payload.get("available_slots")
    if isinstance(slots, list):
        return [slot for slot in slots if isinstance(slot, dict)]

    previous_outputs = payload.get("previous_outputs")
    if isinstance(previous_outputs, dict):
        for output in previous_outputs.values():
            if isinstance(output, dict) and isinstance(output.get("available_slots"), list):
                return [slot for slot in output["available_slots"] if isinstance(slot, dict)]
    return []


def _a2a_draft_from_payload(
    *,
    payload: dict[str, Any],
    user_text: str,
    allow_demo_defaults: bool,
) -> dict[str, Any]:
    existing = payload.get("draft_email")
    if not isinstance(existing, dict):
        previous_outputs = payload.get("previous_outputs")
        if isinstance(previous_outputs, dict):
            for output in previous_outputs.values():
                if isinstance(output, dict) and isinstance(output.get("draft_email"), dict):
                    existing = output["draft_email"]
                    break
    if isinstance(existing, dict):
        return _a2a_normalize_draft(existing)

    slots = _a2a_available_slots(payload)
    recipient_name = (
        str(payload.get("recipient_name") or "").strip()
        or _a2a_recipient_name_from_text(user_text)
        or "there"
    )
    to = _normalize_addresses(
        payload.get("to")
        or payload.get("recipient")
        or payload.get("recipients")
        or payload.get("recipient_email")
    )
    recipient_source = _normalize_recipient_source(
        str(
            payload.get("recipient_source")
            or payload.get("recipient_group")
            or payload.get("group")
            or ""
        )
    )
    if not to and recipient_source == "notice_list":
        to = load_recipients_from_file(_notice_list_path(payload.get("recipient_file")))
    if not to and allow_demo_defaults and recipient_name.lower() == "bob":
        to = ["bob@example.com"]

    subject = str(payload.get("subject") or _a2a_labeled_line(user_text, "subject")).strip()
    if not subject:
        subject = "Meeting options for tomorrow afternoon" if slots else "Email draft"

    body = str(payload.get("body") or _a2a_labeled_body(user_text)).strip()
    if not body:
        body = _a2a_default_draft_body(recipient_name, slots)
    html = payload.get("html") or payload.get("body_html")
    visible_file_paths = _normalize_visible_file_path_values(
        payload.get("visible_file_paths")
        or payload.get("visible_files")
        or payload.get("attachments")
    )

    return {
        "to": ", ".join(to),
        "cc": ", ".join(_normalize_addresses(payload.get("cc"))),
        "bcc": ", ".join(_normalize_addresses(payload.get("bcc"))),
        "recipient_name": recipient_name,
        "subject": subject,
        "body": body,
        "html": str(html) if html is not None else None,
        "visible_file_paths": visible_file_paths,
        "recipient_source": recipient_source,
        "high_importance": _a2a_bool(payload.get("high_importance"), default=False),
    }


def _a2a_normalize_draft(draft: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(draft)
    normalized["to"] = ", ".join(_normalize_addresses(normalized.get("to")))
    normalized["cc"] = ", ".join(_normalize_addresses(normalized.get("cc")))
    normalized["bcc"] = ", ".join(_normalize_addresses(normalized.get("bcc")))
    normalized["subject"] = str(normalized.get("subject") or "")
    normalized["body"] = str(normalized.get("body") or "")
    if normalized.get("html") is not None:
        normalized["html"] = str(normalized.get("html") or "")
    elif normalized.get("body_html") is not None:
        normalized["html"] = str(normalized.get("body_html") or "")
    normalized["visible_file_paths"] = _normalize_visible_file_path_values(
        normalized.get("visible_file_paths")
        or normalized.get("visible_files")
        or normalized.get("attachments")
    )
    normalized["recipient_source"] = _normalize_recipient_source(
        str(normalized.get("recipient_source") or normalized.get("recipient_group") or "")
    )
    if not _normalize_addresses(normalized.get("to")) and normalized["recipient_source"] == "notice_list":
        normalized["to"] = ", ".join(
            load_recipients_from_file(_notice_list_path(normalized.get("recipient_file")))
        )
    normalized["high_importance"] = _a2a_bool(normalized.get("high_importance"), default=False)
    return normalized


def _a2a_default_draft_body(recipient_name: str, slots: list[dict[str, str]]) -> str:
    if slots:
        first = slots[0]
        second = slots[1] if len(slots) > 1 else slots[0]
        return (
            f"Hi {recipient_name},\n\n"
            "I checked my calendar and can meet at either "
            f"{first.get('start', 'the first option')} - {first.get('end', '')} "
            f"or {second.get('start', 'the second option')} - {second.get('end', '')}.\n\n"
            "Which option works better for you?\n\n"
            "Best"
        )

    return f"Hi {recipient_name},\n\n\n\nBest"


def _a2a_labeled_line(text: str, label: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _a2a_labeled_body(text: str) -> str:
    match = re.search(r"(?ims)^\s*body\s*:\s*(.+)\s*$", text)
    if not match:
        return ""
    body = match.group(1).strip()
    body = re.sub(r"(?im)^\s*confirmed\s*:.*$", "", body).strip()
    return body


def _a2a_recipient_name_from_text(text: str) -> str:
    match = re.search(r"\bto\s+([A-Z][A-Za-z]+)\b", text)
    return match.group(1) if match else ""


def _a2a_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _a2a_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _a2a_required_str(payload: dict[str, Any], key: str, skill: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{skill} requires {key}.")
    return value


def _a2a_summarize_messages(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return "No matching email messages were found."
    lines = []
    for index, message in enumerate(messages, start=1):
        subject = message.get("subject") or "(No subject)"
        sender = message.get("from") or "(Unknown sender)"
        snippet = message.get("snippet") or message.get("body") or ""
        lines.append(f"{index}. {subject} from {sender}: {_preview_text(snippet, 180)}")
    return "\n".join(lines)


async def _a2a_complete(
    updater: A2ATaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [_data_part(output)],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [_text_part(text)],
            metadata={"structured_output": output},
        )
    )


async def _a2a_failed(
    updater: A2ATaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [_data_part(output)],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.failed(
        message=updater.new_agent_message(
            [_text_part(text)],
            metadata={"structured_output": output},
        )
    )


async def _a2a_requires_input(
    updater: A2ATaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.add_artifact(
        [_data_part(output)],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.requires_input(
        message=updater.new_agent_message(
            [_text_part(text)],
            metadata={"structured_output": output},
        ),
        final=True,
    )


def _object_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _extract_function_calls(response: Any) -> list[Any]:
    """Extract tool calls from a Chat Completions response."""
    try:
        tool_calls = response.choices[0].message.tool_calls
        return list(tool_calls) if tool_calls else []
    except (AttributeError, IndexError):
        return []


def _response_text(response: Any) -> str:
    """Extract text content from a Chat Completions response."""
    try:
        content = response.choices[0].message.content
        return str(content).strip() if content else "(No final response.)"
    except (AttributeError, IndexError):
        return "(No final response.)"


def _cc_message_to_dict(message: Any) -> dict[str, Any]:
    """Convert a Chat Completions message object to a plain dict for history."""
    msg: dict[str, Any] = {"role": "assistant"}
    content = getattr(message, "content", None)
    if content:
        msg["content"] = content
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in tool_calls
        ]
    return msg


def _preview_text(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated]"


def _redact_status_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    sensitive_keys = {
        "body",
        "html",
        "content",
        "text",
        "replace_text",
        "values",
        "values_json",
    }
    for key, value in arguments.items():
        if key in sensitive_keys:
            redacted[key] = _summarize_status_value(value)
        else:
            redacted[key] = value
    return redacted


def _summarize_status_value(value: Any) -> str:
    if isinstance(value, str):
        return f"<{len(value)} chars>"
    if isinstance(value, list):
        return f"<list length {len(value)}>"
    if isinstance(value, dict):
        return f"<object keys {len(value)}>"
    return f"<{type(value).__name__}>"


def _compact_email_message(
    item: dict[str, Any],
    max_body_chars: int = 4000,
    max_attachment_chars: int = 4000,
) -> dict[str, Any]:
    compact = dict(item)
    body = compact.get("body", "")
    if len(body) > max_body_chars:
        compact["body"] = f"{body[:max_body_chars]}\n...[truncated]"

    attachments = []
    for attachment in compact.get("attachments", []) or []:
        compact_attachment = dict(attachment)
        preview = compact_attachment.get("text_preview", "")
        if len(preview) > max_attachment_chars:
            compact_attachment["text_preview"] = (
                f"{preview[:max_attachment_chars]}\n...[truncated]"
            )
        attachments.append(compact_attachment)
    if attachments:
        compact["attachments"] = attachments

    return compact


def _normalize_addresses(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return parse_recipients([value])
    if isinstance(value, list):
        addresses: list[str] = []
        for item in value:
            addresses.extend(_normalize_addresses(item))
        return addresses
    return parse_recipients([str(value)])


def _normalize_sheet_values(value: Any) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        return [[str(value)]]
    rows: list[list[str]] = []
    for row in value:
        if isinstance(row, list):
            rows.append(["" if cell is None else str(cell) for cell in row])
        else:
            rows.append(["" if row is None else str(row)])
    return rows


def _parse_json_rows(value: str) -> list[list[str]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON rows: {exc}") from exc
    return _normalize_sheet_values(parsed)


def print_agent_status(status_file: Path, raw_json: bool = False) -> None:
    if not status_file.exists():
        raise RuntimeError(f"No agent status file found: {status_file}")

    status = json.loads(status_file.read_text(encoding="utf-8"))
    if raw_json:
        print(json.dumps(status, indent=2, default=str))
        return

    print(f"phase: {status.get('phase', '')}")
    print(f"message: {status.get('message', '')}")
    print(f"timestamp: {status.get('timestamp', '')}")
    for key in sorted(status):
        if key in {"phase", "message", "timestamp"}:
            continue
        print(f"{key}: {status[key]}")


def _safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value)
    return cleaned.strip("_")[:80]


def _decode_mime_header(value: str) -> str:
    decoded_parts = []
    for part, encoding in decode_header(value):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            decoded_parts.append(part)
    return "".join(decoded_parts)


def _extract_raw_message(fetch_data: list[bytes | tuple[bytes, bytes]]) -> bytes | None:
    for item in fetch_data:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1]
    return None


def _get_message_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        html_fallback = ""
        for part in msg.walk():
            content_disposition = part.get_content_disposition()
            if content_disposition == "attachment":
                continue

            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace").strip()
            if content_type == "text/plain" and text:
                return text
            if content_type == "text/html" and text and not html_fallback:
                html_fallback = text

        return html_fallback

    payload = msg.get_payload(decode=True)
    if payload is None:
        return str(msg.get_payload() or "")
    charset = msg.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace").strip()


def _extract_attachments(
    msg: email.message.Message,
    message_uid: str,
    subject: str,
    output_dir: Path,
    max_preview_chars: int = MAX_ATTACHMENT_PREVIEW_CHARS,
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    message_dir_name = _safe_filename(f"{message_uid}-{subject}") or message_uid
    message_dir = output_dir / message_dir_name

    for index, part in enumerate(msg.walk(), start=1):
        if part.is_multipart():
            continue

        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if disposition != "attachment" and not filename:
            continue

        content_type = part.get_content_type()
        decoded_filename = _decode_mime_header(filename) if filename else ""
        safe_filename = _safe_attachment_filename(
            decoded_filename or f"attachment-{index}{_extension_for_content_type(content_type)}"
        )

        payload = part.get_payload(decode=True)
        if payload is None:
            raw_payload = part.get_payload()
            payload = str(raw_payload or "").encode(part.get_content_charset() or "utf-8")

        message_dir.mkdir(parents=True, exist_ok=True)
        output_path = _unique_path(message_dir / safe_filename)
        output_path.write_bytes(payload)

        attachment: dict[str, Any] = {
            "filename": decoded_filename or safe_filename,
            "content_type": content_type,
            "size_bytes": len(payload),
            "path": str(output_path),
        }

        preview, note = _attachment_text_preview(
            data=payload,
            filename=decoded_filename or safe_filename,
            content_type=content_type,
            charset=part.get_content_charset(),
            max_chars=max_preview_chars,
        )
        if preview:
            attachment["text_preview"] = preview
        if note:
            attachment["read_note"] = note

        attachments.append(attachment)

    return attachments


def _attachment_text_preview(
    data: bytes,
    filename: str,
    content_type: str,
    charset: str | None,
    max_chars: int,
) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()

    try:
        if content_type == "text/html" or suffix in {".html", ".htm"}:
            text = _html_to_text(_decode_bytes(data, charset))
        elif (
            content_type.startswith("text/")
            or content_type
            in {
                "application/json",
                "application/xml",
                "application/ics",
                "application/calendar",
            }
            or suffix in TEXT_ATTACHMENT_EXTENSIONS
        ):
            text = _decode_bytes(data, charset)
        elif content_type == "application/pdf" or suffix == ".pdf":
            text = _extract_pdf_text(data)
        elif (
            content_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or suffix == ".docx"
        ):
            text = _extract_docx_text(data)
        elif (
            content_type
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            or suffix == ".xlsx"
        ):
            text = _extract_xlsx_text(data)
        else:
            return "", "Saved attachment; no text preview for this file type."
    except Exception as exc:
        return "", f"Saved attachment, but text extraction failed: {type(exc).__name__}: {exc}"

    text = text.strip()
    if not text:
        return "", "Saved attachment, but no readable text was found."
    if len(text) > max_chars:
        text = f"{text[:max_chars]}\n...[truncated]"
    return text, ""


def _decode_bytes(data: bytes, charset: str | None = None) -> str:
    encodings = [encoding for encoding in (charset, "utf-8", "utf-16", "latin-1") if encoding]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _html_to_text(value: str) -> str:
    without_scripts = re.sub(
        r"(?is)<(script|style).*?>.*?</\1>",
        " ",
        value,
    )
    with_breaks = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", without_scripts)
    without_tags = re.sub(r"(?s)<[^>]+>", " ", with_breaks)
    return html_lib.unescape(re.sub(r"[ \t]+", " ", without_tags))


def _coerce_email_body_and_html(body: str, html: str | None = None) -> tuple[str, str | None]:
    body_text = str(body or "")
    html_text = str(html) if html is not None else None
    if html_text is None and _looks_like_html_email_body(body_text):
        html_text = body_text
        body_text = _html_to_text(body_text).strip() or body_text
    return body_text, html_text


def _looks_like_html_email_body(value: str) -> bool:
    return bool(
        re.search(
            r"(?is)<\s*(?:!doctype|html|body|div|p|br|ul|ol|li|table|thead|tbody|tr|td|th|strong|em|span|a)\b",
            value.lstrip(),
        )
    )


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader
        except ImportError:
            raise RuntimeError("PDF text extraction requires pypdf or PyPDF2")

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def _extract_docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document_xml = archive.read("word/document.xml")

    root = ElementTree.fromstring(document_xml)
    text_nodes = []
    for element in root.iter():
        if element.tag.endswith("}t") and element.text:
            text_nodes.append(element.text)
        elif element.tag.endswith("}p"):
            text_nodes.append("\n")
    return " ".join(text_nodes)


def _extract_xlsx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        shared_strings = _read_xlsx_shared_strings(archive)
        sheet_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        sheets = []
        for sheet_name in sheet_names:
            root = ElementTree.fromstring(archive.read(sheet_name))
            rows = []
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                values = []
                for cell in row:
                    if not cell.tag.endswith("}c"):
                        continue
                    values.append(_xlsx_cell_value(cell, shared_strings))
                if any(values):
                    rows.append("\t".join(values))
            if rows:
                sheets.append(f"{Path(sheet_name).stem}\n" + "\n".join(rows))
        return "\n\n".join(sheets)


def _read_xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    strings = []
    for item in root:
        if not item.tag.endswith("}si"):
            continue
        strings.append("".join(node.text or "" for node in item.iter() if node.tag.endswith("}t")))
    return strings


def _xlsx_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if node.tag.endswith("}t"))

    value = ""
    for child in cell:
        if child.tag.endswith("}v") and child.text is not None:
            value = child.text
            break

    if cell_type == "s" and value:
        index = int(value)
        if 0 <= index < len(shared_strings):
            return shared_strings[index]
    return value


def _extension_for_content_type(content_type: str) -> str:
    extension = mimetypes.guess_extension(content_type) or ""
    if extension == ".jpe":
        return ".jpg"
    return extension


def _safe_attachment_filename(filename: str) -> str:
    name = Path(filename).name
    stem = Path(name).stem
    suffix = Path(name).suffix
    safe_stem = _safe_filename(stem) or "attachment"
    safe_suffix = re.sub(r"[^A-Za-z0-9.]", "", suffix)[:20]
    return f"{safe_stem}{safe_suffix}"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Could not create a unique attachment path for {path}")


def _visible_files_root() -> Path:
    root = Path(__file__).resolve().parent / DEFAULT_VISIBLE_FILES_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _relative_visible_parts(value: Path | str) -> list[str]:
    raw = str(value).strip()
    if not raw:
        raise ValueError("A visible file path is required.")
    if raw.startswith("~"):
        raise ValueError("Visible file paths cannot use home-directory expansion.")

    normalized = raw.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute():
        return []

    parts = [part for part in normalized.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("Visible file paths cannot contain '..'.")
    if DEFAULT_VISIBLE_FILES_DIR in parts:
        parts = parts[parts.index(DEFAULT_VISIBLE_FILES_DIR) + 1 :]
    if not parts:
        raise ValueError("A visible file path must identify a file or subdirectory.")
    return parts


def _resolve_visible_path(value: Path | str, *, allow_root: bool = False) -> Path:
    root = _visible_files_root()
    raw = str(value).strip()
    if not raw and allow_root:
        return root
    if not raw:
        raise ValueError("A visible file path is required.")

    path = Path(raw)
    if path.is_absolute():
        candidate = path.expanduser().resolve()
    else:
        candidate = root.joinpath(*_relative_visible_parts(raw)).resolve()

    if candidate != root and not candidate.is_relative_to(root):
        raise ValueError(f"Path is outside the visible files sandbox: {value}")
    if candidate == root and not allow_root:
        raise ValueError("A visible file path must identify a file below visible_files.")
    return candidate


def _resolve_visible_file_path(value: Path | str) -> Path:
    return _resolve_visible_path(value, allow_root=False)


def _resolve_visible_dir_path(value: Path | str | None = None) -> Path:
    if value is None or str(value).strip() == "":
        return _resolve_visible_path("", allow_root=True)
    return _resolve_visible_path(value, allow_root=True)


def _visible_file_relative_path(path: Path) -> str:
    root = _visible_files_root()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(root).as_posix()
        except ValueError:
            return path.name


def _visible_file_summary(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    root = root or _visible_files_root()
    relative_path = path.relative_to(root).as_posix()
    summary: dict[str, Any] = {
        "name": path.name,
        "relative_path": relative_path,
        "is_directory": False,
        "is_file": False,
        "is_symlink": path.is_symlink(),
        "content_type": mimetypes.guess_type(path.name)[0] or "",
    }
    if path.is_symlink():
        summary["readable"] = False
        summary["note"] = "Symlink targets are not readable through visible file tools."
        return summary

    try:
        stat = path.stat()
    except OSError as exc:
        summary["readable"] = False
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary

    summary.update(
        {
            "is_directory": path.is_dir(),
            "is_file": path.is_file(),
            "size_bytes": stat.st_size,
            "modified_time": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            "readable": path.is_file(),
        }
    )
    return summary


def _normalize_visible_file_path_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        raw = str(value).strip()
        return [raw] if raw else []
    if isinstance(value, (list, tuple, set)):
        paths: list[str] = []
        for item in value:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                paths.append(text)
        return paths
    return [str(value).strip()] if str(value).strip() else []


def _indent_text(value: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in value.splitlines())


def _notice_list_path(path: Path | str | None = None) -> Path:
    if path:
        return Path(path).expanduser()
    env_path = os.getenv("EMAIL_AGENT_NOTICE_LIST")
    if env_path:
        return Path(env_path).expanduser()
    return Path(__file__).resolve().parent / DEFAULT_NOTICE_LIST_FILE


def _normalize_recipient_source(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"notice", "notice_group", "notice_list", "group_notice"}:
        return "notice_list"
    if normalized in {"", "explicit", "manual", "addresses"}:
        return "explicit"
    raise ValueError(
        "Unknown recipient_source. Use 'explicit' or 'notice_list'."
    )


def load_recipients_from_file(path: Path | str) -> list[str]:
    recipient_path = Path(path).expanduser()
    if not recipient_path.exists():
        raise FileNotFoundError(f"Recipient list not found: {recipient_path}")

    entries: list[str] = []
    for line in recipient_path.read_text(encoding="utf-8").splitlines():
        cleaned = line.split("#", 1)[0].strip()
        if not cleaned:
            continue
        entries.append(cleaned)
    return parse_recipients(entries)


def parse_recipients(values: list[str] | None) -> list[str]:
    chunks: list[str] = []
    for value in values or []:
        text = str(value).replace(";", ",")
        for line in text.splitlines():
            cleaned = line.split("#", 1)[0].strip()
            if cleaned:
                chunks.append(cleaned)

    recipients = [
        address.strip()
        for _, address in getaddresses(chunks)
        if address and address.strip()
    ]
    for chunk in chunks:
        recipients.extend(match.group(0) for match in EMAIL_ADDRESS_RE.finditer(chunk))
    return _dedupe_strings(recipients)


def parse_datetime(value: str, timezone_name: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid datetime '{value}'. Use ISO format like '2026-04-30 14:00'."
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed


def build_ics_event(
    organizer: str,
    uid: str,
    title: str,
    start: datetime,
    end: datetime,
    attendees: list[str],
    description: str = "",
    location: str = "",
    method: str = "REQUEST",
) -> str:
    if end <= start:
        raise ValueError("Event end time must be after the start time.")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "PRODID:-//EmailDemo//EmailAgent//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{_ics_datetime(datetime.now(timezone.utc))}",
        f"DTSTART:{_ics_datetime(start.astimezone(timezone.utc))}",
        f"DTEND:{_ics_datetime(end.astimezone(timezone.utc))}",
        f"SUMMARY:{_ics_escape(title)}",
        f"DESCRIPTION:{_ics_escape(description)}",
        f"LOCATION:{_ics_escape(location)}",
        f"ORGANIZER;CN={_ics_escape(organizer)}:mailto:{organizer}",
    ]

    for attendee in attendees:
        lines.append(
            f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
            f"RSVP=TRUE;CN={_ics_escape(attendee)}:mailto:{attendee}"
        )

    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(_fold_ics_line(line) for line in lines) + "\r\n"


def _ics_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold_ics_line(line: str, limit: int = 75) -> str:
    if len(line.encode("utf-8")) <= limit:
        return line

    chunks = []
    current = ""
    current_len = 0
    for char in line:
        char_len = len(char.encode("utf-8"))
        if current and current_len + char_len > limit:
            chunks.append(current)
            current = f" {char}"
            current_len = 1 + char_len
        else:
            current += char
            current_len += char_len

    if current:
        chunks.append(current)
    return "\r\n".join(chunks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read/send Gmail, manage visible files, send calendar events, and run an AI tool agent."
    )
    parser.add_argument(
        "--email",
        default=os.getenv("EMAIL_AGENT_EMAIL", DEFAULT_EMAIL),
        help="Email account to use. Defaults to EMAIL_AGENT_EMAIL or the email_test.py account.",
    )
    parser.add_argument(
        "--timezone",
        default=os.getenv("EMAIL_AGENT_TIMEZONE", DEFAULT_TIMEZONE),
        help="Timezone for naive event datetimes.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    read_parser = subparsers.add_parser("read", help="Read recent email.")
    read_parser.add_argument("--folder", default="INBOX")
    read_parser.add_argument("--limit", type=int, default=5)
    read_parser.add_argument("--criteria", default="ALL")
    read_parser.add_argument("--body", action="store_true", help="Include message bodies.")
    read_parser.add_argument(
        "--attachments",
        action="store_true",
        help="Save attachments and print readable previews when possible.",
    )
    read_parser.add_argument(
        "--attachment-dir",
        type=Path,
        default=Path("attachments"),
        help="Directory for saved attachments.",
    )
    read_parser.add_argument("--mark-read", action="store_true")

    send_parser = subparsers.add_parser("send", help="Send email.")
    send_parser.add_argument("--to", action="append")
    send_parser.add_argument(
        "--to-file",
        type=Path,
        help="Text file with one or more recipient addresses per line.",
    )
    send_parser.add_argument(
        "--notice-list",
        action="store_true",
        help=f"Use the local {DEFAULT_NOTICE_LIST_FILE} as recipients.",
    )
    send_parser.add_argument("--cc", action="append")
    send_parser.add_argument("--bcc", action="append")
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body", default="")
    send_parser.add_argument("--body-file", type=Path)
    send_parser.add_argument("--html")
    send_parser.add_argument(
        "--attachment",
        action="append",
        type=Path,
        help="Attachment path inside visible_files. Relative paths are resolved from visible_files.",
    )
    send_parser.add_argument(
        "--high-importance",
        "--important",
        action="store_true",
        help="Send with high-importance priority headers.",
    )

    event_parser = subparsers.add_parser("event", help="Create/send a calendar event invite.")
    event_parser.add_argument("--title", required=True)
    event_parser.add_argument("--start", required=True)
    event_parser.add_argument("--end", required=True)
    event_parser.add_argument("--attendee", action="append")
    event_parser.add_argument("--description", default="")
    event_parser.add_argument("--location", default="")
    event_parser.add_argument(
        "--no-send",
        action="store_true",
        help="Write an .ics file locally instead of emailing the invite.",
    )
    event_parser.add_argument(
        "--google-api",
        action="store_true",
        help="Create the event in Google Calendar.",
    )
    event_parser.add_argument(
        "--allow-ics-fallback",
        action="store_true",
        help="With --google-api, create a local/email .ics fallback if Google Calendar is unavailable.",
    )

    calendar_list_parser = subparsers.add_parser(
        "calendar-list",
        help="List Google Calendar events.",
    )
    calendar_list_parser.add_argument("--start")
    calendar_list_parser.add_argument("--end")
    calendar_list_parser.add_argument(
        "--days",
        type=int,
        default=3,
        help="Number of days from now to list when --end is omitted.",
    )
    calendar_list_parser.add_argument("--max-results", type=int, default=20)

    calendar_update_parser = subparsers.add_parser(
        "calendar-update",
        help="Update a Google Calendar event by event ID.",
    )
    calendar_update_parser.add_argument("event_id")
    calendar_update_parser.add_argument("--title")
    calendar_update_parser.add_argument("--start")
    calendar_update_parser.add_argument("--end")
    calendar_update_parser.add_argument("--attendee", action="append")
    calendar_update_parser.add_argument("--description")
    calendar_update_parser.add_argument("--location")
    calendar_update_parser.add_argument(
        "--replace-attendees",
        action="store_true",
        help="Replace event attendees with --attendee values.",
    )
    calendar_update_parser.add_argument(
        "--no-send-updates",
        action="store_true",
        help="Do not notify attendees about the update.",
    )

    calendar_delete_parser = subparsers.add_parser(
        "calendar-delete",
        help="Delete a Google Calendar event by event ID.",
    )
    calendar_delete_parser.add_argument("event_id")
    calendar_delete_parser.add_argument(
        "--no-send-updates",
        action="store_true",
        help="Do not notify attendees about the cancellation.",
    )

    subparsers.add_parser(
        "calendar-auth",
        help="Authorize Google Workspace and create token.json.",
    )
    subparsers.add_parser(
        "calendar-status",
        help="Show Google Workspace dependency/auth status.",
    )
    subparsers.add_parser(
        "google-auth",
        help="Authorize Gmail, Calendar, Docs, and Sheets and create token.json.",
    )
    subparsers.add_parser(
        "google-status",
        help="Show Gmail, Calendar, Docs, and Sheets dependency/auth status.",
    )

    docs_list_parser = subparsers.add_parser("docs-list", help="List Google Docs.")
    docs_list_parser.add_argument("--query", default="")
    docs_list_parser.add_argument("--max-results", type=int, default=10)

    docs_read_parser = subparsers.add_parser("docs-read", help="Read a Google Doc.")
    docs_read_parser.add_argument("document_id")

    docs_create_parser = subparsers.add_parser("docs-create", help="Create a Google Doc.")
    docs_create_parser.add_argument("--title", required=True)
    docs_create_parser.add_argument("--content", default="")
    docs_create_parser.add_argument("--content-file", type=Path)

    docs_append_parser = subparsers.add_parser("docs-append", help="Append text to a Google Doc.")
    docs_append_parser.add_argument("document_id")
    docs_append_parser.add_argument("--text", default="")
    docs_append_parser.add_argument("--text-file", type=Path)

    docs_replace_parser = subparsers.add_parser("docs-replace", help="Replace text in a Google Doc.")
    docs_replace_parser.add_argument("document_id")
    docs_replace_parser.add_argument("--find", required=True)
    docs_replace_parser.add_argument("--replace", required=True)

    sheets_list_parser = subparsers.add_parser("sheets-list", help="List Google Sheets.")
    sheets_list_parser.add_argument("--query", default="")
    sheets_list_parser.add_argument("--max-results", type=int, default=10)

    sheets_read_parser = subparsers.add_parser("sheets-read", help="Read a Google Sheet range.")
    sheets_read_parser.add_argument("spreadsheet_id")
    sheets_read_parser.add_argument("--range", default="A1:Z50")

    sheets_create_parser = subparsers.add_parser("sheets-create", help="Create a Google Sheet.")
    sheets_create_parser.add_argument("--title", required=True)
    sheets_create_parser.add_argument("--sheet-name", default="Sheet1")
    sheets_create_parser.add_argument("--values-json", default="[]")

    sheets_write_parser = subparsers.add_parser("sheets-write", help="Overwrite a Google Sheet range.")
    sheets_write_parser.add_argument("spreadsheet_id")
    sheets_write_parser.add_argument("--range", required=True)
    sheets_write_parser.add_argument("--values-json", required=True)

    sheets_append_parser = subparsers.add_parser("sheets-append", help="Append rows to a Google Sheet.")
    sheets_append_parser.add_argument("spreadsheet_id")
    sheets_append_parser.add_argument("--range", required=True)
    sheets_append_parser.add_argument("--values-json", required=True)

    files_list_parser = subparsers.add_parser("files-list", help="List files in visible_files.")
    files_list_parser.add_argument("--directory", default="")
    files_list_parser.add_argument("--query", default="")
    files_list_parser.add_argument("--recursive", dest="recursive", action="store_true", default=True)
    files_list_parser.add_argument("--no-recursive", dest="recursive", action="store_false")
    files_list_parser.add_argument("--max-results", type=int, default=100)

    files_read_parser = subparsers.add_parser("files-read", help="Read a file from visible_files.")
    files_read_parser.add_argument("path")
    files_read_parser.add_argument("--max-chars", type=int, default=MAX_ATTACHMENT_PREVIEW_CHARS)

    files_write_parser = subparsers.add_parser("files-write", help="Write a file in visible_files.")
    files_write_parser.add_argument("path")
    files_write_parser.add_argument("--content", default="")
    files_write_parser.add_argument("--content-file", type=Path)
    files_write_parser.add_argument("--mode", choices=["overwrite", "append", "create_new"], default="overwrite")

    agent_parser = subparsers.add_parser(
        "agent",
        help="Run the AI tool-calling email/calendar agent.",
    )
    agent_parser.add_argument(
        "--model",
        default=os.getenv("EMAIL_AGENT_MODEL", DEFAULT_MODEL),
        help="OpenAI model for agent mode.",
    )
    agent_parser.add_argument(
        "--yes",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    agent_parser.add_argument(
        "--max-steps",
        type=int,
        default=8,
        help="Maximum model/tool loop steps.",
    )
    agent_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print live status updates to this terminal.",
    )
    agent_parser.add_argument(
        "--status",
        dest="status",
        action="store_true",
        default=True,
        help="Print live agent phase updates to this terminal. Enabled by default.",
    )
    agent_parser.add_argument(
        "--no-status",
        dest="status",
        action="store_false",
        help="Do not print live status updates in this terminal.",
    )
    agent_parser.add_argument(
        "--status-file",
        type=Path,
        default=Path(DEFAULT_AGENT_STATUS_FILE),
        help="File where the latest agent status JSON is written.",
    )
    agent_parser.add_argument(
        "--enable-remote-agent-tools",
        action="store_true",
        help="Allow the AI agent to use ADS discovery and outbound A2A request tools.",
    )
    agent_parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single request and exit, even if the agent asks a follow-up.",
    )
    agent_parser.add_argument(
        "--followups",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    agent_parser.add_argument("request", nargs=argparse.REMAINDER)

    a2a_parser = subparsers.add_parser(
        "serve-a2a",
        help="Run the Gmail-backed agent as an A2A HTTP server.",
    )
    a2a_parser.add_argument(
        "--host",
        default=os.getenv("EMAIL_AGENT_A2A_HOST", "0.0.0.0"),
        help="Host interface to bind.",
    )
    a2a_parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("EMAIL_AGENT_A2A_PORT", "9103")),
        help="Port for the A2A HTTP server.",
    )
    a2a_parser.add_argument(
        "--card",
        type=Path,
        default=DEFAULT_EMAIL_A2A_CARD,
        help="A2A Agent Card JSON file.",
    )
    a2a_parser.add_argument(
        "--advertise-url",
        default=os.getenv("EMAIL_AGENT_A2A_URL"),
        help="Externally reachable A2A URL to publish to ADS. Defaults to the Agent Card URL.",
    )
    a2a_parser.add_argument(
        "--enable-remote-agent-tools",
        action="store_true",
        help="Allow served requests to use ADS discovery and outbound A2A request tools.",
    )
    a2a_parser.add_argument(
        "--ads-url",
        default=(
            os.getenv("ADS_SERVER_ADDR")
            or os.getenv("ADS_URL")
            or os.getenv("ADS_SERVER_ADDRESS")
            or os.getenv("DIRECTORY_CLIENT_SERVER_ADDRESS")
            or DEFAULT_ADS_SERVER_ADDR
        ),
        help=f"ADS server address. Defaults to ADS_SERVER_ADDR/ADS_URL or {DEFAULT_ADS_SERVER_ADDR}.",
    )
    a2a_parser.add_argument(
        "--ads-backend",
        default=os.getenv("ADS_BACKEND", DEFAULT_ADS_BACKEND),
        choices=["cli"],
        help=f"Standalone ADS publisher backend. Defaults to {DEFAULT_ADS_BACKEND}.",
    )
    a2a_parser.add_argument(
        "--no-publish-ads",
        action="store_true",
        help="Start the A2A server without publishing the Agent Card to ADS.",
    )

    status_parser = subparsers.add_parser(
        "agent-status",
        help="Print the latest saved agent status.",
    )
    status_parser.add_argument(
        "--status-file",
        type=Path,
        default=Path(DEFAULT_AGENT_STATUS_FILE),
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON status.",
    )

    tools_parser = subparsers.add_parser("tools", help="List the AI agent tools.")
    tools_parser.add_argument(
        "--enable-remote-agent-tools",
        action="store_true",
        help="Include opt-in ADS discovery and outbound A2A request tools.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "agent" and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Missing OPENAI_API_KEY for the AI agent.")

    config = EmailConfig.from_env(
        email_address=args.email,
        timezone_name=args.timezone,
    )
    agent = EmailAgent(config)

    if args.command == "read":
        messages = agent.read_emails(
            folder=args.folder,
            limit=args.limit,
            criteria=args.criteria,
            include_body=args.body,
            include_attachments=args.attachments,
            attachment_dir=args.attachment_dir,
            mark_read=args.mark_read,
        )
        print(f"Found {len(messages)} messages")
        for item in messages:
            print(f"UID: {item['uid']}")
            print(f"From: {item['from']}")
            print(f"Subject: {item['subject']}")
            print(f"Date: {item['date']}")
            if args.body:
                print()
                print(item.get("body", ""))
            if args.attachments:
                attachments = item.get("attachments", [])
                print(f"Attachments: {len(attachments)}")
                for attachment in attachments:
                    print(f"  - {attachment.get('filename', '(unnamed)')}")
                    print(f"    type: {attachment.get('content_type', '')}")
                    print(f"    size: {attachment.get('size_bytes', 0)} bytes")
                    print(f"    path: {attachment.get('path', '')}")
                    text_preview = attachment.get("text_preview")
                    if text_preview:
                        print("    text_preview:")
                        print(_indent_text(str(text_preview), "      "))
                    elif attachment.get("read_note"):
                        print(f"    read_note: {attachment.get('read_note')}")
            print("-" * 60)
        return

    if args.command == "send":
        body = args.body_file.read_text(encoding="utf-8") if args.body_file else args.body
        recipients = parse_recipients(args.to)
        if args.to_file:
            recipients.extend(load_recipients_from_file(args.to_file))
        if args.notice_list:
            recipients.extend(load_recipients_from_file(_notice_list_path()))
        recipients = _dedupe_strings(recipients)
        message_id = agent.send_email(
            to=recipients,
            cc=parse_recipients(args.cc),
            bcc=parse_recipients(args.bcc),
            subject=args.subject,
            body=body,
            html=args.html,
            attachments=args.attachment or [],
            high_importance=args.high_importance,
        )
        print(f"Email sent: {message_id}")
        return

    if args.command == "event":
        start = parse_datetime(args.start, args.timezone)
        end = parse_datetime(args.end, args.timezone)
        result = agent.create_calendar_event(
            title=args.title,
            start=start,
            end=end,
            attendees=parse_recipients(args.attendee),
            description=args.description,
            location=args.location,
            send_invite=not args.no_send,
            use_google_api=args.google_api,
            allow_ics_fallback=args.allow_ics_fallback,
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "calendar-status":
        for key, value in agent.google_workspace_status().items():
            print(f"{key}: {value}")
        return

    if args.command == "calendar-auth":
        result = agent.authorize_google_workspace()
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "google-status":
        for key, value in agent.google_workspace_status().items():
            print(f"{key}: {value}")
        return

    if args.command == "google-auth":
        result = agent.authorize_google_workspace()
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "calendar-list":
        now = datetime.now(ZoneInfo(args.timezone))
        start = parse_datetime(args.start, args.timezone) if args.start else now
        end = (
            parse_datetime(args.end, args.timezone)
            if args.end
            else start + timedelta(days=args.days)
        )
        events = agent.list_calendar_events(
            start=start,
            end=end,
            max_results=args.max_results,
        )
        print(f"Found {len(events)} events")
        for event in events:
            print(f"Title: {event.get('summary', '')}")
            print(f"Start: {event.get('start', '')}")
            print(f"End: {event.get('end', '')}")
            if event.get("location"):
                print(f"Location: {event.get('location')}")
            if event.get("html_link"):
                print(f"Link: {event.get('html_link')}")
            print("-" * 60)
        return

    if args.command == "calendar-update":
        result = agent.update_calendar_event(
            event_id=args.event_id,
            title=args.title,
            start=parse_datetime(args.start, args.timezone) if args.start else None,
            end=parse_datetime(args.end, args.timezone) if args.end else None,
            attendees=parse_recipients(args.attendee),
            description=args.description,
            location=args.location,
            replace_attendees=args.replace_attendees,
            send_updates=not args.no_send_updates,
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "calendar-delete":
        result = agent.delete_calendar_event(
            event_id=args.event_id,
            send_updates=not args.no_send_updates,
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "docs-list":
        docs = agent.list_google_docs(query=args.query, max_results=args.max_results)
        print(f"Found {len(docs)} docs")
        for doc in docs:
            print(f"Title: {doc.get('name', '')}")
            print(f"ID: {doc.get('id', '')}")
            print(f"Modified: {doc.get('modifiedTime', '')}")
            print(f"Link: {doc.get('webViewLink', '')}")
            print("-" * 60)
        return

    if args.command == "docs-read":
        doc = agent.read_google_doc(args.document_id)
        print(f"Title: {doc.get('title', '')}")
        print(f"URL: {doc.get('url', '')}")
        print()
        print(doc.get("text", ""))
        return

    if args.command == "docs-create":
        content = (
            args.content_file.read_text(encoding="utf-8")
            if args.content_file
            else args.content
        )
        result = agent.create_google_doc(title=args.title, content=content)
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "docs-append":
        text = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
        result = agent.append_google_doc(document_id=args.document_id, text=text)
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "docs-replace":
        result = agent.replace_google_doc_text(
            document_id=args.document_id,
            find_text=args.find,
            replace_text=args.replace,
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "sheets-list":
        sheets = agent.list_google_sheets(query=args.query, max_results=args.max_results)
        print(f"Found {len(sheets)} sheets")
        for sheet in sheets:
            print(f"Title: {sheet.get('name', '')}")
            print(f"ID: {sheet.get('id', '')}")
            print(f"Modified: {sheet.get('modifiedTime', '')}")
            print(f"Link: {sheet.get('webViewLink', '')}")
            print("-" * 60)
        return

    if args.command == "sheets-read":
        result = agent.read_google_sheet(
            spreadsheet_id=args.spreadsheet_id,
            range_name=args.range,
        )
        print(f"Range: {result.get('range', '')}")
        print(f"URL: {result.get('url', '')}")
        for row in result.get("values", []):
            print("\t".join(str(cell) for cell in row))
        return

    if args.command == "sheets-create":
        result = agent.create_google_sheet(
            title=args.title,
            sheet_name=args.sheet_name,
            values=_parse_json_rows(args.values_json),
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "sheets-write":
        result = agent.write_google_sheet(
            spreadsheet_id=args.spreadsheet_id,
            range_name=args.range,
            values=_parse_json_rows(args.values_json),
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "sheets-append":
        result = agent.append_google_sheet(
            spreadsheet_id=args.spreadsheet_id,
            range_name=args.range,
            values=_parse_json_rows(args.values_json),
        )
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "files-list":
        files = agent.list_visible_files(
            directory=args.directory,
            query=args.query,
            recursive=args.recursive,
            max_results=args.max_results,
        )
        print(f"Visible root: {_visible_files_root()}")
        print(f"Found {len(files)} entries")
        for item in files:
            kind = "dir" if item.get("is_directory") else "file"
            if item.get("is_symlink"):
                kind = "symlink"
            print(f"{kind}: {item.get('relative_path', '')}")
            if item.get("is_file"):
                print(f"  size: {item.get('size_bytes', 0)} bytes")
                if item.get("content_type"):
                    print(f"  type: {item.get('content_type')}")
        return

    if args.command == "files-read":
        result = agent.read_visible_file(args.path, max_chars=args.max_chars)
        print(f"Path: {result.get('relative_path', '')}")
        print(f"Size: {result.get('size_bytes', 0)} bytes")
        if result.get("content_type"):
            print(f"Type: {result.get('content_type')}")
        if result.get("text"):
            print()
            print(result["text"])
        elif result.get("read_note"):
            print(result["read_note"])
        return

    if args.command == "files-write":
        content = (
            args.content_file.read_text(encoding="utf-8")
            if args.content_file
            else args.content
        )
        result = agent.write_visible_file(args.path, content, mode=args.mode)
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    if args.command == "agent":
        request = " ".join(args.request).strip()
        if not request:
            request = input("Agent request: ").strip()
        if not request:
            raise RuntimeError("Agent request cannot be empty.")

        ai_agent = ToolCallingEmailAgent(
            email_agent=agent,
            model=args.model,
            max_steps=args.max_steps,
            verbose=args.verbose,
            show_status=args.status,
            status_file=args.status_file,
            enable_remote_agent_tools=args.enable_remote_agent_tools,
        )
        if args.once or not sys.stdin.isatty():
            print(ai_agent.run(request))
        else:
            ai_agent.run_interactive(request)
        return

    if args.command == "serve-a2a":
        _require_a2a()
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("The uvicorn package is required for serve-a2a.") from exc

        _email_status("Loading Email Agent A2A card", card=str(args.card))
        card = _with_advertised_url(
            load_email_a2a_agent_card(args.card),
            args.advertise_url,
        )
        published_record: EmailAdsPublishedRecord | None = None
        if not args.no_publish_ads:
            published_record = publish_email_a2a_agent_card_to_ads(
                card,
                ads_url=args.ads_url,
                ads_backend=args.ads_backend,
            )
            cleanup_done = False

            def cleanup_ads_publication() -> None:
                nonlocal cleanup_done
                if cleanup_done:
                    return
                cleanup_done = True
                unpublish_email_a2a_agent_card_from_ads(
                    published_record,
                    ads_url=args.ads_url,
                )

            atexit.register(cleanup_ads_publication)
        else:
            _email_status("Skipping ADS publication", reason="--no-publish-ads")
        _email_status(
            "Starting Email Agent A2A server",
            host=args.host,
            port=args.port,
            agent_url=card.url,
        )
        try:
            uvicorn.run(
                create_email_a2a_app(
                    card,
                    agent,
                    enable_remote_agent_tools=args.enable_remote_agent_tools,
                ),
                host=args.host,
                port=args.port,
            )
        finally:
            if published_record is not None:
                cleanup_ads_publication()
        return

    if args.command == "agent-status":
        print_agent_status(args.status_file, raw_json=args.json)
        return

    if args.command == "tools":
        for tool in _agent_tool_schemas(args.enable_remote_agent_tools):
            print(f"{tool['name']}: {tool['description']}")
        return


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
