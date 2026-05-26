"""
scripts/upload_images.py

Запускать ОДИН РАЗ с локальной машины где есть PNG-файлы.
Загружает все изображения в Telegram, печатает file_id для каждого.

Использование:
    BOT_TOKEN=... ADMIN_ID=... python scripts/upload_images.py

После получения file_id:
1. Скопировать вывод в image_registry.py
2. git rm *.png && git commit -m "Remove 31MB of images from repo"
"""
import asyncio
import os
import sys

from telegram import Bot


BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID  = int(os.environ.get("ADMIN_ID", "0"))

# Все изображения которые используются в боте
IMAGES = [
    "posti.png",
    "posti1.png",
    "posti2.png",
    "posti3.png",
    "posti4.png",
    "posti5.png",
    "posti6.png",
    "posti7.png",
    "posti8.png",
    "posti9.png",
    "posti10.png",
    "posti11.png",
    "posti12.png",
    "posti13.png",
    "posti14.png",
    "1napis.png",
    "2huki.png",
    "4storis.png",
    "5progrev.png",
    "6analitika.png",
    "8animati.png",
    "9idei.png",
]


async def main():
    if not BOT_TOKEN or not ADMIN_ID:
        print("ERROR: BOT_TOKEN и ADMIN_ID должны быть заданы")
        sys.exit(1)

    bot = Bot(BOT_TOKEN)
    results = {}

    for fname in IMAGES:
        if not os.path.exists(fname):
            print(f"SKIP (not found): {fname}")
            continue
        try:
            with open(fname, "rb") as f:
                msg = await bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=f,
                    caption=f"Upload: {fname}",
                )
            file_id = msg.photo[-1].file_id
            results[fname] = file_id
            print(f'✅ {fname}: "{file_id}"')
        except Exception as e:
            print(f"❌ {fname}: {e}")

    print("\n" + "=" * 60)
    print("# Скопируй в image_registry.py:")
    print("=" * 60)
    print("IMAGE_FILE_IDS = {")
    for fname, fid in results.items():
        print(f'    "{fname}": "{fid}",')
    print("}")

    await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
