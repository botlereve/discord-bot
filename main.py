import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import re
import logging
import asyncio
from typing import Dict, List, Optional, Tuple
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError
from keep_alive import keep_alive

# ========= LOGGING SETUP =========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========= 環境變數 =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID"))
TODAY_REMINDER_CHANNEL_ID = int(os.getenv("TODAY_REMINDER_CHANNEL_ID"))
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID"))
SECOND_USER_ID = int(os.getenv("SECOND_USER_ID"))
BOT_COMMAND_CHANNEL_ID = int(os.getenv("BOT_COMMAND_CHANNEL_ID"))
MONGODB_URI = os.getenv("MONGODB_URI")

# ========= BOT SETUP =========
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.description = "Order & Reminder Management Bot"
HK_TZ = pytz.timezone("Asia/Hong_Kong")

# ========= CACHE LOCK =========
cache_lock = asyncio.Lock()

# ========= MONGODB CONNECTION =========
try:
    mongo_client = MongoClient(
        MONGODB_URI,
        serverSelectionTimeoutMS=5000,
        maxPoolSize=50,
        minPoolSize=10,
        maxIdleTimeMS=45000,
        retryWrites=True
    )
    mongo_client.admin.command("ping")
    db = mongo_client["reminder_bot"]
    reminders_collection = db["reminders"]
    orders_collection = db["orders"]
    logger.info("✅ Connected to MongoDB")
except ServerSelectionTimeoutError:
    logger.error("❌ Failed to connect to MongoDB")
    reminders_collection = None
    orders_collection = None
except Exception as e:
    logger.error(f"❌ MongoDB error: {e}")
    reminders_collection = None
    orders_collection = None

# ========= CACHES =========
reminders: Dict[int, List[dict]] = {}
orders_cache: Dict[str, List[dict]] = {}


# ========= PARSER SERVICE =========
class ParserService:
    """Handle all text parsing operations."""
    
    @staticmethod
    def normalize_sizes(text: str) -> str:
        """Normalize size formats: 6" → 6 " (with space after number)."""
        # Match inches variants and add space after number
        text = re.sub(r'(\d+\.?\d*)\s*["″""]', r'\1 "', text)
        return text
    
    @staticmethod
    def extract_fields(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """從【訂單資料】訊息中抽取字段。"""
        pickup = deal = phone = remark = None

        def _after_keyword(s: str, keyword: str) -> Optional[str]:
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

    @staticmethod
    def parse_pickup_date(pickup_str: str) -> Tuple[Optional[datetime], Optional[str]]:
        """
        解析取貨日期，支援：
        - 2025年12月19日
        - 2025-12-19
        - 19/12/2025
        - 12/19 或 19/12 （當年）
        回傳: (datetime, yymmdd_str) 或 (None, None)
        """
        if not pickup_str or len(pickup_str) > 50:
            return None, None
        
        try:
            # Try: 2025年12月19日
            m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", pickup_str)
            if m:
                y, mth, d = map(int, m.groups())
                dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
                yymmdd = dt.strftime("%y%m%d")
                return dt, yymmdd

            # Try: 2025-12-19
            m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", pickup_str)
            if m:
                y, mth, d = map(int, m.groups())
                dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
                yymmdd = dt.strftime("%y%m%d")
                return dt, yymmdd

            # Try: 19/12/2025
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", pickup_str)
            if m:
                d, mth, y = map(int, m.groups())
                dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
                yymmdd = dt.strftime("%y%m%d")
                return dt, yymmdd

            # Try: 12/19 or 19/12 (current year)
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
            logger.warning(f"parse_pickup_date error: {e}")
        
        return None, None

    @staticmethod
    def parse_order_content(text: str) -> List[str]:
        """
        Extract items from 訂單內容 section.
        Normalizes sizes first, then extracts items with quantities.
        Returns list of items in format: "6 " 栗子蜜柑蒙布朗 × 1"
        """
        if "訂單內容" not in text:
            return []

        content_part = text.split("訂單內容")[1]

        # Stop at next section
        for keyword in ["總數", "取貨日期", "交收方式"]:
            if keyword in content_part:
                content_part = content_part.split(keyword)[0]

        content_part = content_part.strip()
        
        # Normalize sizes FIRST (6" → 6 ")
        content_part = ParserService.normalize_sizes(content_part)
        
        # Extract items with format "product × quantity"
        items = []
        pattern = r'([^×\n]+?)\s*(?:×|x)\s*(\d+)'
        matches = re.findall(pattern, content_part)
        
        if matches:
            for product, qty in matches:
                product = product.strip()
                if product and product not in ['總數', '取貨日期']:
                    items.append(f"{product} × {qty}")
        else:
            # No "×" format found, try line-by-line
            for line in content_part.split("\n"):
                line = line.strip()
                if line and line not in ['總數', '取貨日期']:
                    items.append(f"{line} × 1")
        
        return [item.strip() for item in items if item.strip()]

    @staticmethod
    def consolidate_items(items_list: List[str]) -> Dict[str, int]:
        """
        Consolidate duplicate items.
        Input: ["薄荷朱古力瑪德蓮 × 1", "薄荷朱古力瑪德蓮 × 2"]
        Output: {"薄荷朱古力瑪德蓮": 3}
        """
        consolidated = {}

        for item in items_list:
            if "×" in item or "x" in item:
                parts = item.replace("×", "x").split("x")
                product_name = parts[0].strip()
                try:
                    qty = int(parts[-1].strip())
                except:
                    qty = 1
            else:
                product_name = item.strip()
                qty = 1

            consolidated[product_name] = consolidated.get(product_name, 0) + qty

        return consolidated


# ========= ORDER SERVICE =========
class OrderService:
    """Handle all order-related operations."""
    
    def __init__(self, db_collection, cache: dict):
        self.db = db_collection
        self.cache = cache
        self.parser = ParserService()

    def add_order(
        self,
        author: str,
        jump_url: str,
        pickup_date: str,
        yymmdd: str,
        deal_method: str,
        phone: str,
        remark: str,
        full_message: str,
    ) -> None:
        """寫入訂單到內存 + MongoDB。"""
        if yymmdd not in self.cache:
            self.cache[yymmdd] = []
        
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
        self.cache[yymmdd].append(obj)
        self.save_order_to_db(obj)

    def save_order_to_db(self, order: dict) -> None:
        """儲存訂單到 MongoDB。"""
        try:
            if orders_collection is None:
                return
            orders_collection.insert_one(order)
        except Exception as e:
            logger.warning(f"Error saving order to DB: {e}")

    def format_orders_for_date(self, yymmdd: str) -> str:
        """
        Format orders for !c command output.
        Returns formatted string like:
        
        Orders for 2025年12月21日
        6 " 栗子蜜柑蒙布朗 × 1
        8 " 柚子金萱 聖多諾黑 × 1
        ...
        ============================================================ 總數： 17件
        """
        orders = self.cache.get(yymmdd, [])
        if not orders:
            return None

        # Convert yymmdd to display format
        try:
            dt = datetime.strptime(yymmdd, "%y%m%d")
            date_str = dt.strftime("%Y年%m月%d日")
        except:
            date_str = yymmdd

        # Consolidate all items
        all_items = {}
        for order in orders:
            items = self.parser.parse_order_content(order["full_message"])
            consolidated = ParserService.consolidate_items(items)
            for product, qty in consolidated.items():
                all_items[product] = all_items.get(product, 0) + qty

        # Format output
        lines = [f"Orders for {date_str}"]
        total_qty = 0
        
        for product, qty in sorted(all_items.items()):
            lines.append(f"{product} × {qty}")
            total_qty += qty

        # Add separator and total
        lines.append("=" * 60 + f" 總數： {total_qty}件")

        return "\n".join(lines)

    def format_orders_for_month(self, yymm: str) -> str:
        """
        Format all orders for a month.
        Groups by date, shows totals per date and month total.
        """
        matching = {k: v for k, v in self.cache.items() if k.startswith(yymm)}
        if not matching:
            return None

        msg_lines = [f"📋 **Orders for {yymm}**"]
        msg_lines.append("=" * 60)

        total_all_items = {}

        for yymmdd in sorted(matching.keys()):
            orders = matching[yymmdd]
            
            try:
                dt = datetime.strptime(yymmdd, "%y%m%d")
                date_str = dt.strftime("%Y年%m月%d日")
            except:
                date_str = yymmdd

            daily_items = {}
            for order in orders:
                items = self.parser.parse_order_content(order["full_message"])
                consolidated = ParserService.consolidate_items(items)
                for product, qty in consolidated.items():
                    daily_items[product] = daily_items.get(product, 0) + qty
                    total_all_items[product] = total_all_items.get(product, 0) + qty

            msg_lines.append(f"\n**{date_str}** (Total: {sum(daily_items.values())} 件)")
            for product, qty in sorted(daily_items.items()):
                msg_lines.append(f"  {product} × {qty}")

        msg_lines.append("\n" + "=" * 60)
        msg_lines.append(f"**Month Total: {sum(total_all_items.values())} 件**")
        for product, qty in sorted(total_all_items.items()):
            msg_lines.append(f"  {product} × {qty}")

        return "\n".join(msg_lines)


# ========= REMINDER SERVICE =========
class ReminderService:
    """Handle all reminder-related operations."""
    
    def __init__(self, db_collection, cache: dict):
        self.db = db_collection
        self.cache = cache

    def add_reminder(
        self,
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
    ) -> None:
        """寫入內存 + MongoDB。"""
        if user_id not in self.cache:
            self.cache[user_id] = []
        
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
        self.cache[user_id].append(obj)
        self.save_reminder_to_db(user_id, obj)

    def save_reminder_to_db(self, user_id: int, reminder: dict) -> None:
        """儲存單條提醒到 MongoDB。"""
        try:
            if reminders_collection is None:
                return
            r = reminder.copy()
            r["time"] = r["time"].isoformat()
            r["user_id"] = user_id
            reminders_collection.insert_one(r)
        except Exception as e:
            logger.warning(f"Error saving reminder to DB: {e}")

    def update_reminder_in_db(self, user_id: int, reminder: dict) -> None:
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
            logger.warning(f"Error updating reminder in DB: {e}")


# ========= SERVICE INSTANCES =========
parser_service = ParserService()
order_service = OrderService(orders_collection, orders_cache)
reminder_service = ReminderService(reminders_collection, reminders)


# ========= DATABASE LOADING =========
def load_reminders_from_db() -> None:
    """啟動時從 MongoDB 載入所有提醒到內存。"""
    global reminders
    try:
        if reminders_collection is None:
            logger.warning("MongoDB not available, using empty cache")
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
        logger.info(f"✅ Loaded {total} reminders from MongoDB")
    except Exception as e:
        logger.warning(f"Error loading reminders from DB: {e}")
        reminders = {}


def load_orders_from_db() -> None:
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
        logger.info(f"✅ Loaded {total} orders from MongoDB")
    except Exception as e:
        logger.warning(f"Error loading orders from DB: {e}")
        orders_cache = {}


# ========= UTILITY FUNCTIONS =========
async def send_reply(message: str) -> None:
    """所有回覆都送去 BOT_COMMAND_CHANNEL。"""
    channel = bot.get_channel(BOT_COMMAND_CHANNEL_ID)
    if channel:
        await channel.send(message)
    else:
        logger.warning(f"BOT_COMMAND_CHANNEL_ID not found: {BOT_COMMAND_CHANNEL_ID}")


async def send_today_reminder(embed: discord.Embed, mentions: str = "") -> None:
    """發送當日提醒到 TODAY_REMINDER_CHANNEL。"""
    channel = bot.get_channel(TODAY_REMINDER_CHANNEL_ID)
    if channel:
        if mentions:
            await channel.send(f"{mentions}", embed=embed)
        else:
            await channel.send(embed=embed)
    else:
        logger.warning(f"TODAY_REMINDER_CHANNEL_ID not found: {TODAY_REMINDER_CHANNEL_ID}")


async def send_to_cake_channel(message: str) -> bool:
    """Send message to 'cake' channel with Discord 2000 char limit handling."""
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name.lower() == "cake":
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


# ========= EVENTS & COMMANDS =========
@bot.event
async def on_ready() -> None:
    """Bot ready event."""
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    load_reminders_from_db()
    load_orders_from_db()
    check_reminders.start()
    cleanup_cache.start()


@bot.event
async def on_message(message: discord.Message) -> None:
    """所有新訊息：如果有【訂單資料】，自動設定提醒 + 儲存訂單。"""
    if message.author == bot.user:
        return

    if "【訂單資料】" in message.content:
        await process_order_message(message)
    
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error) -> None:
    """Handle command errors gracefully."""
    if isinstance(error, commands.CommandNotFound):
        await send_reply(f"❌ Command not found: {ctx.invoked_with}")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_reply(f"❌ Missing argument: {error.param}")
    elif isinstance(error, commands.MissingPermissions):
        await send_reply("❌ You need **Administrator** permission to use this command.")
    else:
        logger.error(f"Command error: {error}")
        await send_reply(f"❌ An error occurred: {error}")


async def process_order_message(message: discord.Message) -> None:
    """自動幫【訂單資料】設定 2 日前 + 當日提醒，並儲存訂單。"""
    full_text = message.content
    pickup, deal, phone, remark = parser_service.extract_fields(full_text)
    dt_pickup, yymmdd = parser_service.parse_pickup_date(pickup)

    if not dt_pickup:
        await send_reply(
            f"⚠️ Found 【訂單資料】 but pickup date not recognized.\n"
            f" Detected pickup: {pickup or '(not found)'}"
        )
        return

    # 儲存訂單
    async with cache_lock:
        order_service.add_order(
            author=str(message.author),
            jump_url=message.jump_url,
            pickup_date=pickup,
            yymmdd=yymmdd,
            deal_method=deal,
            phone=phone,
            remark=remark,
            full_message=full_text,
        )

    now = datetime.now(HK_TZ)
    user_id = message.author.id

    # 2 日前提醒
    two_days_before = dt_pickup - timedelta(days=2)
    if two_days_before > now:
        async with cache_lock:
            reminder_service.add_reminder(
                user_id=user_id,
                reminder_time=two_days_before,
                message=full_text,
                author=str(message.author),
                jump_url=message.jump_url,
                pickup_date=pickup,
                deal_method=deal,
                phone=phone,
                remark=remark,
                summary_only=False,
            )

        await send_reply(
            f"✅ Auto reminder (2 days before): {two_days_before.strftime('%Y-%m-%d %H:%M')}\n"
            f" 📅 Pickup: {pickup}"
        )
    else:
        # 已少於 2 日 → 即刻發出提醒一次
        if dt_pickup > now:
            channel = bot.get_channel(REMINDER_CHANNEL_ID)
            target_user = await bot.fetch_user(TARGET_USER_ID)
            if channel and target_user:
                embed = discord.Embed(
                    title="⏰ Reminder Time! (auto, <2 days)",
                    description=full_text,
                    color=discord.Color.orange(),
                )
                embed.set_author(name=f"From: {message.author}")
                embed.set_footer(text=f"Pickup: {dt_pickup.strftime('%Y-%m-%d %H:%M')}")
                if message.jump_url:
                    embed.description += f"\n\n[🔗 Original message]({message.jump_url})"
                await channel.send(f"{target_user.mention} Reminder:", embed=embed)
            await send_reply("⚠️ Auto reminder sent because pickup < 2 days.")

    # 當日摘要提醒
    if dt_pickup > now:
        async with cache_lock:
            reminder_service.add_reminder(
                user_id=user_id,
                reminder_time=dt_pickup,
                message=full_text,
                author=str(message.author),
                jump_url=message.jump_url,
                pickup_date=pickup,
                deal_method=deal,
                phone=phone,
                remark=remark,
                summary_only=True,
            )

        await send_reply(
            f"✅ Auto summary on pickup day: {dt_pickup.strftime('%Y-%m-%d %H:%M')}\n"
            f" 📅 Pickup: {pickup}"
        )


@bot.command(name="time", help="Set reminder: !time <hours> [minutes]")
async def set_reminder_time(ctx, hours: int, minutes: int = 0) -> None:
    """!time h m：reply 一條訊息，設定 h 小時 m 分鐘後提醒一次。"""
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!time h m`.")
        return

    try:
        now = datetime.now(HK_TZ)
        reminder_time = now + timedelta(hours=hours, minutes=minutes)
        replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        full_text = replied.content
        pickup, deal, phone, remark = parser_service.extract_fields(full_text)
        user_id = ctx.author.id

        async with cache_lock:
            reminder_service.add_reminder(
                user_id=user_id,
                reminder_time=reminder_time,
                message=full_text,
                author=str(replied.author),
                jump_url=replied.jump_url,
                pickup_date=pickup,
                deal_method=deal,
                phone=phone,
                remark=remark,
                summary_only=False,
            )

        await send_reply(
            f"✅ One-time reminder set for {reminder_time.strftime('%Y-%m-%d %H:%M')}"
        )
    except Exception as e:
        await send_reply(f"❌ Failed to set reminder: {e}")


@bot.command(name="scan", help="Scan orders from last N days (Admin only)")
@commands.has_permissions(administrator=True)
async def scan_orders(ctx, days: int = 7) -> None:
    """!scan [days]：掃過去 N 日所有 channel 嘅【訂單資料】訊息，自動設定提醒。(Admin only)"""
    try:
        await send_reply(f"⏳ Scanning last {days} days...")
        now = datetime.now(HK_TZ)
        cutoff_time = now - timedelta(days=days)
        count = 0
        processed = 0

        for guild in bot.guilds:
            for channel in guild.text_channels:
                try:
                    async for msg in channel.history(after=cutoff_time, limit=None):
                        processed += 1
                        
                        # Show progress every 100 messages
                        if processed % 100 == 0:
                            await send_reply(f"⏳ Processed {processed} messages...")
                        
                        if msg.author == bot.user or "【訂單資料】" not in msg.content:
                            continue
                        
                        pickup_str, deal, phone, remark = parser_service.extract_fields(msg.content)
                        dt_pickup, yymmdd = parser_service.parse_pickup_date(pickup_str)
                        
                        if dt_pickup and yymmdd:
                            existing = orders_cache.get(yymmdd, [])
                            if not any(o["jump_url"] == msg.jump_url for o in existing):
                                async with cache_lock:
                                    order_service.add_order(
                                        author=str(msg.author),
                                        jump_url=msg.jump_url,
                                        pickup_date=pickup_str,
                                        yymmdd=yymmdd,
                                        deal_method=deal,
                                        phone=phone,
                                        remark=remark,
                                        full_message=msg.content,
                                    )

                                # 自動創建提醒
                                user_id = msg.author.id
                                two_days_before = dt_pickup - timedelta(days=2)

                                if two_days_before > now:
                                    async with cache_lock:
                                        reminder_service.add_reminder(
                                            user_id=user_id,
                                            reminder_time=two_days_before,
                                            message=msg.content,
                                            author=str(msg.author),
                                            jump_url=msg.jump_url,
                                            pickup_date=pickup_str,
                                            deal_method=deal,
                                            phone=phone,
                                            remark=remark,
                                            summary_only=False,
                                        )
                                else:
                                    if dt_pickup > now:
                                        target_user = await bot.fetch_user(TARGET_USER_ID)
                                        reminder_ch = bot.get_channel(REMINDER_CHANNEL_ID)
                                        if reminder_ch and target_user:
                                            embed = discord.Embed(
                                                title="⏰ Reminder Time! (auto, <2 days)",
                                                description=msg.content,
                                                color=discord.Color.orange(),
                                            )
                                            embed.set_author(name=f"From: {msg.author}")
                                            embed.set_footer(text=f"Pickup: {dt_pickup.strftime('%Y-%m-%d %H:%M')}")
                                            if msg.jump_url:
                                                embed.description += f"\n\n[🔗 Original message]({msg.jump_url})"
                                            await reminder_ch.send(f"{target_user.mention} Reminder:", embed=embed)

                                if dt_pickup > now:
                                    async with cache_lock:
                                        reminder_service.add_reminder(
                                            user_id=user_id,
                                            reminder_time=dt_pickup,
                                            message=msg.content,
                                            author=str(msg.author),
                                            jump_url=msg.jump_url,
                                            pickup_date=pickup_str,
                                            deal_method=deal,
                                            phone=phone,
                                            remark=remark,
                                            summary_only=True,
                                        )
                                count += 1
                except discord.Forbidden:
                    continue
                except Exception as e:
                    logger.warning(f"Error scanning {channel.name}: {e}")

        await send_reply(f"✅ Scan complete. Processed {processed} messages, saved {count} orders.")
    except Exception as e:
        await send_reply(f"❌ Scan failed: {e}")


@bot.command(name="tdy", help="Show today's orders")
async def show_today_orders(ctx) -> None:
    """!tdy：顯示取貨日期 = 今日嘅所有訂單。"""
    try:
        now = datetime.now(HK_TZ)
        yymmdd = now.strftime("%y%m%d")
        
        output = order_service.format_orders_for_date(yymmdd)
        if not output:
            await send_reply(f"❌ No orders for today ({now.strftime('%Y-%m-%d')}).")
            return

        # Send to cake channel for clean output
        sent = await send_to_cake_channel(output)
        if sent:
            await send_reply(f"✅ Today's orders sent to #cake.")
        else:
            await send_reply(f"❌ #cake channel not found.")
    except Exception as e:
        await send_reply(f"❌ Failed to show today's orders: {e}")


@bot.command(name="d", help="Show orders by date: !d yymmdd or !d yymm")
async def show_orders_by_date(ctx, date_arg: str) -> None:
    """
    !d yymmdd：顯示指定日期嘅訂單
    !d yymm：顯示指定月份嘅訂單
    """
    try:
        if len(date_arg) == 4:
            # !d yymm
            yymm = date_arg
            matching = {k: v for k, v in orders_cache.items() if k.startswith(yymm)}

            if not matching:
                await send_reply(f"❌ No orders found for {yymm}.")
                return

            total_count = sum(len(v) for v in matching.values())
            msg_lines = [f"📋 **Orders for {yymm}** - Total: {total_count}"]
            msg_lines.append("=" * 50)

            for yymmdd in sorted(matching.keys()):
                orders = matching[yymmdd]
                try:
                    dt = datetime.strptime(yymmdd, "%y%m%d")
                    date_str = dt.strftime("%Y-%m-%d")
                except:
                    date_str = yymmdd

                msg_lines.append(f"\n**📅 {date_str}** ({len(orders)} orders)")
                for i, order in enumerate(orders, 1):
                    msg_lines.append(
                        f" #{i} - 📞 {order['phone'] or 'N/A'} | 📍 {order['deal_method'] or 'N/A'}"
                    )
                    if order["remark"]:
                        msg_lines.append(f" 📝 {order['remark']}")

        elif len(date_arg) == 6:
            # !d yymmdd
            yymmdd = date_arg
            orders = orders_cache.get(yymmdd, [])

            if not orders:
                try:
                    dt = datetime.strptime(yymmdd, "%y%m%d")
                    date_str = dt.strftime("%Y-%m-%d")
                except:
                    date_str = yymmdd
                await send_reply(f"❌ No orders found for {date_str}.")
                return

            try:
                dt = datetime.strptime(yymmdd, "%y%m%d")
                date_str = dt.strftime("%Y-%m-%d")
            except:
                date_str = yymmdd

            msg_lines = [f"📋 **Orders for {date_str}** - Total: {len(orders)}"]
            msg_lines.append("=" * 50)

            for i, order in enumerate(orders, 1):
                msg_lines.append(f"\n**#{i}**")
                msg_lines.append(f"👤 Author: {order['author']}")
                msg_lines.append(f"📞 Phone: {order['phone'] or 'N/A'}")
                msg_lines.append(f"📍 Method: {order['deal_method'] or 'N/A'}")
                msg_lines.append(f"📝 Remark: {order['remark'] or 'N/A'}")
                if order["jump_url"]:
                    msg_lines.append(f"🔗 [View Message]({order['jump_url']})")
        else:
            await send_reply("❌ Invalid format. Use `!d yymmdd` or `!d yymm`.")
            return

        full_text = "\n".join(msg_lines)
        reminder_channel = bot.get_channel(REMINDER_CHANNEL_ID)
        if reminder_channel:
            for chunk in [full_text[i : i + 1990] for i in range(0, len(full_text), 1990)]:
                await reminder_channel.send(chunk)
            await send_reply(f"✅ Orders sent to #reminders.")
        else:
            await send_reply(f"❌ REMINDER_CHANNEL not found.")
    except Exception as e:
        await send_reply(f"❌ Failed to show orders: {e}")


@bot.command(name="c", help="Check orders by date: !c yymmdd or !c yymm")
async def check_orders(ctx, date_arg: str) -> None:
    """
    !c yymm：顯示指定月份嘅訂單內容（自動合併重複項目）
    !c yymmdd：顯示指定日期嘅訂單內容及所需數量
    
    Output format:
    Orders for 2025年12月21日
    6 " 栗子蜜柑蒙布朗 × 1
    8 " 柚子金萱 聖多諾黑 × 1
    ...
    ============================================================ 總數： 17件
    """
    try:
        if len(date_arg) == 4:
            # !c yymm - Show month view
            yymm = date_arg
            output = order_service.format_orders_for_month(yymm)
            if not output:
                await send_reply(f"❌ No orders found for {yymm}.")
                return
        elif len(date_arg) == 6:
            # !c yymmdd - Show specific date view
            yymmdd = date_arg
            output = order_service.format_orders_for_date(yymmdd)
            if not output:
                try:
                    dt = datetime.strptime(yymmdd, "%y%m%d")
                    date_str = dt.strftime("%Y-%m-%d")
                except:
                    date_str = yymmdd
                await send_reply(f"❌ No orders found for {date_str}.")
                return
        else:
            await send_reply("❌ Invalid format. Use `!c yymmdd` or `!c yymm`.")
            return

        # Send to #cake channel
        sent = await send_to_cake_channel(output)
        if sent:
            await send_reply(f"✅ Results sent to #cake channel.")
        else:
            await send_reply(f"❌ #cake channel not found.")

    except Exception as e:
        await send_reply(f"❌ Failed to check orders: {e}")


# ========= BACKGROUND TASKS =========
@tasks.loop(minutes=1)
async def check_reminders() -> None:
    """每分鐘檢查有冇到時間嘅提醒，到時間就發，然後標記 sent=True。"""
    now = datetime.now(HK_TZ)
    for user_id, user_rems in list(reminders.items()):
        for r in user_rems[:]:
            if now >= r["time"] and not r.get("sent", False):
                try:
                    target_user = await bot.fetch_user(TARGET_USER_ID)
                    if not target_user:
                        r["sent"] = True
                        reminder_service.update_reminder_in_db(user_id, r)
                        continue

                    summary_only = r.get("summary_only", False)
                    if summary_only:
                        lines = ["Today's Pickup / Delivery:"]
                        if r.get("phone"):
                            lines.append(f"📞 Phone: {r['phone']}")
                        if r.get("deal_method"):
                            lines.append(f"📍 Method: {r['deal_method']}")
                        if r.get("remark"):
                            lines.append(f"📝 Remark: {r['remark']}")
                        desc = "\n".join(lines)
                    else:
                        desc = r["message"]

                    embed = discord.Embed(
                        title="⏰ Reminder Time!",
                        description=desc,
                        color=discord.Color.blue(),
                    )

                    embed.set_author(name=f"From: {r['author']}")
                    embed.set_footer(text=f"Time: {r['time'].strftime('%Y-%m-%d %H:%M')}")
                    if r.get("jump_url"):
                        embed.description += f"\n\n[🔗 Original message]({r['jump_url']})"

                    mentions = target_user.mention
                    if summary_only:
                        try:
                            second_user = await bot.fetch_user(SECOND_USER_ID)
                            if second_user:
                                mentions += f" {second_user.mention}"
                        except Exception:
                            pass

                    if summary_only:
                        await send_today_reminder(embed, mentions)
                    else:
                        channel = bot.get_channel(REMINDER_CHANNEL_ID)
                        if channel:
                            await channel.send(f"{mentions} Reminder:", embed=embed)

                    r["sent"] = True
                    reminder_service.update_reminder_in_db(user_id, r)
                except Exception as e:
                    logger.error(f"Reminder failed: {e}")
                    r["sent"] = True
                    reminder_service.update_reminder_in_db(user_id, r)


@tasks.loop(hours=1)
async def cleanup_cache() -> None:
    """Remove old reminders (30+ days) and orders (90+ days) to prevent memory bloat."""
    try:
        now = datetime.now(HK_TZ)
        
        # Clean up old reminders
        cutoff_reminders = now - timedelta(days=30)
        for user_id, rems in list(reminders.items()):
            reminders[user_id] = [r for r in rems if r["time"] > cutoff_reminders]
            if not reminders[user_id]:
                del reminders[user_id]
        
        # Clean up old orders
        cutoff_orders = now - timedelta(days=90)
        to_delete = []
        for yymmdd in orders_cache.keys():
            try:
                order_date = datetime.strptime(yymmdd, "%y%m%d")
                if order_date < cutoff_orders:
                    to_delete.append(yymmdd)
            except:
                pass
        
        for yymmdd in to_delete:
            del orders_cache[yymmdd]
        
        if to_delete:
            logger.info(f"✅ Cleaned up {len(to_delete)} old order dates")
    except Exception as e:
        logger.error(f"Cache cleanup error: {e}")


# ========= STARTUP =========
keep_alive()
bot.run(BOT_TOKEN)
