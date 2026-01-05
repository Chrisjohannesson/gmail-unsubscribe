"""
Gmail Web Client - FastAPI Application

A lightweight web interface for managing Gmail emails.
"""

import asyncio
from pathlib import Path
from typing import Optional
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, Form, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gmail_client import GmailClient, one_click_unsubscribe, async_one_click_unsubscribe, send_unsubscribe_email
from database import EmailDatabase, init_db

# Parallel processing configuration
MAX_CONCURRENT_HTTP = 5      # Parallel one-click requests
MAX_BROWSER_WORKERS = 3      # Parallel Selenium instances

PROJECT_DIR = Path(__file__).parent

# Initialize database on startup
init_db()

# Global instances
gmail = GmailClient()
db = EmailDatabase()

# Sync state
sync_status = {"running": False, "progress": 0, "total": 0, "message": ""}

# Unsubscribe state
unsub_status = {
    "running": False,
    "progress": 0,
    "total": 0,
    "results": [],  # List of {sender, success, method, message}
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - authenticate on startup."""
    try:
        gmail.authenticate()
        print("Gmail authenticated successfully!")
    except Exception as e:
        print(f"Gmail auth failed: {e}")
        print("Please ensure credentials.json is present.")
    yield


app = FastAPI(title="Gmail Client", lifespan=lifespan)

# Templates
templates = Jinja2Templates(directory=str(PROJECT_DIR / "templates"))


# Custom template filters
def format_date(value):
    """Format date for display."""
    if isinstance(value, str):
        from datetime import datetime
        try:
            value = datetime.fromisoformat(value)
        except:
            return value
    return value.strftime("%b %d, %Y")


def format_datetime(value):
    """Format datetime for display."""
    if isinstance(value, str):
        from datetime import datetime
        try:
            value = datetime.fromisoformat(value)
        except:
            return value
    return value.strftime("%b %d, %Y at %I:%M %p")


templates.env.filters["format_date"] = format_date
templates.env.filters["format_datetime"] = format_datetime


# Routes

@app.get("/", response_class=HTMLResponse)
async def home():
    """Redirect to inbox."""
    return RedirectResponse(url="/inbox", status_code=302)


@app.get("/inbox", response_class=HTMLResponse)
async def inbox(
    request: Request,
    category: str = "all",
    page: int = 1,
    q: Optional[str] = None
):
    """Display email inbox."""
    limit = 50
    offset = (page - 1) * limit

    emails = await db.get_emails(
        category=category,
        limit=limit,
        offset=offset,
        search=q
    )

    total = await db.get_count(category=category if not q else None)
    total_pages = (total + limit - 1) // limit

    return templates.TemplateResponse("inbox.html", {
        "request": request,
        "emails": emails,
        "category": category,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "search": q,
        "sync_status": sync_status
    })


@app.get("/email/{email_id}", response_class=HTMLResponse)
async def view_email(request: Request, email_id: str):
    """View single email."""
    # Get from cache first
    email = await db.get_email(email_id)

    # Fetch full content from Gmail if needed
    full_email = gmail.get_email(email_id, include_body=True)

    if full_email:
        # Update cache
        await db.save_email(full_email)
        email = {
            "id": full_email.id,
            "subject": full_email.subject,
            "sender": full_email.sender,
            "sender_email": full_email.sender_email,
            "date": full_email.date.isoformat(),
            "body_html": full_email.body_html,
            "body_text": full_email.body_text,
            "unsubscribe_url": full_email.unsubscribe_url,
            "labels": full_email.labels,
            "category": full_email.category
        }

        # Mark as read
        gmail.mark_as_read([email_id])

    if not email:
        return RedirectResponse(url="/inbox", status_code=302)

    return templates.TemplateResponse("email.html", {
        "request": request,
        "email": email
    })


@app.post("/email/{email_id}/delete")
async def delete_email(email_id: str):
    """Delete an email."""
    gmail.delete_emails([email_id])
    await db.delete_emails([email_id])
    return RedirectResponse(url="/inbox", status_code=302)


@app.post("/email/{email_id}/archive")
async def archive_email(email_id: str):
    """Archive an email."""
    gmail.archive_emails([email_id])
    await db.delete_emails([email_id])
    return RedirectResponse(url="/inbox", status_code=302)


@app.post("/bulk-action")
async def bulk_action(
    action: str = Form(...),
    email_ids: list[str] = Form(default=[])
):
    """Handle bulk actions on selected emails."""
    if not email_ids:
        return RedirectResponse(url="/inbox", status_code=302)

    if action == "delete":
        gmail.delete_emails(email_ids)
        await db.delete_emails(email_ids)
    elif action == "archive":
        gmail.archive_emails(email_ids)
        await db.delete_emails(email_ids)

    return RedirectResponse(url="/inbox", status_code=302)


@app.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_manager(request: Request):
    """Show unsubscribe manager with sender list."""
    senders = await db.get_senders(category="promotions")

    return templates.TemplateResponse("unsubscribe.html", {
        "request": request,
        "senders": senders,
        "unsub_status": unsub_status
    })


@app.post("/unsubscribe")
async def process_unsubscribe(
    background_tasks: BackgroundTasks,
    sender_emails: list[str] = Form(default=[])
):
    """Start unsubscribe process for selected senders."""
    # Get unsubscribe URLs for selected senders
    senders = await db.get_senders(category="promotions")
    to_process = [
        {
            "sender": s["sender"],
            "url": s["unsubscribe_url"],
            "mailto": s.get("unsubscribe_mailto"),
            "one_click": bool(s.get("unsubscribe_post"))
        }
        for s in senders
        if s["sender_email"] in sender_emails and (s["unsubscribe_url"] or s.get("unsubscribe_mailto"))
    ]

    if to_process:
        background_tasks.add_task(run_mass_unsubscribe, to_process)

    return RedirectResponse(url="/unsubscribe?processing=true", status_code=302)


@app.get("/api/unsub-status")
async def get_unsub_status():
    """Get current unsubscribe status."""
    return unsub_status


@app.get("/api/failed-urls")
async def get_failed_urls():
    """Get URLs for failed unsubscribes to open manually."""
    # Get senders that failed
    failed_senders = [r["sender"] for r in unsub_status.get("results", []) if not r["success"]]
    if not failed_senders:
        return {"urls": []}

    # Look up their URLs from the database
    senders = await db.get_senders(category="promotions")
    urls = [
        {"sender": s["sender"], "url": s["unsubscribe_url"]}
        for s in senders
        if s["sender"] in failed_senders and s["unsubscribe_url"]
    ]
    return {"urls": urls}


def browser_unsubscribe_worker(item: dict) -> dict:
    """
    Worker function for browser-based unsubscribe.
    Each worker creates its own browser instance.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')

    driver = None
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(15)

        driver.get(item["url"])

        # Use WebDriverWait instead of time.sleep
        wait = WebDriverWait(driver, 3)

        # Try to click unsubscribe button
        clicked = False
        patterns = [
            "//button[contains(translate(., 'UNSUBSCRIBE', 'unsubscribe'), 'unsubscribe')]",
            "//input[@type='submit'][contains(translate(@value, 'UNSUBSCRIBE', 'unsubscribe'), 'unsubscribe')]",
            "//a[contains(translate(., 'UNSUBSCRIBE', 'unsubscribe'), 'unsubscribe')]",
            "//button[contains(translate(., 'CONFIRM', 'confirm'), 'confirm')]",
        ]

        for pattern in patterns:
            try:
                elements = driver.find_elements(By.XPATH, pattern)
                for elem in elements:
                    if elem.is_displayed() and elem.is_enabled():
                        elem.click()
                        clicked = True
                        break
            except:
                continue
            if clicked:
                break

        return {
            "sender": item["sender"],
            "success": clicked,
            "method": "browser",
            "message": "Clicked" if clicked else "Page loaded (manual may be needed)"
        }

    except Exception as e:
        return {
            "sender": item["sender"],
            "success": False,
            "method": "browser",
            "message": str(e)[:50]
        }
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass


async def run_mass_unsubscribe(items: list[dict]):
    """
    Run mass unsubscribe with PARALLEL processing.

    - One-click: Async HTTP requests (5 concurrent)
    - Browser: Thread pool with 3 concurrent browsers
    - Both phases run simultaneously
    """
    global unsub_status
    unsub_status = {
        "running": True,
        "progress": 0,
        "total": len(items),
        "results": []
    }

    # Separate one-click from browser-required
    one_click_items = [i for i in items if i["one_click"]]
    browser_items = [i for i in items if not i["one_click"]]

    # Semaphore to limit concurrent HTTP requests
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_HTTP)

    async def bounded_one_click(item: dict) -> dict:
        """Process one-click unsubscribe with concurrency limit and mailto fallback."""
        async with semaphore:
            success, message = await async_one_click_unsubscribe(item["url"])

            # If HTTP failed and we have mailto, try that as fallback
            if not success and item.get("mailto"):
                mailto_success, mailto_message = await send_unsubscribe_email(
                    item["mailto"], gmail.service
                )
                if mailto_success:
                    return {
                        "sender": item["sender"],
                        "success": True,
                        "method": "mailto",
                        "message": mailto_message
                    }
                # Both failed - return original HTTP error
                message = f"{message} (mailto also failed)"

            return {
                "sender": item["sender"],
                "success": success,
                "method": "one-click",
                "message": message
            }

    async def process_one_click_batch() -> list[dict]:
        """Process all one-click items in parallel."""
        if not one_click_items:
            return []
        tasks = [bounded_one_click(item) for item in one_click_items]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Handle any exceptions
        processed = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed.append({
                    "sender": one_click_items[i]["sender"],
                    "success": False,
                    "method": "one-click",
                    "message": str(result)[:50]
                })
            else:
                processed.append(result)
        return processed

    async def process_browser_batch() -> list[dict]:
        """Process all browser items in parallel using thread pool, with mailto fallback."""
        if not browser_items:
            return []
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=MAX_BROWSER_WORKERS) as executor:
            tasks = [
                loop.run_in_executor(executor, browser_unsubscribe_worker, item)
                for item in browser_items
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle results and try mailto fallback for failures
        processed = []
        for i, result in enumerate(results):
            item = browser_items[i]

            if isinstance(result, Exception):
                result = {
                    "sender": item["sender"],
                    "success": False,
                    "method": "browser",
                    "message": str(result)[:50]
                }

            # If browser failed and we have mailto, try that
            if not result["success"] and item.get("mailto"):
                mailto_success, mailto_message = await send_unsubscribe_email(
                    item["mailto"], gmail.service
                )
                if mailto_success:
                    result = {
                        "sender": item["sender"],
                        "success": True,
                        "method": "mailto",
                        "message": mailto_message
                    }

            processed.append(result)
        return processed

    try:
        # Run BOTH phases in parallel
        one_click_results, browser_results = await asyncio.gather(
            process_one_click_batch(),
            process_browser_batch()
        )

        # Combine results
        unsub_status["results"] = one_click_results + browser_results
        unsub_status["progress"] = unsub_status["total"]

    except Exception as e:
        print(f"Mass unsubscribe error: {e}")
        # Mark any unprocessed as failed
        unsub_status["results"].append({
            "sender": "Unknown",
            "success": False,
            "method": "error",
            "message": str(e)[:50]
        })

    finally:
        unsub_status["running"] = False
        unsub_status["progress"] = unsub_status["total"]


# Keep old sync_status reference for compatibility
async def run_unsubscribe(urls: list[dict]):
    """Legacy function - redirects to new implementation."""
    items = [{"sender": u["sender"], "url": u["url"], "one_click": False} for u in urls]
    await run_mass_unsubscribe(items)


@app.get("/api/sync")
async def sync_emails(background_tasks: BackgroundTasks, max_emails: int = 500):
    """Sync emails from Gmail to local cache."""
    background_tasks.add_task(do_sync, max_emails)
    return {"status": "started", "message": "Syncing emails..."}


async def do_sync(max_emails: int):
    """Background task to sync emails."""
    global sync_status
    sync_status = {
        "running": True,
        "progress": 0,
        "total": max_emails,
        "message": "Fetching emails from Gmail..."
    }

    try:
        page_token = None
        fetched = 0

        while fetched < max_emails:
            emails, page_token = gmail.get_emails(
                query='',
                max_results=min(100, max_emails - fetched),
                page_token=page_token
            )

            if not emails:
                break

            await db.save_emails(emails)
            fetched += len(emails)

            sync_status["progress"] = fetched
            sync_status["message"] = f"Synced {fetched} emails..."

            if not page_token:
                break

        # Rebuild FTS index
        sync_status["message"] = "Building search index..."
        await db.rebuild_fts()

    except Exception as e:
        sync_status["message"] = f"Sync error: {e}"
        print(f"Sync error: {e}")

    finally:
        sync_status = {
            "running": False,
            "progress": 0,
            "total": 0,
            "message": f"Synced {fetched} emails"
        }


@app.get("/api/sync-status")
async def get_sync_status():
    """Get current sync status."""
    return sync_status


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
