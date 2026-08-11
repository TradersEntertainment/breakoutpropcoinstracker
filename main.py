"""
Giriş noktası — üç parçayı tek süreçte çalıştırır:
  1. Funding alarm botu (bot.py) → TELEGRAM_TOKEN / CHAT_ID
  2. Range finder (range_finder.py) → RANGE_CHAT_ID
  3. Dashboard web sunucusu (dashboard.py) → PORT

Railway startCommand: python main.py
"""

import threading
import time
import traceback

import bot
import dashboard
import range_finder
import simulator
from bot import log


def supervise(name: str, target) -> threading.Thread:
    """Döngüyü ayrı thread'de çalıştırır; çökerse 30 sn sonra yeniden başlatır."""

    def runner() -> None:
        while True:
            try:
                target()
                log(f"{name} döngüsü sonlandı.")
                return  # normal dönüş (ör. RUN_ONCE)
            except Exception:
                log(f"{name} çöktü, 30 sn sonra yeniden başlatılacak:\n{traceback.format_exc()}")
                time.sleep(30)

    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    return thread


def main() -> None:
    supervise("funding-bot", bot.main)
    supervise("range-finder", range_finder.main)
    supervise("simulator", simulator.main)
    dashboard.serve()  # ana thread'i işgal eder


if __name__ == "__main__":
    main()
