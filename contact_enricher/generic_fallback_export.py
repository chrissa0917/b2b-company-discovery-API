from __future__ import annotations

import asyncio
import json

from .generic_fallback_fast import load_rows, collect, write, serve
from .reoon_verifier import check_reoon_balance


async def main():
    rows = load_rows()
    generics = await collect(rows)
    for row_num, row in enumerate(rows, start=1):
        generic = generics.get(row_num, {})
        if generic.get("email"):
            print("GENERIC_FALLBACK_ROW " + json.dumps({
                "benchmark_row": row_num,
                "company": row.get("Company", ""),
                "website": row.get("Website URL", ""),
                "email": generic.get("email", ""),
                "source": generic.get("source", ""),
            }, ensure_ascii=False), flush=True)
    balance = await check_reoon_balance()
    write(rows, generics, balance)


if __name__ == "__main__":
    asyncio.run(main())
    serve()
