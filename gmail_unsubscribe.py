#!/usr/bin/env python3
"""
Gmail Unsubscribe Tool

Extracts unsubscribe links from Gmail Promotions emails and optionally
auto-unsubscribes using Selenium.

Setup:
1. Create a Google Cloud project at https://console.cloud.google.com/
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app)
4. Download credentials.json to this folder
5. Run: pip install -r requirements.txt
6. Run: python gmail_unsubscribe.py
"""

import os
import re
import csv
import sys
import base64
import email
import time
import argparse
from pathlib import Path

# Fix Windows console encoding for Unicode
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
from typing import Optional
from dataclasses import dataclass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from bs4 import BeautifulSoup

# Gmail API scope - read-only for safety
SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']

# Project directory
PROJECT_DIR = Path(__file__).parent
CREDENTIALS_FILE = PROJECT_DIR / 'credentials.json'
TOKEN_FILE = PROJECT_DIR / 'token.json'
OUTPUT_CSV = PROJECT_DIR / 'unsubscribe_links.csv'


@dataclass
class UnsubscribeLink:
    """Represents an unsubscribe link with company info."""
    company_name: str
    unsubscribe_url: str
    source: str  # 'header' or 'body'


def authenticate_gmail():
    """
    Authenticate with Gmail API using OAuth 2.0.

    First run will open a browser for consent.
    Subsequent runs use cached token.
    """
    creds = None

    # Load existing token if available
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    # Refresh or get new credentials if needed
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refreshing access token...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(f"ERROR: {CREDENTIALS_FILE} not found!")
                print("\nSetup instructions:")
                print("1. Go to https://console.cloud.google.com/")
                print("2. Create a project and enable Gmail API")
                print("3. Create OAuth 2.0 credentials (Desktop app)")
                print("4. Download and save as 'credentials.json' in this folder")
                return None

            print("Opening browser for authentication...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save token for future runs
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
        print("Authentication successful! Token saved.")

    return build('gmail', 'v1', credentials=creds)


def get_promotions_emails(service, max_results: int = 500):
    """
    Fetch emails from the Promotions category.

    Args:
        service: Gmail API service object
        max_results: Maximum number of emails to fetch

    Yields:
        Message objects with full content
    """
    print(f"Searching for Promotions emails (max {max_results})...")

    messages = []
    page_token = None

    while len(messages) < max_results:
        # Search for emails in Promotions category
        results = service.users().messages().list(
            userId='me',
            q='category:promotions',
            maxResults=min(100, max_results - len(messages)),
            pageToken=page_token
        ).execute()

        batch = results.get('messages', [])
        if not batch:
            break

        messages.extend(batch)
        print(f"  Found {len(messages)} emails so far...")

        page_token = results.get('nextPageToken')
        if not page_token:
            break

    print(f"Fetching full content for {len(messages)} emails...")

    for i, msg in enumerate(messages, 1):
        if i % 50 == 0:
            print(f"  Processing {i}/{len(messages)}...")

        # Get full message content
        full_msg = service.users().messages().get(
            userId='me',
            id=msg['id'],
            format='full'
        ).execute()

        yield full_msg


def extract_from_header(sender: str) -> str:
    """Extract company name from email sender."""
    # Format: "Company Name <email@example.com>" or just "email@example.com"
    match = re.match(r'^"?([^"<]+)"?\s*<', sender)
    if match:
        return match.group(1).strip()

    # Try to extract from email domain
    match = re.search(r'@([^.]+)', sender)
    if match:
        return match.group(1).capitalize()

    return sender


def get_header_value(headers: list, name: str) -> Optional[str]:
    """Get a header value by name."""
    for header in headers:
        if header['name'].lower() == name.lower():
            return header['value']
    return None


def extract_unsubscribe_from_header(headers: list) -> Optional[str]:
    """
    Extract unsubscribe URL from List-Unsubscribe header.

    This is the most reliable source for unsubscribe links.
    Format: <mailto:unsub@example.com>, <https://example.com/unsub>
    """
    unsub_header = get_header_value(headers, 'List-Unsubscribe')
    if not unsub_header:
        return None

    # Find HTTP(S) URLs in the header
    urls = re.findall(r'<(https?://[^>]+)>', unsub_header)
    if urls:
        return urls[0]

    return None


def decode_body(payload: dict) -> str:
    """Decode email body from base64."""
    body = ""

    if 'body' in payload and payload['body'].get('data'):
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')

    if 'parts' in payload:
        for part in payload['parts']:
            mime_type = part.get('mimeType', '')
            if mime_type == 'text/html':
                if part.get('body', {}).get('data'):
                    body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8', errors='ignore')
                    break
            elif 'parts' in part:
                # Nested multipart
                body = decode_body(part)
                if body:
                    break

    return body


def extract_unsubscribe_from_body(html_body: str) -> Optional[str]:
    """
    Extract unsubscribe URL from email HTML body.

    Looks for links containing 'unsubscribe' in text or URL.
    """
    if not html_body:
        return None

    soup = BeautifulSoup(html_body, 'lxml')

    # Find all links
    for link in soup.find_all('a', href=True):
        href = link.get('href', '')
        text = link.get_text().lower()

        # Check if link text or URL contains unsubscribe indicators
        if any(word in text for word in ['unsubscribe', 'opt out', 'opt-out', 'remove']):
            if href.startswith('http'):
                return href

        if 'unsubscribe' in href.lower():
            if href.startswith('http'):
                return href

    return None


def extract_unsubscribe_links(service, max_emails: int = 500) -> list[UnsubscribeLink]:
    """
    Extract unsubscribe links from Promotions emails.

    Returns:
        List of UnsubscribeLink objects
    """
    links = []
    seen_urls = set()

    for msg in get_promotions_emails(service, max_emails):
        headers = msg.get('payload', {}).get('headers', [])

        # Get sender info
        sender = get_header_value(headers, 'From') or 'Unknown'
        company = extract_from_header(sender)

        # Try to get unsubscribe URL from header first (most reliable)
        unsub_url = extract_unsubscribe_from_header(headers)
        source = 'header'

        # Fall back to parsing HTML body
        if not unsub_url:
            html_body = decode_body(msg.get('payload', {}))
            unsub_url = extract_unsubscribe_from_body(html_body)
            source = 'body'

        if unsub_url and unsub_url not in seen_urls:
            seen_urls.add(unsub_url)
            links.append(UnsubscribeLink(
                company_name=company,
                unsubscribe_url=unsub_url,
                source=source
            ))

    return links


def save_to_csv(links: list[UnsubscribeLink], output_file: Path = OUTPUT_CSV):
    """Save unsubscribe links to CSV file."""
    print(f"\nSaving {len(links)} unique unsubscribe links to {output_file}...")

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Company Name', 'Unsubscribe URL', 'Source'])

        for link in sorted(links, key=lambda x: x.company_name.lower()):
            writer.writerow([link.company_name, link.unsubscribe_url, link.source])

    print(f"Saved to {output_file}")


def auto_unsubscribe(links: list[UnsubscribeLink], headless: bool = False):
    """
    Automatically visit unsubscribe links and attempt to click unsubscribe buttons.

    Args:
        links: List of UnsubscribeLink objects
        headless: Run browser without visible window
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from webdriver_manager.chrome import ChromeDriverManager
    except ImportError:
        print("ERROR: Selenium not installed. Run: pip install selenium webdriver-manager")
        return

    print(f"\n{'='*60}")
    print("AUTO-UNSUBSCRIBE MODE")
    print(f"{'='*60}")
    print(f"Will attempt to unsubscribe from {len(links)} services")
    print("This will open Chrome and visit each unsubscribe link.")
    print("\nPress Ctrl+C at any time to stop.\n")

    # Setup Chrome
    options = Options()
    if headless:
        options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.implicitly_wait(5)

    results = {'success': 0, 'failed': 0, 'skipped': 0}

    # Common unsubscribe button patterns
    button_patterns = [
        "//button[contains(translate(., 'UNSUBSCRIBE', 'unsubscribe'), 'unsubscribe')]",
        "//input[@type='submit'][contains(translate(@value, 'UNSUBSCRIBE', 'unsubscribe'), 'unsubscribe')]",
        "//a[contains(translate(., 'UNSUBSCRIBE', 'unsubscribe'), 'unsubscribe')]",
        "//button[contains(translate(., 'CONFIRM', 'confirm'), 'confirm')]",
        "//input[@type='submit'][contains(translate(@value, 'CONFIRM', 'confirm'), 'confirm')]",
        "//button[contains(translate(., 'OPT OUT', 'opt out'), 'opt out')]",
        "//button[contains(translate(., 'REMOVE', 'remove'), 'remove')]",
    ]

    try:
        for i, link in enumerate(links, 1):
            print(f"\n[{i}/{len(links)}] {link.company_name}")
            print(f"  URL: {link.unsubscribe_url[:70]}...")

            try:
                driver.get(link.unsubscribe_url)
                time.sleep(2)  # Wait for page load

                # Try to find and click unsubscribe button
                clicked = False
                for pattern in button_patterns:
                    try:
                        elements = driver.find_elements(By.XPATH, pattern)
                        for elem in elements:
                            if elem.is_displayed() and elem.is_enabled():
                                elem.click()
                                clicked = True
                                print(f"  ✓ Clicked unsubscribe button")
                                results['success'] += 1
                                time.sleep(2)
                                break
                    except Exception:
                        continue
                    if clicked:
                        break

                if not clicked:
                    print(f"  ? Page loaded - manual action may be needed")
                    results['skipped'] += 1

            except TimeoutException:
                print(f"  ✗ Timeout loading page")
                results['failed'] += 1
            except WebDriverException as e:
                print(f"  ✗ Error: {str(e)[:50]}")
                results['failed'] += 1
            except Exception as e:
                print(f"  ✗ Unexpected error: {str(e)[:50]}")
                results['failed'] += 1

            # Small delay between requests
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nStopped by user.")
    finally:
        driver.quit()

    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")
    print(f"Successfully clicked: {results['success']}")
    print(f"Loaded (manual needed): {results['skipped']}")
    print(f"Failed to load: {results['failed']}")


def main():
    parser = argparse.ArgumentParser(
        description='Extract unsubscribe links from Gmail Promotions'
    )
    parser.add_argument(
        '--max-emails', '-m',
        type=int,
        default=500,
        help='Maximum number of emails to process (default: 500)'
    )
    parser.add_argument(
        '--auto-unsubscribe', '-a',
        action='store_true',
        help='Automatically visit unsubscribe links with Selenium'
    )
    parser.add_argument(
        '--headless',
        action='store_true',
        help='Run browser in headless mode (no visible window)'
    )
    parser.add_argument(
        '--output', '-o',
        type=str,
        default=str(OUTPUT_CSV),
        help=f'Output CSV file (default: {OUTPUT_CSV})'
    )
    parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Skip confirmation prompt for auto-unsubscribe'
    )

    args = parser.parse_args()

    print("="*60)
    print("GMAIL UNSUBSCRIBE TOOL")
    print("="*60)

    # Authenticate
    print("\n[1/3] Authenticating with Gmail...")
    service = authenticate_gmail()
    if not service:
        return 1

    # Extract links
    print("\n[2/3] Extracting unsubscribe links...")
    links = extract_unsubscribe_links(service, args.max_emails)

    if not links:
        print("\nNo unsubscribe links found!")
        return 0

    print(f"\nFound {len(links)} unique unsubscribe links:")
    print(f"  - From headers: {sum(1 for l in links if l.source == 'header')}")
    print(f"  - From body: {sum(1 for l in links if l.source == 'body')}")

    # Save to CSV
    print("\n[3/3] Saving results...")
    save_to_csv(links, Path(args.output))

    # Auto-unsubscribe if requested
    if args.auto_unsubscribe:
        if args.yes:
            auto_unsubscribe(links, args.headless)
        else:
            response = input("\nProceed with auto-unsubscribe? (y/N): ")
            if response.lower() == 'y':
                auto_unsubscribe(links, args.headless)

    print("\nDone!")
    return 0


if __name__ == '__main__':
    exit(main())
