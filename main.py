#!/usr/bin/env python3
# ABOUTME: Entry point for Nado scalping bot.
# ABOUTME: Run with: python main.py

import asyncio
import signal
import sys

from bot import NadoScalpingBot


async def main():
    bot = NadoScalpingBot()

    # Graceful shutdown on Ctrl+C or SIGTERM
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        
        def _shutdown(sig_name):
            print(f"\n[!] {sig_name} received — shutting down gracefully …")
            asyncio.create_task(bot.stop())
            
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _shutdown, sig.name)
            except NotImplementedError:
                pass

    try:
        await bot.start()
    except KeyboardInterrupt:
        print("\n[!] Ctrl+C received — shutting down gracefully …")
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
