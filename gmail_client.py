"""
Gmail API Client

Provides a clean interface for interacting with Gmail API.
Handles authentication, email fetching, and modifications.
"""

import os
import re
import base64
import requests
import httpx
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

# Gmail API scopes
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

# Project directory
PROJECT_DIR = Path(__file__).parent
CREDENTIALS_FILE = PROJECT_DIR / 'credentials.json'
TOKEN_FILE = PROJECT_DIR / 'token.json'


@dataclass
class Email:
    """Represents an email message."""
    id: str
    thread_id: str
    subject: str
    sender: str
    sender_email: str
    date: datetime
    snippet: str
    labels: list[str] = field(default_factory=list)
    body_html: Optional[str] = None
    body_text: Optional[str] = None
    unsubscribe_url: Optional[str] = None
    unsubscribe_mailto: Optional[str] = None  # mailto: address for email-based unsubscribe
    unsubscribe_post: bool = False  # True if one-click unsubscribe is supported
    is_read: bool = True

    @property
    def category(self) -> str:
        """Get email category from labels."""
        for label in self.labels:
            if label.startswith('CATEGORY_'):
                return label.replace('CATEGORY_', '').lower()
        return 'primary'

    @property
    def is_promotions(self) -> bool:
        return 'CATEGORY_PROMOTIONS' in self.labels


def one_click_unsubscribe(url: str, timeout: int = 10) -> tuple[bool, str]:
    """
    Perform one-click unsubscribe via HTTP POST (RFC 8058).

    Args:
        url: The unsubscribe URL from List-Unsubscribe header
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        response = requests.post(
            url,
            data='List-Unsubscribe=One-Click',
            headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'Gmail-Unsubscribe-Client/1.0'
            },
            timeout=timeout,
            allow_redirects=True
        )

        # Success codes: 200, 202 (accepted), 204 (no content)
        if response.status_code in [200, 202, 204]:
            return True, f"Success (HTTP {response.status_code})"
        elif response.status_code in [301, 302, 303, 307, 308]:
            return True, "Success (redirected)"
        else:
            return False, f"HTTP {response.status_code}"

    except requests.Timeout:
        return False, "Timeout"
    except requests.RequestException as e:
        return False, str(e)[:50]


async def async_one_click_unsubscribe(
    url: str,
    timeout: int = 15,
    max_retries: int = 2,
    backoff_factor: float = 1.5
) -> tuple[bool, str]:
    """
    Async version of one-click unsubscribe via HTTP POST (RFC 8058).
    Uses httpx for non-blocking requests with retry logic.

    Args:
        url: The unsubscribe URL from List-Unsubscribe header
        timeout: Request timeout in seconds
        max_retries: Maximum number of retry attempts
        backoff_factor: Multiplier for exponential backoff

    Returns:
        Tuple of (success: bool, message: str)
    """
    import asyncio

    last_error = "Unknown error"

    for attempt in range(max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.post(
                    url,
                    data='List-Unsubscribe=One-Click',
                    headers={
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'User-Agent': 'Gmail-Unsubscribe-Client/1.0'
                    }
                )

            # Success codes: 200, 202 (accepted), 204 (no content)
            if response.status_code in [200, 202, 204]:
                return True, f"Success (HTTP {response.status_code})"
            elif response.status_code in [301, 302, 303, 307, 308]:
                return True, "Success (redirected)"
            elif response.status_code >= 500:
                # Server error - retry
                last_error = f"HTTP {response.status_code}"
                if attempt < max_retries:
                    await asyncio.sleep(backoff_factor ** attempt)
                    continue
            else:
                # Client error (4xx) - don't retry
                return False, f"HTTP {response.status_code}"

        except httpx.TimeoutException:
            last_error = "Timeout"
            if attempt < max_retries:
                await asyncio.sleep(backoff_factor ** attempt)
                continue
        except httpx.RequestError as e:
            last_error = str(e)[:50]
            if attempt < max_retries:
                await asyncio.sleep(backoff_factor ** attempt)
                continue

    return False, last_error


async def send_unsubscribe_email(mailto: str, gmail_service) -> tuple[bool, str]:
    """
    Send an unsubscribe email using Gmail API.

    Args:
        mailto: The mailto string (e.g., "unsubscribe@example.com?subject=Unsubscribe")
        gmail_service: Authenticated Gmail API service

    Returns:
        Tuple of (success: bool, message: str)
    """
    import base64
    from email.mime.text import MIMEText
    from urllib.parse import urlparse, parse_qs

    try:
        # Parse mailto string
        if '?' in mailto:
            to_address, query = mailto.split('?', 1)
            params = parse_qs(query)
            subject = params.get('subject', ['Unsubscribe'])[0]
            body = params.get('body', ['Please unsubscribe me from this mailing list.'])[0]
        else:
            to_address = mailto
            subject = 'Unsubscribe'
            body = 'Please unsubscribe me from this mailing list.'

        # Create email message
        message = MIMEText(body)
        message['to'] = to_address
        message['subject'] = subject

        # Encode and send
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        gmail_service.users().messages().send(
            userId='me',
            body={'raw': raw}
        ).execute()

        return True, "Email sent"

    except Exception as e:
        return False, f"Email failed: {str(e)[:30]}"


class GmailClient:
    """Gmail API client with authentication and email operations."""

    def __init__(self):
        self.service = None
        self._creds = None

    def authenticate(self) -> bool:
        """
        Authenticate with Gmail API using OAuth 2.0.
        Returns True if successful, False otherwise.
        """
        creds = None

        # Load existing token
        if TOKEN_FILE.exists():
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

        # Refresh or get new credentials
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None

            if not creds:
                if not CREDENTIALS_FILE.exists():
                    raise FileNotFoundError(
                        f"credentials.json not found at {CREDENTIALS_FILE}"
                    )

                flow = InstalledAppFlow.from_client_secrets_file(
                    str(CREDENTIALS_FILE), SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Save token
            with open(TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())

        self._creds = creds
        self.service = build('gmail', 'v1', credentials=creds)
        return True

    def get_emails(
        self,
        query: str = '',
        max_results: int = 100,
        page_token: Optional[str] = None
    ) -> tuple[list[Email], Optional[str]]:
        """
        Fetch emails matching query.
        """
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        results = self.service.users().messages().list(
            userId='me',
            q=query,
            maxResults=min(max_results, 100),
            pageToken=page_token
        ).execute()

        messages = results.get('messages', [])
        next_page = results.get('nextPageToken')

        emails = []
        for msg in messages:
            email = self._fetch_email_metadata(msg['id'])
            if email:
                emails.append(email)

        return emails, next_page

    def get_email(self, email_id: str, include_body: bool = True) -> Optional[Email]:
        """Get a single email by ID with full content."""
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        try:
            msg = self.service.users().messages().get(
                userId='me',
                id=email_id,
                format='full'
            ).execute()

            return self._parse_message(msg, include_body=include_body)
        except Exception:
            return None

    def delete_emails(self, email_ids: list[str]) -> int:
        """Delete emails (move to trash)."""
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        deleted = 0
        for email_id in email_ids:
            try:
                self.service.users().messages().trash(
                    userId='me',
                    id=email_id
                ).execute()
                deleted += 1
            except Exception:
                pass

        return deleted

    def archive_emails(self, email_ids: list[str]) -> int:
        """Archive emails (remove INBOX label)."""
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        archived = 0
        for email_id in email_ids:
            try:
                self.service.users().messages().modify(
                    userId='me',
                    id=email_id,
                    body={'removeLabelIds': ['INBOX']}
                ).execute()
                archived += 1
            except Exception:
                pass

        return archived

    def mark_as_read(self, email_ids: list[str]) -> int:
        """Mark emails as read."""
        if not self.service:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        count = 0
        for email_id in email_ids:
            try:
                self.service.users().messages().modify(
                    userId='me',
                    id=email_id,
                    body={'removeLabelIds': ['UNREAD']}
                ).execute()
                count += 1
            except Exception:
                pass

        return count

    def _fetch_email_metadata(self, email_id: str) -> Optional[Email]:
        """Fetch email metadata (without full body)."""
        try:
            msg = self.service.users().messages().get(
                userId='me',
                id=email_id,
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date', 'List-Unsubscribe', 'List-Unsubscribe-Post']
            ).execute()

            return self._parse_message(msg, include_body=False)
        except Exception:
            return None

    def _parse_message(self, msg: dict, include_body: bool = False) -> Email:
        """Parse Gmail API message into Email object."""
        headers = {h['name']: h['value'] for h in msg.get('payload', {}).get('headers', [])}

        # Parse sender
        sender_raw = headers.get('From', 'Unknown')
        sender_name, sender_email = self._parse_sender(sender_raw)

        # Parse date
        date_str = headers.get('Date', '')
        date = self._parse_date(date_str)

        # Get unsubscribe URL and mailto from header
        list_unsub_header = headers.get('List-Unsubscribe', '')
        unsub_url = self._extract_unsubscribe_url(list_unsub_header)
        unsub_mailto = self._extract_unsubscribe_mailto(list_unsub_header)

        # Check for one-click unsubscribe support (RFC 8058)
        unsub_post = 'List-Unsubscribe-Post' in headers and unsub_url is not None

        # Parse body if requested
        body_html = None
        body_text = None
        if include_body:
            body_html, body_text = self._decode_body(msg.get('payload', {}))

            # Try to find unsubscribe URL in body if not in header
            if not unsub_url and body_html:
                unsub_url = self._extract_unsubscribe_from_body(body_html)

        return Email(
            id=msg['id'],
            thread_id=msg.get('threadId', ''),
            subject=headers.get('Subject', '(No Subject)'),
            sender=sender_name,
            sender_email=sender_email,
            date=date,
            snippet=msg.get('snippet', ''),
            labels=msg.get('labelIds', []),
            body_html=body_html,
            body_text=body_text,
            unsubscribe_url=unsub_url,
            unsubscribe_mailto=unsub_mailto,
            unsubscribe_post=unsub_post,
            is_read='UNREAD' not in msg.get('labelIds', [])
        )

    def _parse_sender(self, sender: str) -> tuple[str, str]:
        """Parse sender into name and email."""
        match = re.match(r'^"?([^"<]+)"?\s*<([^>]+)>', sender)
        if match:
            return match.group(1).strip(), match.group(2).strip()

        email_match = re.search(r'[\w\.-]+@[\w\.-]+', sender)
        if email_match:
            email = email_match.group(0)
            name = email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
            return name, email

        return sender, sender

    def _parse_date(self, date_str: str) -> datetime:
        """Parse email date string."""
        from email.utils import parsedate_to_datetime
        try:
            return parsedate_to_datetime(date_str)
        except Exception:
            return datetime.now()

    def _extract_unsubscribe_url(self, header: str) -> Optional[str]:
        """Extract HTTP URL from List-Unsubscribe header."""
        if not header:
            return None
        urls = re.findall(r'<(https?://[^>]+)>', header)
        return urls[0] if urls else None

    def _extract_unsubscribe_mailto(self, header: str) -> Optional[str]:
        """Extract mailto: address from List-Unsubscribe header."""
        if not header:
            return None
        # Match mailto: links like <mailto:unsubscribe@example.com> or <mailto:unsub@example.com?subject=unsubscribe>
        mailtos = re.findall(r'<mailto:([^>]+)>', header)
        return mailtos[0] if mailtos else None

    def _decode_body(self, payload: dict) -> tuple[Optional[str], Optional[str]]:
        """Decode email body from payload."""
        html_body = None
        text_body = None

        def extract_parts(part):
            nonlocal html_body, text_body

            mime_type = part.get('mimeType', '')
            data = part.get('body', {}).get('data')

            if data:
                decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                if mime_type == 'text/html':
                    html_body = decoded
                elif mime_type == 'text/plain':
                    text_body = decoded

            for sub_part in part.get('parts', []):
                extract_parts(sub_part)

        extract_parts(payload)
        return html_body, text_body

    def _extract_unsubscribe_from_body(self, html: str) -> Optional[str]:
        """Find unsubscribe link in HTML body."""
        soup = BeautifulSoup(html, 'lxml')

        for link in soup.find_all('a', href=True):
            href = link.get('href', '')
            text = link.get_text().lower()

            if any(word in text for word in ['unsubscribe', 'opt out', 'opt-out']):
                if href.startswith('http'):
                    return href

            if 'unsubscribe' in href.lower() and href.startswith('http'):
                return href

        return None
