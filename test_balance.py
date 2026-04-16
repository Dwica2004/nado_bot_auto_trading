import asyncio
from nado_api import NadoRestClient, build_sender_hex, WALLET_ADDRESS, SUBACCOUNT, GATEWAY

print("GATEWAY:", GATEWAY)

async def test():
    rest = NadoRestClient()
    sender = build_sender_hex(WALLET_ADDRESS, SUBACCOUNT)
    print("sender:", sender)

    # Test max_withdrawable
    try:
        data = await rest.query({"type": "max_withdrawable", "sender": sender, "product_id": 0})
        print("max_withdrawable raw:", data)
    except Exception as e:
        print("max_withdrawable error:", e)

    # Test subaccount_info with v1 URL
    try:
        info = await rest.get_subaccount_info(sender)
        print("exists:", info.get("exists"))
        healths = info.get("healths", [])
        for i, h in enumerate(healths[:3]):
            print(f"  health[{i}]:", h)
    except Exception as e:
        print("subaccount_info error:", e)

    bal = await rest.get_account_equity(sender)
    print(f"Balance: ${bal:.4f}")

    await rest.close()

asyncio.run(test())
