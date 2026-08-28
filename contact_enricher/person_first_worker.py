from __future__ import annotations

import asyncio
import json
import sys

from .ddgs_people import enrich_person_ddgs

POSITIONS = [
    "Marketing",
    "PR & Communications",
    "Partnerships & Business Development",
    "Leadership / Founder",
]


async def main() -> None:
    company = sys.argv[1]
    website = sys.argv[2]
    result = await enrich_person_ddgs(company, website, POSITIONS)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
