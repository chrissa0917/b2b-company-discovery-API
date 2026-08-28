from __future__ import annotations

import asyncio
import json
import sys
import time

from .open_source_email_scraper import scrape_public_contact_data


async def main() -> None:
    website = sys.argv[1]
    started = time.perf_counter()
    result = await scrape_public_contact_data(
        website,
        timeout_seconds=12,
        depth=1,
        max_links_from_page=6,
        browser_fallback=False,
    )
    print(json.dumps({
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "emails": result.emails,
        "linkedin_urls": result.linkedin_urls,
        "pages_checked": result.pages_checked,
        "rejected_external_emails": result.rejected_external_emails or [],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
