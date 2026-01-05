# Gmail Unsubscribe Manager

A lightweight web-based email client focused on managing promotional emails and bulk unsubscribing.

## Features

### Email Inbox
- View emails with search and filtering
- Filter by category (Primary, Promotions, Social, etc.)
- Read full email content in-browser
- Delete and archive emails

### Unsubscribe Manager
- View all senders from your Promotions folder
- See email count per sender
- Bulk select senders to unsubscribe from
- Real-time progress tracking

### Smart Unsubscribe System
The tool uses multiple strategies to maximize unsubscribe success:

1. **One-Click Unsubscribe (RFC 8058)** - Instant HTTP POST for compliant senders
2. **Retry with Exponential Backoff** - Retries failed requests up to 2x
3. **mailto: Fallback** - Sends unsubscribe email via Gmail API when HTTP fails
4. **Browser Automation** - Selenium clicks unsubscribe buttons on web pages
5. **Manual Queue** - "Open Failed in Browser" button for remaining items

### Performance
- **Parallel Processing** - 5 concurrent HTTP requests, 3 browser instances
- **SQLite Caching** - Fast browsing without hitting Gmail API
- **Full-Text Search** - Search emails by subject, sender, or content

## Tech Stack

- **Backend**: FastAPI (Python)
- **Frontend**: HTML + Tailwind CSS + Alpine.js
- **Database**: SQLite with async support (aiosqlite)
- **Gmail API**: OAuth 2.0 authentication
- **Browser Automation**: Selenium + Chrome

## Setup

### 1. Google Cloud Console
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project
3. Enable the Gmail API
4. Create OAuth 2.0 credentials (Desktop application)
5. Download `credentials.json` to the project folder
6. Add your email as a test user in OAuth consent screen

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Run the Server
```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

### 4. First Run
1. Open http://127.0.0.1:8000
2. Complete OAuth flow in browser (first time only)
3. Click "Sync" to fetch emails from Gmail
4. Go to "Unsubscribe" to manage subscriptions

## Project Structure

```
gmail-unsubscribe/
├── app.py              # FastAPI application & routes
├── gmail_client.py     # Gmail API wrapper & unsubscribe functions
├── database.py         # SQLite cache for email metadata
├── requirements.txt    # Python dependencies
├── templates/
│   ├── base.html       # Base template with navigation
│   ├── inbox.html      # Email list view
│   ├── email.html      # Single email view
│   └── unsubscribe.html # Unsubscribe manager
├── credentials.json    # (you provide) Google OAuth credentials
└── token.json          # (auto-generated) OAuth token
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/inbox` | Email inbox with pagination |
| GET | `/email/{id}` | View single email |
| POST | `/email/{id}/delete` | Delete email |
| POST | `/email/{id}/archive` | Archive email |
| GET | `/unsubscribe` | Unsubscribe manager |
| POST | `/unsubscribe` | Process bulk unsubscribe |
| GET | `/api/sync` | Sync emails from Gmail |
| GET | `/api/unsub-status` | Get unsubscribe progress |
| GET | `/api/failed-urls` | Get failed unsubscribe URLs |

## Security Notes

- `credentials.json` and `token.json` are in `.gitignore`
- Database file (`emails.db`) is excluded from git
- OAuth tokens are stored locally only
- No data is sent to external servers (except Gmail API)

## License

MIT
