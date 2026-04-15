import asyncio
import websockets

async def test_ws(url):
    try:
        async with websockets.connect(url) as ws:
            print(f'{url} -> CONNECTED')
            await ws.send('{"method":"subscribe","stream":{"type":"best_bid_offer","product_id":1}}')
            res = await asyncio.wait_for(ws.recv(), timeout=2)
            print(f'Res: {res}')
    except Exception as e:
        print(f'{url} -> FAILED: {e}')

async def main():
    await test_ws('wss://gateway.prod.nado.xyz/v1/ws')
    await test_ws('wss://gateway.prod.nado.xyz/ws')
    await test_ws('wss://subscriptions.nado.xyz/ws')
    await test_ws('wss://subscriptions.prod.nado.xyz/ws')
    
asyncio.run(main())
