import asyncio
from client import NadoRestClient

async def main():
    rest = NadoRestClient()
    res = await rest.get_all_products()
    perps = res.get("perp_products", [])
    print(f"Total perps: {len(perps)}")
    
    symbols = await rest.query({"type": "symbols"})
    print(f"Total symbols: {len(symbols.get('symbols', {}).keys())}")
    
    # Let's map product_id -> symbol
    symbol_map = {}
    for key, val in symbols.get("symbols", {}).items():
        symbol_map[val.get("product_id")] = val.get("symbol")
        
    for p in perps[:10]:
        pid = p.get('product_id')
        print(f"ID: {pid} | Symbol: {symbol_map.get(pid, 'UNKNOWN')}")

    await rest.close()

if __name__ == "__main__":
    asyncio.run(main())
