"""
Job Management for Unsubscribe Operations

Handles job lifecycle: create, start, update, complete.
Persists state to SQLite for resilience across restarts.
"""

import os
import uuid
import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict

PROJECT_DIR = Path(__file__).parent
DB_FILE = Path(os.getenv("DB_PATH", str(PROJECT_DIR / 'emails.db')))


@dataclass
class JobItem:
    """Represents a single unsubscribe item within a job."""
    id: Optional[int] = None
    job_id: str = ""
    sender: str = ""
    sender_email: str = ""
    unsubscribe_url: Optional[str] = None
    unsubscribe_mailto: Optional[str] = None
    method_attempted: Optional[str] = None
    status: str = "pending"  # pending, running, success, failed
    error_message: Optional[str] = None
    attempted_at: Optional[str] = None
    retry_count: int = 0


@dataclass
class Job:
    """Represents an unsubscribe job."""
    id: str = ""
    status: str = "pending"  # pending, running, completed, failed
    created_at: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    total_items: int = 0
    completed_items: int = 0
    successful_items: int = 0
    failed_items: int = 0
    items: list = field(default_factory=list)


class JobManager:
    """Async job management interface."""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_FILE)

    async def create_job(self, items: list[dict]) -> Job:
        """Create a new job with items."""
        job_id = str(uuid.uuid4())
        now = datetime.now().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            # Insert job
            await db.execute('''
                INSERT INTO jobs (id, status, created_at, total_items)
                VALUES (?, ?, ?, ?)
            ''', (job_id, 'pending', now, len(items)))

            # Insert job items
            for item in items:
                await db.execute('''
                    INSERT INTO job_items
                    (job_id, sender, sender_email, unsubscribe_url, unsubscribe_mailto, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    job_id,
                    item.get('sender', ''),
                    item.get('sender_email', ''),
                    item.get('url'),
                    item.get('mailto'),
                    'pending'
                ))

            await db.commit()

        return await self.get_job(job_id)

    async def get_job(self, job_id: str) -> Optional[Job]:
        """Get a job with all its items."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Get job
            cursor = await db.execute(
                'SELECT * FROM jobs WHERE id = ?', (job_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None

            job = Job(
                id=row['id'],
                status=row['status'],
                created_at=row['created_at'],
                started_at=row['started_at'],
                completed_at=row['completed_at'],
                total_items=row['total_items'],
                completed_items=row['completed_items'],
                successful_items=row['successful_items'],
                failed_items=row['failed_items']
            )

            # Get items
            cursor = await db.execute(
                'SELECT * FROM job_items WHERE job_id = ? ORDER BY id',
                (job_id,)
            )
            rows = await cursor.fetchall()
            job.items = [
                JobItem(
                    id=r['id'],
                    job_id=r['job_id'],
                    sender=r['sender'],
                    sender_email=r['sender_email'],
                    unsubscribe_url=r['unsubscribe_url'],
                    unsubscribe_mailto=r['unsubscribe_mailto'],
                    method_attempted=r['method_attempted'],
                    status=r['status'],
                    error_message=r['error_message'],
                    attempted_at=r['attempted_at'],
                    retry_count=r['retry_count']
                )
                for r in rows
            ]

            return job

    async def list_jobs(self, limit: int = 20, offset: int = 0) -> list[Job]:
        """List jobs ordered by creation date (newest first)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute('''
                SELECT * FROM jobs
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            ''', (limit, offset))
            rows = await cursor.fetchall()

            jobs = []
            for row in rows:
                jobs.append(Job(
                    id=row['id'],
                    status=row['status'],
                    created_at=row['created_at'],
                    started_at=row['started_at'],
                    completed_at=row['completed_at'],
                    total_items=row['total_items'],
                    completed_items=row['completed_items'],
                    successful_items=row['successful_items'],
                    failed_items=row['failed_items']
                ))

            return jobs

    async def start_job(self, job_id: str) -> None:
        """Mark job as running."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE jobs SET status = 'running', started_at = ?
                WHERE id = ?
            ''', (now, job_id))
            await db.commit()

    async def update_item(
        self,
        item_id: int,
        status: str,
        method: str,
        error_message: Optional[str] = None
    ) -> None:
        """Update a job item's status."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE job_items
                SET status = ?, method_attempted = ?, error_message = ?, attempted_at = ?
                WHERE id = ?
            ''', (status, method, error_message, now, item_id))

            # Get job_id to update counters
            cursor = await db.execute(
                'SELECT job_id FROM job_items WHERE id = ?', (item_id,)
            )
            row = await cursor.fetchone()
            if row:
                job_id = row[0]

                # Update job counters
                if status == 'success':
                    await db.execute('''
                        UPDATE jobs
                        SET completed_items = completed_items + 1,
                            successful_items = successful_items + 1
                        WHERE id = ?
                    ''', (job_id,))
                elif status == 'failed':
                    await db.execute('''
                        UPDATE jobs
                        SET completed_items = completed_items + 1,
                            failed_items = failed_items + 1
                        WHERE id = ?
                    ''', (job_id,))

            await db.commit()

    async def complete_job(self, job_id: str, status: str = 'completed') -> None:
        """Mark job as completed or failed."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                UPDATE jobs SET status = ?, completed_at = ?
                WHERE id = ?
            ''', (status, now, job_id))
            await db.commit()

    async def get_pending_items(self, job_id: str) -> list[JobItem]:
        """Get all pending items for a job."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('''
                SELECT * FROM job_items
                WHERE job_id = ? AND status = 'pending'
                ORDER BY id
            ''', (job_id,))
            rows = await cursor.fetchall()

            return [
                JobItem(
                    id=r['id'],
                    job_id=r['job_id'],
                    sender=r['sender'],
                    sender_email=r['sender_email'],
                    unsubscribe_url=r['unsubscribe_url'],
                    unsubscribe_mailto=r['unsubscribe_mailto'],
                    method_attempted=r['method_attempted'],
                    status=r['status'],
                    error_message=r['error_message'],
                    attempted_at=r['attempted_at'],
                    retry_count=r['retry_count']
                )
                for r in rows
            ]

    async def get_failed_items(self, job_id: str) -> list[JobItem]:
        """Get all failed items for a job (for retry)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('''
                SELECT * FROM job_items
                WHERE job_id = ? AND status = 'failed'
                ORDER BY id
            ''', (job_id,))
            rows = await cursor.fetchall()

            return [
                JobItem(
                    id=r['id'],
                    job_id=r['job_id'],
                    sender=r['sender'],
                    sender_email=r['sender_email'],
                    unsubscribe_url=r['unsubscribe_url'],
                    unsubscribe_mailto=r['unsubscribe_mailto'],
                    method_attempted=r['method_attempted'],
                    status=r['status'],
                    error_message=r['error_message'],
                    attempted_at=r['attempted_at'],
                    retry_count=r['retry_count']
                )
                for r in rows
            ]

    async def reset_failed_items(self, job_id: str) -> int:
        """Reset failed items to pending for retry. Returns count."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('''
                UPDATE job_items
                SET status = 'pending', retry_count = retry_count + 1
                WHERE job_id = ? AND status = 'failed'
            ''', (job_id,))
            count = cursor.rowcount

            # Reset job counters for retry
            await db.execute('''
                UPDATE jobs
                SET status = 'pending',
                    completed_items = completed_items - ?,
                    failed_items = 0,
                    completed_at = NULL
                WHERE id = ?
            ''', (count, job_id))

            await db.commit()
            return count

    async def get_active_job(self) -> Optional[Job]:
        """Get the currently running job, if any."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM jobs WHERE status = 'running' LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                return await self.get_job(row['id'])
            return None

    async def get_job_status(self, job_id: str) -> dict:
        """Get job status in the format expected by the frontend."""
        job = await self.get_job(job_id)
        if not job:
            return {"running": False, "progress": 0, "total": 0, "results": []}

        results = [
            {
                "sender": item.sender,
                "success": item.status == "success",
                "method": item.method_attempted or "pending",
                "message": item.error_message or ("Done" if item.status == "success" else "")
            }
            for item in job.items
            if item.status in ("success", "failed")
        ]

        return {
            "running": job.status == "running",
            "progress": job.completed_items,
            "total": job.total_items,
            "results": results,
            "job_id": job.id
        }
