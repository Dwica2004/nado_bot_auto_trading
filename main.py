# -*- coding: utf-8 -*-
"""
Nado Auto Trader — Entry Point
Jalankan: python main.py

Bot akan:
1. Scan semua coin di Nado DEX
2. Entry berdasarkan RSI + EMA + Volume + News sentiment
3. Manage posisi dengan trailing stop, TP1/TP2, early exit
"""
import sys, os, asyncio
sys.stdout.reconfigure(encoding="utf-8")

LOCKFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot.lock")

def check_single_instance():
    """Pastikan hanya satu instance bot yang bisa jalan."""
    if os.path.exists(LOCKFILE):
        with open(LOCKFILE, "r") as f:
            old_pid = f.read().strip()
        print(f"[ERROR] Bot sudah jalan! PID={old_pid}")
        print(f"  Kalau tidak jalan, hapus file: {LOCKFILE}")
        print("  Atau jalankan: del .bot.lock")
        sys.exit(1)

    # Tulis PID kita
    with open(LOCKFILE, "w") as f:
        f.write(str(os.getpid()))

def cleanup_lock():
    if os.path.exists(LOCKFILE):
        os.remove(LOCKFILE)

from bot_scanner import main

if __name__ == "__main__":
    check_single_instance()
    print("=" * 60)
    print("  🤖 Nado Auto Trader — STARTED")
    print(f"  PID: {os.getpid()}")
    print("  Stop: Ctrl+C")
    print("=" * 60)
    try:
        asyncio.run(main())
    finally:
        cleanup_lock()
        print("Bot stopped. Lockfile removed.")
