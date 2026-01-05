"""
SQLite Database for Email Caching

Caches email metadata for fast browsing without hitting Gmail API.
"""

import sqlite3
import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import asdict

from gmail_client import Email

PROJECT_DIR = Path(__file__).parent
DB_FILE = PROJECT_DIR / 'emails.db'


def init_db():
    """Initialize database schema (sync version for startup)."""
    conn = sqlite3.connect(str(DB_FILE))
    conn.execute('''
        CREATE TABLE IF NOT EXISTS emails (
            id TEXT PRIMARY KEY,
            thread_id TEXT,
            subject TEXT,
            sender TEXT,
            sender_email TEXT,
            date TEXT,
            snippet TEXT,
            labels TEXT,
            category TEXT,
            unsubscribe_url TEXT,
            unsubscribe_post INTEGER DEFAULT 0,
            is_read INTEGER,
            synced_at TEXT
        )
    ''')
    # Add columns if upgrading from old schema
    try:
        conn.execute('ALTER TABLE emails ADD COLUMN unsubscribe_post INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        conn.execute('ALTER TABLE emails ADD COLUMN unsubscribe_mailto TEXT')
    except sqlite3.OperationalError:
        pass  # Column already exists
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_sender_email ON emails(sender_email)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_category ON emails(category)
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_date ON emails(date DESC)
    ''')
    conn.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
            id, subject, sender, snippet,
            content=emails,
            content_rowid=rowid
        )
    ''')
    conn.commit()
    conn.close()


class EmailDatabase:
    """Async database interface for email caching."""

    def __init__(self):
        self.db_path = str(DB_FILE)

    async def save_email(self, email: Email):
        """Save or update an email in the cache."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                INSERT OR REPLACE INTO emails
                (id, thread_id, subject, sender, sender_email, date, snippet,
                 labels, category, unsubscribe_url, unsubscribe_mailto, unsubscribe_post, is_read, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                email.id,
                email.thread_id,
                email.subject,
                email.sender,
                email.sender_email,
                email.date.isoformat(),
                email.snippet,
                ','.join(email.labels),
                email.category,
                email.unsubscribe_url,
                email.unsubscribe_mailto,
                1 if email.unsubscribe_post else 0,
                1 if email.is_read else 0,
                datetime.now().isoformat()
            ))
            await db.commit()

    async def save_emails(self, emails: list[Email]):
        """Bulk save emails."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany('''
                INSERT OR REPLACE INTO emails
                (id, thread_id, subject, sender, sender_email, date, snippet,
                 labels, category, unsubscribe_url, unsubscribe_mailto, unsubscribe_post, is_read, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', [
                (
                    e.id, e.thread_id, e.subject, e.sender, e.sender_email,
                    e.date.isoformat(), e.snippet, ','.join(e.labels),
                    e.category, e.unsubscribe_url, e.unsubscribe_mailto,
                    1 if e.unsubscribe_post else 0,
                    1 if e.is_read else 0, datetime.now().isoformat()
                )
                for e in emails
            ])
            await db.commit()

    async def get_emails(
        self,
        category: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
        search: Optional[str] = None
    ) -> list[dict]:
        """Get emails from cache with optional filtering."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if search:
                # Full-text search
                query = '''
                    SELECT e.* FROM emails e
                    JOIN emails_fts fts ON e.id = fts.id
                    WHERE emails_fts MATCH ?
                '''
                params = [search]
            else:
                query = 'SELECT * FROM emails WHERE 1=1'
                params = []

            if category and category != 'all':
                query += ' AND category = ?'
                params.append(category)

            query += ' ORDER BY date DESC LIMIT ? OFFSET ?'
            params.extend([limit, offset])

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            return [dict(row) for row in rows]

    async def get_email(self, email_id: str) -> Optional[dict]:
        """Get single email by ID."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT * FROM emails WHERE id = ?',
                (email_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_senders(self, category: Optional[str] = None) -> list[dict]:
        """Get unique senders with email counts."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            query = '''
                SELECT
                    sender,
                    sender_email,
                    COUNT(*) as email_count,
                    MAX(unsubscribe_url) as unsubscribe_url,
                    MAX(unsubscribe_mailto) as unsubscribe_mailto,
                    MAX(unsubscribe_post) as unsubscribe_post,
                    MAX(date) as last_email
                FROM emails
            '''
            params = []

            if category and category != 'all':
                query += ' WHERE category = ?'
                params.append(category)

            query += ' GROUP BY sender_email ORDER BY email_count DESC'

            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()

            return [dict(row) for row in rows]

    async def delete_emails(self, email_ids: list[str]):
        """Remove emails from cache."""
        async with aiosqlite.connect(self.db_path) as db:
            placeholders = ','.join('?' * len(email_ids))
            await db.execute(
                f'DELETE FROM emails WHERE id IN ({placeholders})',
                email_ids
            )
            await db.commit()

    async def get_count(self, category: Optional[str] = None) -> int:
        """Get total email count."""
        async with aiosqlite.connect(self.db_path) as db:
            if category and category != 'all':
                cursor = await db.execute(
                    'SELECT COUNT(*) FROM emails WHERE category = ?',
                    (category,)
                )
            else:
                cursor = await db.execute('SELECT COUNT(*) FROM emails')

            row = await cursor.fetchone()
            return row[0] if row else 0

    async def rebuild_fts(self):
        """Rebuild full-text search index."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO emails_fts(emails_fts) VALUES('rebuild')")
            await db.commit()
