import asyncio
import aiohttp

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get('https://gateway.prod.nado.xyz/v1/query?type=subaccount_info&subaccount=0xd41c88ee843dccbe6e8ad3d62e4c33ccb6c9787b000000000000000000000000') as r:
            j = await r.json()
            spots = j['data'].get('spot_balances', [])
            print("SPOT_BALANCES:", spots)

asyncio.run(main())
