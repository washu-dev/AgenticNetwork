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
import asyncio
import base64
import email
import html as html_lib
import io
import json
import mimetypes
import os
import re
import sys
import uuid
import warnings
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

try:
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.apps import A2AStarletteApplication
    from a2a.server.events import EventQueue
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
    from a2a.types import AgentCard, DataPart, Part, TextPart
except ImportError as exc:
    A2A_IMPORT_ERROR: ImportError | None = exc
    AgentExecutor = object
    RequestContext = Any
    EventQueue = Any
    TaskUpdater = Any
    AgentCard = Any
    DataPart = Any
    Part = Any
    TextPart = Any
    A2AStarletteApplication = None
    DefaultRequestHandler = None
    InMemoryTaskStore = None
else:
    A2A_IMPORT_ERROR = None


DEFAULT_EMAIL = "emailagent.cspl@gmail.com"
DEFAULT_TIMEZONE = "America/Chicago"
DEFAULT_MODEL = "gpt-5-mini"
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
DEFAULT_EMAIL_A2A_CARD = (
    Path(__file__).resolve().parent
    / "AgenticNetwork"
    / "v2_agent_network"
    / "configs"
    / "agent_cards"
    / "email_agent.card.json"
)
MAX_ATTACHMENT_PREVIEW_CHARS = 8000
TEXT_ATTACHMENT_EXTENSIONS = {
    ".csv",
    ".eml",
    ".html",
    ".htm",
    ".ics",
    ".json",
    ".log",
    ".md",
    ".rtf",
    ".text",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


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
        include_body: bool = False,
        include_attachments: bool = False,
        attachment_dir: Path | str = "attachments",
        mark_read: bool = False,
    ) -> list[dict[str, Any]]:
        """Read newest matching messages through the Gmail API."""
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
        messages: list[dict[str, Any]] = []

        for ref in message_refs:
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
    ) -> str:
        """Send an email through the Gmail API."""
        if not to:
            raise ValueError("At least one recipient is required.")

        msg = self._build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
            attachments=attachments,
        )
        sent = (
            self._gmail_service()
            .users()
            .messages()
            .send(userId="me", body={"raw": _gmail_raw_message(msg)})
            .execute()
        )
        return sent.get("id", msg["Message-ID"])

    def write_email_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        html: str | None = None,
    ) -> str:
        """Create a Gmail draft."""
        if not to:
            raise ValueError("At least one recipient is required.")

        msg = self._build_email_message(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            html=html,
        )
        draft = (
            self._gmail_service()
            .users()
            .drafts()
            .create(userId="me", body={"message": {"raw": _gmail_raw_message(msg)}})
            .execute()
        )
        return draft.get("id", "")

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

        google_api_error = ""
        if use_google_api:
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
            if google_result:
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
        return result

    def list_calendar_events(
        self,
        start: datetime,
        end: datetime,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Read events from Google Calendar."""
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
        return [_format_google_event(item) for item in events_result.get("items", [])]

    def authorize_google_calendar(self) -> dict[str, Any]:
        """Run OAuth setup and write token.json for Google Workspace access."""
        return self.authorize_google_workspace()

    def authorize_google_workspace(self) -> dict[str, Any]:
        """Run OAuth setup and write token.json for Gmail, Calendar, Docs, and Sheets."""
        credentials = self._google_credentials(interactive=True)
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
        return status

    def list_google_docs(self, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
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
        return result.get("files", []) or []

    def read_google_doc(self, document_id: str) -> dict[str, Any]:
        document = (
            self._google_service("docs", "v1")
            .documents()
            .get(documentId=document_id)
            .execute()
        )
        return {
            "document_id": document_id,
            "title": document.get("title", ""),
            "text": _extract_google_doc_text(document),
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
        }

    def create_google_doc(self, title: str, content: str = "") -> dict[str, Any]:
        service = self._google_service("docs", "v1")
        document = service.documents().create(body={"title": title}).execute()
        document_id = document["documentId"]
        if content:
            service.documents().batchUpdate(
                documentId=document_id,
                body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
            ).execute()
        return {
            "document_id": document_id,
            "title": title,
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
        }

    def append_google_doc(self, document_id: str, text: str) -> dict[str, Any]:
        service = self._google_service("docs", "v1")
        document = service.documents().get(documentId=document_id).execute()
        end_index = _google_doc_end_index(document)
        service.documents().batchUpdate(
            documentId=document_id,
            body={"requests": [{"insertText": {"location": {"index": end_index}, "text": text}}]},
        ).execute()
        return {
            "document_id": document_id,
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
            "appended_chars": len(text),
        }

    def replace_google_doc_text(
        self,
        document_id: str,
        find_text: str,
        replace_text: str,
    ) -> dict[str, Any]:
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
        return {
            "document_id": document_id,
            "occurrences_changed": occurrences,
            "url": f"https://docs.google.com/document/d/{document_id}/edit",
        }

    def list_google_sheets(self, query: str = "", max_results: int = 10) -> list[dict[str, Any]]:
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
        return result.get("files", []) or []

    def read_google_sheet(self, spreadsheet_id: str, range_name: str = "A1:Z50") -> dict[str, Any]:
        values = (
            self._google_service("sheets", "v4")
            .spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        return {
            "spreadsheet_id": spreadsheet_id,
            "range": values.get("range", range_name),
            "values": values.get("values", []),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }

    def create_google_sheet(
        self,
        title: str,
        sheet_name: str = "Sheet1",
        values: list[list[Any]] | None = None,
    ) -> dict[str, Any]:
        service = self._google_service("sheets", "v4")
        spreadsheet = (
            service.spreadsheets()
            .create(body={"properties": {"title": title}, "sheets": [{"properties": {"title": sheet_name}}]})
            .execute()
        )
        spreadsheet_id = spreadsheet["spreadsheetId"]
        if values:
            self.write_google_sheet(spreadsheet_id, f"{sheet_name}!A1", values)
        return {
            "spreadsheet_id": spreadsheet_id,
            "title": title,
            "url": spreadsheet.get(
                "spreadsheetUrl",
                f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
            ),
        }

    def write_google_sheet(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
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
        return {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": result.get("updatedRange", range_name),
            "updated_cells": result.get("updatedCells", 0),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }

    def append_google_sheet(
        self,
        spreadsheet_id: str,
        range_name: str,
        values: list[list[Any]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict[str, Any]:
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
        return {
            "spreadsheet_id": spreadsheet_id,
            "updated_range": updates.get("updatedRange", range_name),
            "updated_cells": updates.get("updatedCells", 0),
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
        }

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

        if token_path.exists():
            missing_scopes = _missing_google_scopes(token_path)
            if missing_scopes:
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
            return credentials

        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
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

        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path),
            GOOGLE_WORKSPACE_SCOPES,
        )
        credentials = flow.run_local_server(port=0, open_browser=False, prompt="consent")
        token_path.write_text(credentials.to_json(), encoding="utf-8")
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
        return {
            "mode": "google_calendar_api",
            "created_in_google_calendar": True,
            "event_id": created.get("id", ""),
            "html_link": created.get("htmlLink", ""),
            "summary": created.get("summary", title),
            "start": created.get("start", {}),
            "end": created.get("end", {}),
        }


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
                    "description": "Whether to include message body text.",
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
            },
            "required": ["to", "cc", "bcc", "subject", "body"],
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
            },
            "required": ["to", "cc", "bcc", "subject", "body"],
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


class ToolCallingEmailAgent:
    """OpenClaw-style agent loop: model chooses tools, Python executes them."""

    def __init__(
        self,
        email_agent: EmailAgent,
        model: str = DEFAULT_MODEL,
        auto_approve: bool = False,
        max_steps: int = 8,
        verbose: bool = False,
        show_status: bool = False,
        status_file: Path | str | None = DEFAULT_AGENT_STATUS_FILE,
    ):
        self.email_agent = email_agent
        self.model = model
        self.auto_approve = auto_approve
        self.max_steps = max_steps
        self.verbose = verbose
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
            response = self._start_response(client, user_request)
            text, _ = self._finish_response(client, response)
            self.status.update("finished", "Agent finished", response_preview=_preview_text(text))
            return text
        except Exception as exc:
            self.status.update("error", f"{type(exc).__name__}: {exc}")
            raise

    def run_interactive(self, user_request: str, followups: int = 5) -> None:
        try:
            self.status.update(
                "starting",
                "Starting interactive agent request",
                request_preview=_preview_text(user_request),
                model=self.model,
            )
            client = self._openai_client()
            response = self._start_response(client, user_request)

            for _ in range(followups + 1):
                text, response = self._finish_response(client, response)
                print(text)

                if not _needs_user_followup(text) or not sys.stdin.isatty():
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
                response = client.responses.create(
                    model=self.model,
                    input=[{"role": "user", "content": next_request}],
                    tools=AGENT_TOOL_SCHEMAS,
                    previous_response_id=_object_get(response, "id"),
                    parallel_tool_calls=False,
                )

            self.status.update("finished", "Stopped after maximum follow-up turns")
            print("Stopped after the maximum number of follow-up turns.")
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

    def _start_response(self, client: Any, user_request: str) -> Any:
        self.status.update("reasoning", "Calling model", model=self.model)
        return client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": user_request},
            ],
            tools=AGENT_TOOL_SCHEMAS,
            parallel_tool_calls=False,
        )

    def _finish_response(self, client: Any, response: Any) -> tuple[str, Any]:
        for _ in range(self.max_steps):
            tool_calls = _extract_function_calls(response)
            if not tool_calls:
                return _response_text(response), response

            tool_outputs = []
            for call in tool_calls:
                name = _object_get(call, "name", "")
                call_id = _object_get(call, "call_id", "")
                raw_arguments = _object_get(call, "arguments", "{}")
                arguments: dict[str, Any] = {}
                try:
                    arguments = json.loads(raw_arguments or "{}")
                    self.status.update(
                        "calling_tool",
                        f"Calling tool: {name}",
                        tool=name,
                        tool_args=_redact_status_arguments(arguments),
                    )
                    result = self._run_tool(name, arguments)
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
                    self.status.update(
                        "tool_error",
                        f"Tool error: {name}",
                        tool=name,
                        error=result["error"],
                    )

                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, default=str),
                    }
                )

            self.status.update("reasoning", "Calling model with tool results", model=self.model)
            response = client.responses.create(
                model=self.model,
                input=tool_outputs,
                tools=AGENT_TOOL_SCHEMAS,
                previous_response_id=_object_get(response, "id"),
                parallel_tool_calls=False,
            )

        return (
            "I stopped after the maximum number of tool steps. "
            "Try narrowing the request or increasing --max-steps."
        ), response

    def _system_prompt(self) -> str:
        now = datetime.now(ZoneInfo(self.email_agent.config.timezone_name))
        return (
            "You are a local AI email and calendar agent. Use tools when you need "
            "fresh email data, when you need to write a draft, when the user asks "
            "to send email, or when the user asks to create a calendar event. "
            "Use the Gmail API email tools; do not ask for IMAP or app passwords. "
            "Use list_calendar_events when the user asks what is on their calendar. "
            "Use create_calendar_event with use_google_api true and allow_ics_fallback "
            "false when the user asks to add an event to their calendar. "
            "Use Google Docs tools for document requests and Google Sheets tools "
            "for spreadsheet requests. Ask for a document or spreadsheet ID when "
            "a search result is ambiguous. "
            "Ask the user for missing email addresses, dates, or times instead of "
            "guessing. Keep final answers concise and include the concrete result "
            "of any tool action. If the user explicitly asks you to send an email "
            "and the recipient/content are available, call the send_email tool; do "
            "not ask a separate permission question because the runtime will ask "
            "for approval. If the user confirms a previous draft should be sent, "
            "call send_email with the draft contents from the conversation. Sending "
            "email, marking mail as read, and sending calendar invites require "
            "runtime approval unless the command was run with --yes. Never say an "
            "event was added to Google Calendar unless the tool result has "
            "created_in_google_calendar true. Google Docs and Sheets write actions "
            "also require runtime approval. When the user "
            "asks about an attachment, file, PDF, document, spreadsheet, or invite "
            "inside an email, call read_email with include_attachments true. "
            f"The configured account is {self.email_agent.config.email_address}. "
            f"The local timezone is {self.email_agent.config.timezone_name}. "
            f"The current local time is {now.isoformat()}."
        )

    def _run_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "read_email":
            mark_read = bool(arguments.get("mark_read", False))
            if mark_read and not self._approve("mark messages as read", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}

            limit = int(arguments.get("limit", 5))
            limit = max(1, min(limit, 25))
            messages = self.email_agent.read_emails(
                folder=str(arguments.get("folder") or "INBOX"),
                limit=limit,
                criteria=str(arguments.get("criteria") or "ALL"),
                include_body=bool(arguments.get("include_body", False)),
                include_attachments=bool(arguments.get("include_attachments", False)),
                mark_read=mark_read,
            )
            return {
                "ok": True,
                "messages": [_compact_email_message(item) for item in messages],
            }

        if name == "write_email_draft":
            draft_id = self.email_agent.write_email_draft(
                to=_normalize_addresses(arguments.get("to")),
                cc=_normalize_addresses(arguments.get("cc")),
                bcc=_normalize_addresses(arguments.get("bcc")),
                subject=str(arguments.get("subject") or ""),
                body=str(arguments.get("body") or ""),
            )
            return {"ok": True, "draft_id": draft_id}

        if name == "send_email":
            if not self._approve("send email", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}

            message_id = self.email_agent.send_email(
                to=_normalize_addresses(arguments.get("to")),
                cc=_normalize_addresses(arguments.get("cc")),
                bcc=_normalize_addresses(arguments.get("bcc")),
                subject=str(arguments.get("subject") or ""),
                body=str(arguments.get("body") or ""),
            )
            return {"ok": True, "message_id": message_id}

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
            if not self._approve("create Google Doc", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}
            return {
                "ok": True,
                **self.email_agent.create_google_doc(
                    title=str(arguments.get("title") or ""),
                    content=str(arguments.get("content") or ""),
                ),
            }

        if name == "append_google_doc":
            if not self._approve("append to Google Doc", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}
            return {
                "ok": True,
                **self.email_agent.append_google_doc(
                    document_id=str(arguments.get("document_id") or ""),
                    text=str(arguments.get("text") or ""),
                ),
            }

        if name == "replace_google_doc_text":
            if not self._approve("replace text in Google Doc", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}
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
            if not self._approve("create Google Sheet", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}
            return {
                "ok": True,
                **self.email_agent.create_google_sheet(
                    title=str(arguments.get("title") or ""),
                    sheet_name=str(arguments.get("sheet_name") or "Sheet1"),
                    values=_normalize_sheet_values(arguments.get("values")),
                ),
            }

        if name == "write_google_sheet":
            if not self._approve("write Google Sheet range", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}
            return {
                "ok": True,
                **self.email_agent.write_google_sheet(
                    spreadsheet_id=str(arguments.get("spreadsheet_id") or ""),
                    range_name=str(arguments.get("range_name") or ""),
                    values=_normalize_sheet_values(arguments.get("values")),
                ),
            }

        if name == "append_google_sheet":
            if not self._approve("append rows to Google Sheet", arguments):
                return {"ok": False, "cancelled": True, "message": "Not approved."}
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
            if (send_invite or use_google_api) and not self._approve(
                "create calendar event / send invite",
                arguments,
            ):
                return {"ok": False, "cancelled": True, "message": "Not approved."}

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

        if name == "current_time":
            now = datetime.now(ZoneInfo(self.email_agent.config.timezone_name))
            return {
                "ok": True,
                "now": now.isoformat(),
                "timezone": self.email_agent.config.timezone_name,
            }

        return {"ok": False, "error": f"Unknown tool: {name}"}

    def _approve(self, action: str, arguments: dict[str, Any]) -> bool:
        if self.auto_approve:
            self.status.update(
                "approval_auto",
                f"Auto-approved action: {action}",
                action=action,
                tool_args=_redact_status_arguments(arguments),
            )
            return True
        if not sys.stdin.isatty():
            self.status.update(
                "approval_denied",
                f"Action needs approval but stdin is not interactive: {action}",
                action=action,
            )
            return False

        self.status.update(
            "waiting_for_approval",
            f"Waiting for approval: {action}",
            action=action,
            tool_args=_redact_status_arguments(arguments),
        )
        print()
        print(f"Approval required to {action}:")
        print(json.dumps(arguments, indent=2, default=str))
        answer = input("Approve? [y/N]: ").strip().lower()
        approved = answer in {"y", "yes"}
        self.status.update(
            "approval_granted" if approved else "approval_denied",
            f"{'Approved' if approved else 'Denied'} action: {action}",
            action=action,
        )
        return approved


class GmailEmailAgentExecutor(AgentExecutor):
    """A2A AgentExecutor wrapper around the Gmail-backed EmailAgent."""

    def __init__(self, email_agent: EmailAgent | None = None):
        if A2A_IMPORT_ERROR is not None:
            raise RuntimeError(
                "The a2a-sdk package is required for A2A server mode. "
                "Install the multi-agent dependencies first."
            ) from A2A_IMPORT_ERROR
        self.email_agent = email_agent or EmailAgent(EmailConfig.from_env())

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        skill = _a2a_skill_from_context(context)

        try:
            if skill in {"email/search", "email/read"}:
                await self._read_or_search_email(context, updater, skill)
                return
            if skill == "email/draft":
                await self._draft_email(context, updater)
                return
            if skill == "email/send":
                await self._send_email(context, updater)
                return
            if skill == "email/summarize_thread":
                await self._summarize_thread(context, updater)
                return
            if skill in {"email/google/status", "email/current_time"}:
                await self._workspace_utility(context, updater, skill)
                return
            if skill in {
                "email/calendar/status",
                "email/calendar/list_events",
                "email/calendar/create_event",
            }:
                await self._calendar_action(context, updater, skill)
                return
            if skill in {
                "email/docs/list",
                "email/docs/read",
                "email/docs/create",
                "email/docs/append",
                "email/docs/replace_text",
                "email/docs/create_and_email",
            }:
                await self._docs_action(context, updater, skill)
                return
            if skill in {
                "email/sheets/list",
                "email/sheets/read",
                "email/sheets/create",
                "email/sheets/write",
                "email/sheets/append",
            }:
                await self._sheets_action(context, updater, skill)
                return

            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-unsupported-skill",
                artifact_name="email_unsupported_skill",
                text=f"Unsupported email skill: {skill}",
                output={
                    "skill": skill,
                    "status": "unsupported",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
        except Exception as exc:
            await _a2a_complete(
                updater,
                skill=skill,
                artifact_id="email-error",
                artifact_name="email_error",
                text=f"{type(exc).__name__}: {exc}",
                output={
                    "skill": skill,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.reject()

    async def _read_or_search_email(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        skill: str,
    ) -> None:
        payload = _a2a_payload_from_context(context)
        mark_read = _a2a_bool(payload.get("mark_read"), default=False)
        if mark_read and not _a2a_confirmed(context):
            await _a2a_requires_input(
                updater,
                skill=skill,
                artifact_id="email-mark-read-confirmation-required",
                artifact_name="email_mark_read_confirmation_required",
                text="Marking email as read changes mailbox state and requires confirmation.",
                output={
                    "skill": skill,
                    "confirmation_required": True,
                    "reason": "Marking email as read changes mailbox state and requires confirmation.",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
            return

        include_body = _a2a_bool(payload.get("include_body"), default=skill == "email/read")
        messages = await asyncio.to_thread(
            self.email_agent.read_emails,
            folder=str(payload.get("folder") or "INBOX"),
            limit=_a2a_int(payload.get("limit"), default=5, minimum=1, maximum=25),
            criteria=str(payload.get("criteria") or payload.get("query") or "ALL"),
            include_body=include_body,
            include_attachments=_a2a_bool(payload.get("include_attachments"), default=False),
            attachment_dir=Path(str(payload.get("attachment_dir") or "attachments")),
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

    async def _draft_email(self, context: RequestContext, updater: TaskUpdater) -> None:
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
            "context_id": context.context_id,
            "task_id": context.task_id,
        }

        if _a2a_bool(payload.get("create_gmail_draft"), default=False):
            if not _a2a_confirmed(context):
                await _a2a_requires_input(
                    updater,
                    skill="email/draft",
                    artifact_id="email-draft-confirmation-required",
                    artifact_name="email_draft_confirmation_required",
                    text="Creating a Gmail draft changes mailbox state and requires confirmation.",
                    output={
                        **output,
                        "confirmation_required": True,
                        "reason": "Creating a Gmail draft changes mailbox state and requires confirmation.",
                    },
                )
                return
            draft_id = await asyncio.to_thread(
                self.email_agent.write_email_draft,
                to=_normalize_addresses(draft.get("to")),
                cc=_normalize_addresses(draft.get("cc")),
                bcc=_normalize_addresses(draft.get("bcc")),
                subject=str(draft.get("subject") or ""),
                body=str(draft.get("body") or ""),
                html=draft.get("html") if isinstance(draft.get("html"), str) else None,
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

    async def _send_email(self, context: RequestContext, updater: TaskUpdater) -> None:
        payload = _a2a_payload_from_context(context)
        if not _a2a_confirmed(context):
            await _a2a_requires_input(
                updater,
                skill="email/send",
                artifact_id="email-send-confirmation-required",
                artifact_name="email_send_confirmation_required",
                text="Sending email is a write action and requires explicit confirmation.",
                output={
                    "skill": "email/send",
                    "confirmation_required": True,
                    "reason": "Sending email is a write action and requires explicit confirmation.",
                    "context_id": context.context_id,
                    "task_id": context.task_id,
                },
            )
            return

        draft = _a2a_draft_from_payload(
            payload=payload,
            user_text=_a2a_user_text(context),
            allow_demo_defaults=False,
        )
        to = _normalize_addresses(draft.get("to"))
        if not to:
            raise ValueError("email/send requires at least one recipient address.")
        if not str(draft.get("subject") or "").strip():
            raise ValueError("email/send requires a subject.")
        if not str(draft.get("body") or "").strip():
            raise ValueError("email/send requires a body.")

        message_id = await asyncio.to_thread(
            self.email_agent.send_email,
            to=to,
            cc=_normalize_addresses(draft.get("cc")),
            bcc=_normalize_addresses(draft.get("bcc")),
            subject=str(draft.get("subject") or ""),
            body=str(draft.get("body") or ""),
            html=draft.get("html") if isinstance(draft.get("html"), str) else None,
        )
        output = {
            "skill": "email/send",
            "status": "completed",
            "message_id": message_id,
            "sent_email": {
                "to": ", ".join(to),
                "subject": str(draft.get("subject") or ""),
            },
            "context_id": context.context_id,
            "task_id": context.task_id,
        }
        await _a2a_complete(
            updater,
            skill="email/send",
            artifact_id="email-send",
            artifact_name="email_send",
            text=f"Sent email to {', '.join(to)}.",
            output=output,
        )

    async def _summarize_thread(self, context: RequestContext, updater: TaskUpdater) -> None:
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
        context: RequestContext,
        updater: TaskUpdater,
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
        context: RequestContext,
        updater: TaskUpdater,
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

        if not _a2a_confirmed(context):
            await _a2a_confirmation_required(context, updater, skill, "Creating calendar events or sending invites requires confirmation.")
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
        context: RequestContext,
        updater: TaskUpdater,
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

        if not _a2a_confirmed(context):
            await _a2a_confirmation_required(context, updater, skill, "Creating or modifying Google Docs requires confirmation.")
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
        context: RequestContext,
        updater: TaskUpdater,
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

        if not _a2a_confirmed(context):
            await _a2a_confirmation_required(context, updater, skill, "Creating or modifying Google Sheets requires confirmation.")
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
) -> AgentCard:
    _require_a2a()
    with Path(card_path).open("r", encoding="utf-8") as fh:
        return AgentCard.model_validate(json.load(fh))


def create_email_a2a_app(
    agent_card: AgentCard,
    email_agent: EmailAgent | None = None,
) -> Any:
    _require_a2a()
    request_handler = DefaultRequestHandler(
        agent_executor=GmailEmailAgentExecutor(email_agent),
        task_store=InMemoryTaskStore(),
    )
    return A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    ).build()


def _require_a2a() -> None:
    if A2A_IMPORT_ERROR is not None:
        raise RuntimeError(
            "A2A server mode requires the a2a-sdk, starlette, and uvicorn packages. "
            "Install the AgenticNetwork dependencies before running serve-a2a."
        ) from A2A_IMPORT_ERROR


def _a2a_skill_from_context(context: RequestContext) -> str:
    metadata = context.metadata or {}
    return str(metadata.get("skill") or "email/draft")


def _a2a_confirmed(context: RequestContext) -> bool:
    metadata = context.metadata or {}
    return metadata.get("confirmed") is True or metadata.get("confirmation_status") == "confirmed"


def _a2a_user_text(context: RequestContext) -> str:
    try:
        return str(context.get_user_input())
    except Exception:
        return ""


def _a2a_payload_from_context(context: RequestContext) -> dict[str, Any]:
    payload: dict[str, Any] = dict(context.metadata or {})
    if context.message:
        for part in context.message.parts:
            root = part.root
            if isinstance(root, DataPart) and isinstance(root.data, dict):
                payload.update(root.data)
    return payload


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
    if not to and allow_demo_defaults and recipient_name.lower() == "bob":
        to = ["bob@example.com"]

    subject = str(payload.get("subject") or "").strip()
    if not subject:
        subject = "Meeting options for tomorrow afternoon" if slots else "Email draft"

    body = str(payload.get("body") or "").strip()
    if not body:
        body = _a2a_default_draft_body(recipient_name, slots)

    return {
        "to": ", ".join(to),
        "cc": ", ".join(_normalize_addresses(payload.get("cc"))),
        "bcc": ", ".join(_normalize_addresses(payload.get("bcc"))),
        "recipient_name": recipient_name,
        "subject": subject,
        "body": body,
    }


def _a2a_normalize_draft(draft: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(draft)
    normalized["to"] = ", ".join(_normalize_addresses(normalized.get("to")))
    normalized["cc"] = ", ".join(_normalize_addresses(normalized.get("cc")))
    normalized["bcc"] = ", ".join(_normalize_addresses(normalized.get("bcc")))
    normalized["subject"] = str(normalized.get("subject") or "")
    normalized["body"] = str(normalized.get("body") or "")
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


async def _a2a_confirmation_required(
    context: RequestContext,
    updater: TaskUpdater,
    skill: str,
    reason: str,
) -> None:
    await _a2a_requires_input(
        updater,
        skill=skill,
        artifact_id=skill.replace("/", "-") + "-confirmation-required",
        artifact_name=skill.replace("/", "_").replace("-", "_") + "_confirmation_required",
        text=reason,
        output={
            "skill": skill,
            "confirmation_required": True,
            "reason": reason,
            "context_id": context.context_id,
            "task_id": context.task_id,
        },
    )


async def _a2a_complete(
    updater: TaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.start_work()
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.complete(
        message=updater.new_agent_message(
            [Part(root=TextPart(text=text))],
            metadata={"structured_output": output},
        )
    )


async def _a2a_requires_input(
    updater: TaskUpdater,
    *,
    skill: str,
    artifact_id: str,
    artifact_name: str,
    text: str,
    output: dict[str, Any],
) -> None:
    await updater.add_artifact(
        [Part(root=DataPart(data=output))],
        artifact_id=artifact_id,
        name=artifact_name,
        metadata={"skill": skill, "content_type": "application/json"},
    )
    await updater.requires_input(
        message=updater.new_agent_message(
            [Part(root=TextPart(text=text))],
            metadata={"structured_output": output},
        ),
        final=True,
    )


def _object_get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _extract_function_calls(response: Any) -> list[Any]:
    return [
        item
        for item in (_object_get(response, "output", []) or [])
        if _object_get(item, "type") == "function_call"
    ]


def _response_text(response: Any) -> str:
    output_text = _object_get(response, "output_text")
    if output_text:
        return str(output_text)

    parts: list[str] = []
    for item in (_object_get(response, "output", []) or []):
        if _object_get(item, "type") != "message":
            continue
        for content in (_object_get(item, "content", []) or []):
            if _object_get(content, "type") in {"output_text", "text"}:
                text = _object_get(content, "text", "")
                if text:
                    parts.append(str(text))
    return "\n".join(parts).strip() or "(No final response.)"


def _needs_user_followup(text: str) -> bool:
    normalized = " ".join(text.strip().lower().split())
    if not normalized:
        return False

    followup_phrases = (
        "would you like me",
        "do you want me",
        "should i",
        "shall i",
        "may i",
        "can i send",
        "please provide",
        "please confirm",
    )
    if any(phrase in normalized for phrase in followup_phrases):
        return True

    return normalized.endswith("?")


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


def _indent_text(value: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" for line in value.splitlines())


def parse_recipients(values: list[str] | None) -> list[str]:
    recipients: list[str] = []
    for value in values or []:
        recipients.extend(item.strip() for item in value.split(",") if item.strip())
    return recipients


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
        description="Read/send Gmail, send calendar events, and run an AI tool agent."
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
    send_parser.add_argument("--to", action="append", required=True)
    send_parser.add_argument("--cc", action="append")
    send_parser.add_argument("--bcc", action="append")
    send_parser.add_argument("--subject", required=True)
    send_parser.add_argument("--body", default="")
    send_parser.add_argument("--body-file", type=Path)
    send_parser.add_argument("--html")
    send_parser.add_argument("--attachment", action="append", type=Path)

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
        help="Auto-approve write actions such as sending mail or invites.",
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
        "--once",
        action="store_true",
        help="Run a single request and exit, even if the agent asks a follow-up.",
    )
    agent_parser.add_argument(
        "--followups",
        type=int,
        default=5,
        help="Maximum follow-up turns when the agent asks for input.",
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

    subparsers.add_parser("tools", help="List the AI agent tools.")

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
        message_id = agent.send_email(
            to=parse_recipients(args.to),
            cc=parse_recipients(args.cc),
            bcc=parse_recipients(args.bcc),
            subject=args.subject,
            body=body,
            html=args.html,
            attachments=args.attachment or [],
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

    if args.command == "agent":
        request = " ".join(args.request).strip()
        if not request:
            request = input("Agent request: ").strip()
        if not request:
            raise RuntimeError("Agent request cannot be empty.")

        ai_agent = ToolCallingEmailAgent(
            email_agent=agent,
            model=args.model,
            auto_approve=args.yes,
            max_steps=args.max_steps,
            verbose=args.verbose,
            show_status=args.status,
            status_file=args.status_file,
        )
        if args.once or not sys.stdin.isatty():
            print(ai_agent.run(request))
        else:
            ai_agent.run_interactive(request, followups=args.followups)
        return

    if args.command == "serve-a2a":
        _require_a2a()
        try:
            import uvicorn
        except ImportError as exc:
            raise RuntimeError("The uvicorn package is required for serve-a2a.") from exc

        card = load_email_a2a_agent_card(args.card)
        uvicorn.run(
            create_email_a2a_app(card, agent),
            host=args.host,
            port=args.port,
        )
        return

    if args.command == "agent-status":
        print_agent_status(args.status_file, raw_json=args.json)
        return

    if args.command == "tools":
        for tool in AGENT_TOOL_SCHEMAS:
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
