import asyncio
from client import NadoRestClient

async def main():
    rest = NadoRestClient()
    try:
        res = await rest.get_all_products()
        perps = res.get("perp_products", [])
        
        symbols_res = await rest.query({"type": "symbols"})
        symbol_map = {}
        for key, val in symbols_res.get("symbols", {}).items():
            symbol_map[val.get("product_id")] = val.get("symbol")
            
        print(f"{'SYMBOL':<15} | {'MAX LEV':<7} | {'PRODUCT_ID'}")
        print("-" * 35)
        
        for p in perps:
            pid = p.get('product_id')
            sym = symbol_map.get(pid, f"PID_{pid}")
            
            risk = p.get("risk", {})
            lw = float(risk.get("long_weight_initial_x18", 0)) / 1e18
            
            # Max Leverage = 1 / (1 - weight)
            if lw < 1:
                max_lev = round(1 / (1 - lw))
            else:
                max_lev = 1
                
            print(f"{sym:<15} | {max_lev:<7}x | {pid}")
            
    finally:
        await rest.close()

if __name__ == "__main__":
    asyncio.run(main())
