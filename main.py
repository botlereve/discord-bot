import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import re
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from keep_alive import keep_alive

# ========= 環境變數 =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID"))
TODAY_REMINDER_CHANNEL_ID = int(os.getenv("TODAY_REMINDER_CHANNEL_ID"))  # 當日提醒專用 channel
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID"))
SECOND_USER_ID = int(os.getenv("SECOND_USER_ID"))  # 當日摘要會 tag 第二個人
BOT_COMMAND_CHANNEL_ID = int(os.getenv("BOT_COMMAND_CHANNEL_ID"))
MONGODB_URI = os.getenv("MONGODB_URI")

# ============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
HK_TZ = pytz.timezone("Asia/Hong_Kong")

# ---------- MongoDB ----------
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    mongo_client.admin.command("ping")
    db = mongo_client["reminder_bot"]
    reminders_collection = db["reminders"]
    orders_collection = db["orders"]
    print("✅ Connected to MongoDB")
except ServerSelectionTimeoutError:
    print("❌ Failed to connect to MongoDB")
    reminders_collection = None
    orders_collection = None
except Exception as e:
    print(f"❌ MongoDB error: {e}")
    reminders_collection = None
    orders_collection = None

# 內存 cache：{ user_id: [reminder, ...] }
reminders = {}

# 訂單快取：{ "yymmdd": [order, ...] }
orders_cache = {}


def load_reminders_from_db():
    """啟動時從 MongoDB 載入所有提醒到內存。"""
    global reminders
    try:
        if reminders_collection is None:
            print("⚠ MongoDB not available, using empty cache")
            reminders = {}
            return
        reminders = {}
        for doc in reminders_collection.find():
            user_id = doc["user_id"]
            if user_id not in reminders:
                reminders[user_id] = []
            r = doc.copy()
            r.pop("_id", None)
            r["time"] = datetime.fromisoformat(r["time"])
            reminders[user_id].append(r)
        total = sum(len(v) for v in reminders.values())
        print(f"✅ Loaded {total} reminders from MongoDB")
    except Exception as e:
        print(f"⚠ Error loading reminders from DB: {e}")
        reminders = {}


def load_orders_from_db():
    """從 MongoDB 載入所有訂單到快取。"""
    global orders_cache
    try:
        if orders_collection is None:
            orders_cache = {}
            return
        orders_cache = {}
        for doc in orders_collection.find():
            yymmdd = doc.get("yymmdd")
            if not yymmdd:
                continue
            if yymmdd not in orders_cache:
                orders_cache[yymmdd] = []
            o = doc.copy()
            o.pop("_id", None)
            orders_cache[yymmdd].append(o)
        total = sum(len(v) for v in orders_cache.values())
        print(f"✅ Loaded {total} orders from MongoDB")
    except Exception as e:
        print(f"⚠ Error loading orders from DB: {e}")
        orders_cache = {}


def save_reminder_to_db(user_id: int, reminder: dict):
    """儲存單條提醒到 MongoDB。"""
    try:
        if reminders_collection is None:
            return
        r = reminder.copy()
        r["time"] = r["time"].isoformat()
        r["user_id"] = user_id
        reminders_collection.insert_one(r)
    except Exception as e:
        print(f"⚠ Error saving reminder to DB: {e}")


def update_reminder_in_db(user_id: int, reminder: dict):
    """更新提醒（例如 sent=True）。"""
    try:
        if reminders_collection is None:
            return
        r = reminder.copy()
        r["time"] = r["time"].isoformat()
        r["user_id"] = user_id
        reminders_collection.update_one(
            {"user_id": user_id, "time": r["time"]},
            {"$set": r}
        )
    except Exception as e:
        print(f"⚠ Error updating reminder in DB: {e}")


def save_order_to_db(order: dict):
    """儲存訂單到 MongoDB。"""
    try:
        if orders_collection is None:
            return
        orders_collection.insert_one(order)
    except Exception as e:
        print(f"⚠ Error saving order to DB: {e}")


# ---------- 共用工具 ----------
async def send_reply(message: str):
    """所有回覆都送去 BOT_COMMAND_CHANNEL。"""
    channel = bot.get_channel(BOT_COMMAND_CHANNEL_ID)
    if channel:
        await channel.send(message)
    else:
        print(f"⚠ BOT_COMMAND_CHANNEL_ID not found: {BOT_COMMAND_CHANNEL_ID}")


async def send_today_reminder(embed: discord.Embed, mentions: str = ""):
    """發送當日提醒到 TODAY_REMINDER_CHANNEL。"""
    channel = bot.get_channel(TODAY_REMINDER_CHANNEL_ID)
    if channel:
        if mentions:
            await channel.send(f"{mentions}", embed=embed)
        else:
            await channel.send(embed=embed)
    else:
        print(f"⚠ TODAY_REMINDER_CHANNEL_ID not found: {TODAY_REMINDER_CHANNEL_ID}")


async def send_to_cake_channel(message: str):
    """Send message to 'cake' channel."""
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name.lower() == "cake":
                # Handle Discord 2000 character limit
                if len(message) <= 2000:
                    await channel.send(message)
                else:
                    # Split into chunks
                    lines = message.split("\n")
                    current = ""
                    for line in lines:
                        if len(current) + len(line) + 1 > 1990:
                            if current:
                                await channel.send(current)
                            current = line
                        else:
                            current += "\n" + line if current else line
                    if current:
                        await channel.send(current)
                return True
    return False


def extract_fields(text: str):
    """從【訂單資料】訊息中抽取字段。"""
    pickup = deal = phone = remark = None

    def _after_keyword(s: str, keyword: str):
        if keyword not in s:
            return None
        part = s.split(keyword, 1)[1]
        part = part.lstrip(":： ").strip()
        return part.splitlines()[0].strip() if part else None

    pickup = _after_keyword(text, "取貨日期")
    deal = _after_keyword(text, "交收方式")
    phone = _after_keyword(text, "聯絡人電話")
    remark = _after_keyword(text, "Remark")
    return pickup, deal, phone, remark


def parse_pickup_date(pickup_str: str):
    """
    解析取貨日期，支援：
    - 2025年12月19日
    - 2025-12-19
    - 19/12/2025
    - 12/19 或 19/12 （當年）
    回傳: (datetime, yymmdd_str) 或 (None, None)
    """
    if not pickup_str:
        return None, None
    try:
        m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", pickup_str)
        if m:
            y, mth, d = map(int, m.groups())
            dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
            yymmdd = dt.strftime("%y%m%d")
            return dt, yymmdd

        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", pickup_str)
        if m:
            y, mth, d = map(int, m.groups())
            dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
            yymmdd = dt.strftime("%y%m%d")
            return dt, yymmdd

        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", pickup_str)
        if m:
            d, mth, y = map(int, m.groups())
            dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
            yymmdd = dt.strftime("%y%m%d")
            return dt, yymmdd

        m = re.search(r"(\d{1,2})/(\d{1,2})", pickup_str)
        if m:
            first, second = map(int, m.groups())
            y = datetime.now(HK_TZ).year
            if first > 12:
                d, mth = first, second
            else:
                mth, d = first, second
            dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
            yymmdd = dt.strftime("%y%m%d")
            return dt, yymmdd
    except Exception as e:
        print(f"⚠ parse_pickup_date error: {e}")
    return None, None


# ========= 這裡開始係更新過的訂單內容解析 =========

def normalize_sizes(text: str) -> str:
    """
    將 6\" / 6” / 6 ″ 呢啲樣式統一變成 6吋，避免 size 被 regex 食咗。
    """
    # 匹配：數字 + 可選空格 + 各種雙引號符號
    return re.sub(r'(\d+)\s*[\"”“″]', r'\1吋', text)


def parse_order_content(text: str):
    """
    Extract items from 訂單內容 section.
    - 支援「產品名 × 數量」
    - 支援只有「產品名」（當 1 件）
    - 將 6\"/6”/8\" 等 normalize 成「6吋」
    Returns list of items with quantities.
    Example: ["6吋 威士忌朱古力 拿破崙 × 1", "薄朱 達克瓦茲 × 2"]
    """
    if "訂單內容" not in text:
        return []

    # 先 normalize 吋數
    text = normalize_sizes(text)

    # Split by 訂單內容
    content_part = text.split("訂單內容")[1]

    # Stop at next section (總數, 取貨日期, etc.)
    for stop_kw in ["總數", "取貨日期", "取貨日期：", "取貨日期 :", "交收方式", "Remark"]:
        if stop_kw in content_part:
            content_part = content_part.split(stop_kw)[0]

    content_part = content_part.strip()

    # 一行一件貨：包括可能冇寫 × 數量嘅情況
    # group1: 產品名，group2: 可選數量
    pattern = r'(.+?)(?:\s*(?:×|x)\s*(\d+))?(?:\n|$)'
    matches = re.findall(pattern, content_part)

    items = []
    for product, qty in matches:
        product = product.strip()
        if not product:
            continue
        # 避免捉到「訂單內容」空行之類
        if product in ["訂單內容", "總數", "取貨日期"]:
            continue
        # 默認冇寫數量就當 1 件
        qty = int(qty) if qty else 1
        items.append(f"{product} × {qty}")

    return items


def consolidate_items(items_list):
    """
    Consolidate duplicate items.
    Input: ["薄荷朱古力瑪德蓮 × 1", "薄荷朱古力瑪德蓮 × 2"]
    Output: {"薄荷朱古力瑪德蓮": 3}
    """
    consolidated = {}

    for item in items_list:
        # Try to extract quantity
        if "×" in item or "x" in item:
            # Split by × or x
            parts = item.replace("×", "x").split("x")
            product_name = parts[0].strip()
            try:
                qty = int(parts[-1].strip())
            except:
                qty = 1
        else:
            product_name = item.strip()
            qty = 1

        # Add to consolidated dict
        consolidated[product_name] = consolidated.get(product_name, 0) + qty

    return consolidated

# ========= 訂單 / 提醒邏輯（原樣保留） =========

def add_reminder(
    user_id: int,
    reminder_time: datetime,
    message: str,
    author: str,
    jump_url: str,
    pickup_date: str,
    deal_method: str,
    phone: str,
    remark: str,
    summary_only: bool,
):
    """寫入內存 + MongoDB。"""
    global reminders
    if user_id not in reminders:
        reminders[user_id] = []
    obj = {
        "time": reminder_time,
        "message": message,
        "author": author,
        "jump_url": jump_url,
        "pickup_date": pickup_date,
        "deal_method": deal_method,
        "phone": phone,
        "remark": remark,
        "summary_only": summary_only,
        "sent": False,
    }
    reminders[user_id].append(obj)
    save_reminder_to_db(user_id, obj)


def add_order(
    author: str,
    jump_url: str,
    pickup_date: str,
    yymmdd: str,
    deal_method: str,
    phone: str,
    remark: str,
    full_message: str,
):
    """寫入訂單到內存 + MongoDB。"""
    global orders_cache
    if yymmdd not in orders_cache:
        orders_cache[yymmdd] = []
    obj = {
        "yymmdd": yymmdd,
        "yymm": yymmdd[:4],
        "author": author,
        "jump_url": jump_url,
        "pickup_date": pickup_date,
        "deal_method": deal_method,
        "phone": phone,
        "remark": remark,
        "full_message": full_message,
        "timestamp": datetime.now(HK_TZ).isoformat(),
    }
    orders_cache[yymmdd].append(obj)
    save_order_to_db(obj)

# 之後的 on_ready / on_message / commands / check_reminders / keep_alive() 等照用你而家嗰份就可以，完全唔需要改。
