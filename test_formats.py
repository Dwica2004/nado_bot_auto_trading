import asyncio
import websockets

async def test_formats():
    url = "wss://gateway.prod.nado.xyz/v1/ws"
    
    msgs = [
        '{"method": "subscribe", "stream": {"type": "ticker", "product_id": 1}}',
        '{"method": "subscribe", "stream": {"type": "market", "product_id": 1}}',
        '{"type": "subscribe", "channel": "ticker", "product_id": 1}',
        '{"type": "subscribe", "channel": "bbo", "product_id": 1}',
        '{"method": "subscribe", "topic": "best_bid_offer", "product_id": 1}',
    ]
    
    for msg in msgs:
        print(f"\nSending: {msg}")
        try:
            async with websockets.connect(url, ping_interval=None) as ws:
                await ws.send(msg)
                res = await asyncio.wait_for(ws.recv(), timeout=2)
                print(f"Res: {res}")
        except Exception as e:
            print(f"Error: {e}")

asyncio.run(test_formats())
