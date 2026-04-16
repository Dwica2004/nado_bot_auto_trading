import asyncio
from nado_api import NadoRestClient

async def t():
    r = NadoRestClient()
    d = await r.query({"type": "all_products"})
    perps = d.get("perp_products", [])
    print("Nado PERP products:")
    for p in perps[:20]:
        pid   = p.get("product_id")
        inner = p.get("product", {})
        book  = inner.get("book_info", {})
        mark  = float(book.get("mark_price_x18", 0)) / 1e18
        sym   = book.get("symbol", "?")
        print(f"  id={pid:3d}  sym={sym:<20}  mark={mark:.4f}")
    await r.close()

asyncio.run(t())
