import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import re
import logging
import asyncio
import hashlib
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
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
mongo_client = None
reminders_collection = None
orders_collection = None

def init_mongodb():
    """Initialize MongoDB connection with error handling."""
    global mongo_client, reminders_collection, orders_collection
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
        return True
    except ServerSelectionTimeoutError:
        logger.error("❌ Failed to connect to MongoDB - timeout")
        return False
    except Exception as e:
        logger.error(f"❌ MongoDB error: {e}")
        return False

# ========= CACHES =========
reminders: Dict[int, List[dict]] = {}
orders_cache: Dict[str, List[dict]] = {}

# ========= INPUT VALIDATOR =========
class InputValidator:
    """Validate and sanitize user inputs."""
    
    @staticmethod
    def validate_date_arg(date_arg: str) -> bool:
        """Ensure date argument is valid format."""
        if not date_arg or len(date_arg) not in [4, 6]:
            return False
        if not date_arg.isdigit():
            return False
        return True
    
    @staticmethod
    def validate_pickup_date(pickup_str: str) -> bool:
        """Ensure pickup date string is reasonable."""
        if not pickup_str:
            return False
        if len(pickup_str) > 100:
            return False
        return True
    
    @staticmethod
    def is_reasonable_quantity(qty: int) -> bool:
        """Ensure quantity is reasonable (1-1000)."""
        return 0 < qty <= 1000

validator = InputValidator()

# ========= RATE LIMITER =========
class RateLimiter:
    """Prevent abuse with rate limiting."""
    
    def __init__(self, max_per_minute: int = 10):
        self.user_commands = defaultdict(list)
        self.max_commands_per_minute = max_per_minute
    
    def is_rate_limited(self, user_id: int) -> bool:
        """Check if user exceeded rate limit."""
        now = datetime.now(HK_TZ)
        one_min_ago = now - timedelta(minutes=1)
        
        self.user_commands[user_id] = [
            cmd_time for cmd_time in self.user_commands[user_id]
            if cmd_time > one_min_ago
        ]
        
        if len(self.user_commands[user_id]) >= self.max_commands_per_minute:
            return True
        
        self.user_commands[user_id].append(now)
        return False

rate_limiter = RateLimiter()

# ========= SMART CACHE =========
class SmartOrderCache:
    """Cache with deduplication and integrity checks."""
    
    def __init__(self):
        self.orders = {}
        self.seen_urls = set()
        self.content_hashes = {}
    
    def is_duplicate(self, jump_url: str, full_message: str) -> bool:
        """Check if order already exists."""
        if jump_url in self.seen_urls:
            return True
        
        content_hash = hashlib.md5(full_message.encode()).hexdigest()
        if content_hash in self.content_hashes.values():
            return True
        
        return False
    
    def add_order(self, yymmdd: str, order: dict) -> bool:
        """Add order only if not duplicate. Returns True if added."""
        if self.is_duplicate(order["jump_url"], order["full_message"]):
            logger.debug(f"Skipped duplicate order: {order['jump_url']}")
            return False
        
        if yymmdd not in self.orders:
            self.orders[yymmdd] = []
        
        self.orders[yymmdd].append(order)
        self.seen_urls.add(order["jump_url"])
        
        content_hash = hashlib.md5(order["full_message"].encode()).hexdigest()
        self.content_hashes[order["jump_url"]] = content_hash
        return True
    
    def get(self, key: str, default=None):
        """Dict-like access."""
        return self.orders.get(key, default)
    
    def items(self):
        """Dict-like iteration."""
        return self.orders.items()
    
    def keys(self):
        """Dict-like keys."""
        return self.orders.keys()

smart_cache = SmartOrderCache()

# ========= BOT STATS =========
class BotStats:
    """Track bot performance and health."""
    
    def __init__(self):
        self.start_time = datetime.now(HK_TZ)
        self.orders_processed = 0
        self.reminders_sent = 0
        self.errors = defaultdict(int)
    
    def record_order(self):
        self.orders_processed += 1
    
    def record_reminder(self):
        self.reminders_sent += 1
    
    def record_error(self, error_type: str):
        self.errors[error_type] += 1
    
    def get_stats(self) -> str:
        uptime = datetime.now(HK_TZ) - self.start_time
        total_reminders = sum(len(v) for v in reminders.values())
        total_orders = sum(len(v) for v in orders_cache.items()) if orders_cache else 0
        
        return f"""📊 **Bot Statistics**
━━━━━━━━━━━━━━━━━━━━━━
⏱️  Uptime: {uptime}
📦 Orders Processed: {self.orders_processed}
⏰ Reminders Sent: {self.reminders_sent}
❌ Errors: {sum(self.errors.values())}
💾 Reminders in Cache: {total_reminders}
📋 Order Dates in Cache: {len(orders_cache)}
"""

bot_stats = BotStats()

# ========= MONGODB HEALTH CHECK =========
async def check_mongodb_health() -> bool:
    """Verify MongoDB connection is alive."""
    try:
        if mongo_client:
            mongo_client.admin.command("ping")
            return True
    except Exception as e:
        logger.error(f"MongoDB health check failed: {e}")
    return False

async def ensure_mongodb_connected() -> bool:
    """Attempt to reconnect if connection lost."""
    global mongo_client, reminders_collection, orders_collection
    
    if not await check_mongodb_health():
        logger.warning("Attempting to reconnect to MongoDB...")
        if init_mongodb():
            return True
    return await check_mongodb_health()

# ========= DATA PERSISTENCE =========
class DataPersistence:
    """Ensure data is safely persisted with backups."""
    
    @staticmethod
    async def backup_cache_to_db():
        """Sync cache to database as backup."""
        try:
            if reminders_collection is None or orders_collection is None:
                return
            
            for yymmdd, orders in orders_cache.items():
                for order in orders:
                    try:
                        existing = orders_collection.find_one({
                            "yymmdd": yymmdd,
                            "jump_url": order["jump_url"]
                        })
                        if not existing:
                            orders_collection.insert_one(order)
                    except Exception as e:
                        logger.warning(f"Backup error for {yymmdd}: {e}")
            
            logger.info("✅ Cache backup complete")
        except Exception as e:
            logger.error(f"Backup failed: {e}")
    
    @staticmethod
    async def verify_data_integrity() -> bool:
        """Check for corrupted data."""
        issues = []
        
        for yymmdd, orders in orders_cache.items():
            if not isinstance(orders, list):
                issues.append(f"Invalid orders type for {yymmdd}")
            for order in orders:
                if not order.get("jump_url"):
                    issues.append(f"Order missing jump_url")
        
        for user_id, rems in reminders.items():
            if not isinstance(rems, list):
                issues.append(f"Invalid reminders type for user {user_id}")
        
        if issues:
            logger.warning(f"Integrity issues: {len(issues)} found")
            return False
        
        logger.info("✅ Data integrity check passed")
        return True

data_persistence = DataPersistence()

# ========= PARSER SERVICE =========
class ParserService:
    """Handle all text parsing operations."""
    
    @staticmethod
    def normalize_sizes(text: str) -> str:
        """Normalize size formats: 6" → 6 " (with space)."""
        text = re.sub(r'(\d+\.?\d*)\s*["″""]', r'\1 "', text)
        return text
    
    @staticmethod
    def extract_fields(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """Extract order fields from message."""
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
    def parse_pickup_date_smart(pickup_str: str) -> Tuple[Optional[datetime], Optional[str]]:
        """Parse date with validation and multiple fallbacks."""
        if not pickup_str or not validator.validate_pickup_date(pickup_str):
            return None, None
        
        try:
            # Try: 2025年12月19日
            m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", pickup_str)
            if m:
                y, mth, d = map(int, m.groups())
                if 1 <= mth <= 12 and 1 <= d <= 31:
                    dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
                    return dt, dt.strftime("%y%m%d")

            # Try: 2025-12-19
            m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", pickup_str)
            if m:
                y, mth, d = map(int, m.groups())
                if 1 <= mth <= 12 and 1 <= d <= 31:
                    dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
                    return dt, dt.strftime("%y%m%d")

            # Try: 19/12/2025
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", pickup_str)
            if m:
                d, mth, y = map(int, m.groups())
                if 1 <= mth <= 12 and 1 <= d <= 31:
                    dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
                    return dt, dt.strftime("%y%m%d")

            # Try: 12/19 or 19/12 (current year)
            m = re.search(r"(\d{1,2})/(\d{1,2})", pickup_str)
            if m:
                first, second = map(int, m.groups())
                y = datetime.now(HK_TZ).year
                d = first if first > 12 else second
                mth = second if first > 12 else first
                if 1 <= mth <= 12 and 1 <= d <= 31:
                    dt = HK_TZ.localize(datetime(y, mth, d, 9, 0))
                    return dt, dt.strftime("%y%m%d")
        except Exception as e:
            logger.warning(f"Date parse error: {e}")
        
        return None, None

    @staticmethod
    def parse_order_content_smart(text: str) -> List[str]:
        """Parse items with smart fallback strategies."""
        if "訂單內容" not in text:
            return []

        content_part = text.split("訂單內容")[1]
        for keyword in ["總數", "取貨日期", "交收方式"]:
            if keyword in content_part:
                content_part = content_part.split(keyword)[0]

        content_part = content_part.strip()
        content_part = ParserService.normalize_sizes(content_part)
        
        items = []
        
        # Strategy 1: Look for × or x pattern
        pattern = r'([^×\n]+?)\s*(?:×|x)\s*(\d+)'
        matches = re.findall(pattern, content_part)
        
        if matches:
            for product, qty in matches:
                product = product.strip()
                if product and len(product) < 100:
                    try:
                        qty_int = int(qty)
                        if validator.is_reasonable_quantity(qty_int):
                            items.append(f"{product} × {qty_int}")
                    except:
                        items.append(f"{product} × 1")
            return items
        
        # Strategy 2: Line-by-line if no × found
        for line in content_part.split("\n"):
            line = line.strip()
            if line and len(line) < 100 and line not in ['總數', '取貨日期']:
                items.append(f"{line} × 1")
        
        return items

    @staticmethod
    def consolidate_items(items_list: List[str]) -> Dict[str, int]:
        """Consolidate duplicate items with validation."""
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

            if product_name and 1 <= qty <= 1000:
                consolidated[product_name] = consolidated.get(product_name, 0) + qty

        return consolidated

parser_service = ParserService()

# ========= ORDER SERVICE =========
class OrderService:
    """Handle all order-related operations."""
    
    def __init__(self, cache: SmartOrderCache):
        self.cache = cache

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
    ) -> bool:
        """Add order to cache. Returns True if added (not duplicate)."""
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
        
        if self.cache.add_order(yymmdd, obj):
            self.save_order_to_db(obj)
            bot_stats.record_order()
            return True
        return False

    def save_order_to_db(self, order: dict) -> None:
        """Save order to MongoDB."""
        try:
            if orders_collection is None:
                return
            orders_collection.insert_one(order)
        except Exception as e:
            logger.warning(f"Error saving order to DB: {e}")
            bot_stats.record_error("db_write")

    def format_orders_for_date(self, yymmdd: str) -> Optional[str]:
        """Format orders for !c output."""
        orders = self.cache.get(yymmdd, [])
        if not orders:
            return None

        try:
            dt = datetime.strptime(yymmdd, "%y%m%d")
            date_str = dt.strftime("%Y年%m月%d日")
        except:
            date_str = yymmdd

        all_items = {}
        for order in orders:
            items = parser_service.parse_order_content_smart(order["full_message"])
            consolidated = ParserService.consolidate_items(items)
            for product, qty in consolidated.items():
                all_items[product] = all_items.get(product, 0) + qty

        lines = [f"Orders for {date_str}"]
        total_qty = 0
        
        for product, qty in sorted(all_items.items()):
            lines.append(f"{product} × {qty}")
            total_qty += qty

        lines.append("=" * 60 + f" 總數： {total_qty}件")
        return "\n".join(lines)

    def format_orders_for_month(self, yymm: str) -> Optional[str]:
        """Format all orders for a month."""
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
                items = parser_service.parse_order_content_smart(order["full_message"])
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

order_service = OrderService(smart_cache)

# ========= REMINDER SERVICE =========
class ReminderService:
    """Handle all reminder-related operations."""
    
    def __init__(self, cache: dict):
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
        """Add reminder to cache."""
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
        """Save reminder to MongoDB."""
        try:
            if reminders_collection is None:
                return
            r = reminder.copy()
            r["time"] = r["time"].isoformat()
            r["user_id"] = user_id
            reminders_collection.insert_one(r)
        except Exception as e:
            logger.warning(f"Error saving reminder: {e}")
            bot_stats.record_error("reminder_save")

    def update_reminder_in_db(self, user_id: int, reminder: dict) -> None:
        """Update reminder in MongoDB."""
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
            logger.warning(f"Error updating reminder: {e}")

reminder_service = ReminderService(reminders)

# ========= DATABASE LOADING =========
def load_reminders_from_db() -> None:
    """Load all reminders from MongoDB."""
    global reminders
    try:
        if reminders_collection is None:
            logger.warning("MongoDB not available")
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
        logger.info(f"✅ Loaded {total} reminders")
    except Exception as e:
        logger.warning(f"Error loading reminders: {e}")
        reminders = {}

def load_orders_from_db() -> None:
    """Load all orders from MongoDB."""
    try:
        if orders_collection is None:
            return
        
        for doc in orders_collection.find():
            yymmdd = doc.get("yymmdd")
            if not yymmdd:
                continue
            
            o = doc.copy()
            o.pop("_id", None)
            
            if smart_cache.add_order(yymmdd, o):
                # Only count if not duplicate
                bot_stats.record_order()
        
        total = sum(len(v) for v in smart_cache.items())
        logger.info(f"✅ Loaded {total} orders")
    except Exception as e:
        logger.warning(f"Error loading orders: {e}")

# ========= UTILITY FUNCTIONS =========
async def send_reply(message: str) -> None:
    """Send reply to BOT_COMMAND_CHANNEL."""
    try:
        channel = bot.get_channel(BOT_COMMAND_CHANNEL_ID)
        if channel:
            await channel.send(message)
        else:
            logger.warning(f"Command channel not found: {BOT_COMMAND_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"Error sending reply: {e}")
        bot_stats.record_error("send_reply")

async def send_today_reminder(embed: discord.Embed, mentions: str = "") -> None:
    """Send reminder to TODAY_REMINDER_CHANNEL."""
    try:
        channel = bot.get_channel(TODAY_REMINDER_CHANNEL_ID)
        if channel:
            if mentions:
                await channel.send(f"{mentions}", embed=embed)
            else:
                await channel.send(embed=embed)
        else:
            logger.warning(f"Today reminder channel not found")
    except Exception as e:
        logger.error(f"Error sending today reminder: {e}")
        bot_stats.record_error("send_today_reminder")

async def send_to_cake_channel(message: str) -> bool:
    """Send message to #cake channel."""
    try:
        for guild in bot.guilds:
            for channel in guild.text_channels:
                if channel.name.lower() == "cake":
                    if len(message) <= 2000:
                        await channel.send(message)
                    else:
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
    except Exception as e:
        logger.error(f"Error sending to cake channel: {e}")
        bot_stats.record_error("cake_channel")
    
    return False

# ========= EVENTS & COMMANDS =========
@bot.event
async def on_ready() -> None:
    """Bot startup event."""
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    load_reminders_from_db()
    load_orders_from_db()
    check_reminders.start()
    cleanup_cache.start()
    periodic_backup.start()
    periodic_health_check.start()

@bot.event
async def on_message(message: discord.Message) -> None:
    """Handle incoming messages."""
    if message.author == bot.user:
        return

    if "【訂單資料】" in message.content:
        await process_order_message(message)
    
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error) -> None:
    """Handle command errors."""
    try:
        if isinstance(error, commands.CommandNotFound):
            await send_reply(f"❌ Command not found: {ctx.invoked_with}")
        elif isinstance(error, commands.MissingRequiredArgument):
            await send_reply(f"❌ Missing argument: {error.param}")
        elif isinstance(error, commands.MissingPermissions):
            await send_reply("❌ You need **Administrator** permission.")
        else:
            logger.error(f"Command error: {error}")
            await send_reply(f"❌ Error: {str(error)[:100]}")
            bot_stats.record_error("command_error")
    except Exception as e:
        logger.error(f"Error in error handler: {e}")

@bot.event
async def on_disconnect() -> None:
    """Handle bot disconnection."""
    logger.warning("⚠️ Bot disconnected, backing up cache...")
    await data_persistence.backup_cache_to_db()

# ========= ORDER MESSAGE PROCESSING =========
async def process_order_message(message: discord.Message) -> None:
    """Process incoming order message."""
    try:
        full_text = message.content
        pickup, deal, phone, remark = parser_service.extract_fields(full_text)
        dt_pickup, yymmdd = parser_service.parse_pickup_date_smart(pickup)

        if not dt_pickup:
            await send_reply(
                f"⚠️ Found 【訂單資料】 but pickup date not recognized.\n"
                f" Detected: {pickup or '(not found)'}"
            )
            return

        # Save order
        async with cache_lock:
            if not order_service.add_order(
                author=str(message.author),
                jump_url=message.jump_url,
                pickup_date=pickup,
                yymmdd=yymmdd,
                deal_method=deal,
                phone=phone,
                remark=remark,
                full_message=full_text,
            ):
                await send_reply("⚠️ Order already exists (duplicate)")
                return

        now = datetime.now(HK_TZ)
        user_id = message.author.id

        # Set reminders
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
            await send_reply(f"✅ Reminder set for {two_days_before.strftime('%Y-%m-%d %H:%M')}")
        else:
            if dt_pickup > now:
                channel = bot.get_channel(REMINDER_CHANNEL_ID)
                target_user = await bot.fetch_user(TARGET_USER_ID)
                if channel and target_user:
                    embed = discord.Embed(
                        title="⏰ Reminder (Auto, <2 days)",
                        description=full_text[:1024],
                        color=discord.Color.orange(),
                    )
                    embed.set_author(name=f"From: {message.author}")
                    if message.jump_url:
                        embed.add_field(name="Link", value=f"[View]({message.jump_url})", inline=False)
                    await channel.send(f"{target_user.mention}", embed=embed)
                await send_reply("⚠️ Pickup < 2 days, reminder sent now")

        # Set today reminder
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
            await send_reply(f"✅ Today reminder set for {dt_pickup.strftime('%Y-%m-%d')}")
    except Exception as e:
        logger.error(f"Error processing order: {e}")
        await send_reply(f"❌ Error processing order: {str(e)[:100]}")
        bot_stats.record_error("process_order")

# ========= COMMANDS =========
@bot.command(name="time", help="Set reminder: !time <hours> [minutes]")
async def set_reminder_time(ctx, hours: int, minutes: int = 0) -> None:
    """Set one-time reminder."""
    try:
        if ctx.message.reference is None:
            await send_reply("❌ Please reply to a message first")
            return

        now = datetime.now(HK_TZ)
        reminder_time = now + timedelta(hours=hours, minutes=minutes)
        replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        
        pickup, deal, phone, remark = parser_service.extract_fields(replied.content)
        
        async with cache_lock:
            reminder_service.add_reminder(
                user_id=ctx.author.id,
                reminder_time=reminder_time,
                message=replied.content,
                author=str(replied.author),
                jump_url=replied.jump_url,
                pickup_date=pickup,
                deal_method=deal,
                phone=phone,
                remark=remark,
                summary_only=False,
            )

        await send_reply(f"✅ Reminder set for {reminder_time.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        await send_reply(f"❌ Failed: {str(e)[:100]}")
        bot_stats.record_error("set_time")

@bot.command(name="c", help="Check orders: !c yymmdd or !c yymm")
async def check_orders(ctx, date_arg: str) -> None:
    """Check orders by date with smart formatting."""
    try:
        if rate_limiter.is_rate_limited(ctx.author.id):
            await send_reply("⚠️ Too many requests, please wait")
            return
        
        if not validator.validate_date_arg(date_arg):
            await send_reply("❌ Invalid format. Use `!c yymmdd` or `!c yymm`")
            return
        
        if len(date_arg) == 6:
            output = order_service.format_orders_for_date(date_arg)
        else:
            output = order_service.format_orders_for_month(date_arg)
        
        if not output:
            await send_reply(f"❌ No orders found for {date_arg}")
            return
        
        sent = await send_to_cake_channel(output)
        await send_reply("✅ Results sent to #cake" if sent else "❌ #cake not found")
    except Exception as e:
        logger.error(f"Check orders error: {e}")
        await send_reply(f"❌ Error: {str(e)[:100]}")
        bot_stats.record_error("check_orders")

@bot.command(name="stats", help="Show bot statistics")
async def show_stats(ctx) -> None:
    """Display bot statistics."""
    try:
        await send_reply(bot_stats.get_stats())
    except Exception as e:
        logger.error(f"Stats error: {e}")

@bot.command(name="tdy", help="Show today's orders")
async def show_today_orders(ctx) -> None:
    """Show today's orders."""
    try:
        now = datetime.now(HK_TZ)
        yymmdd = now.strftime("%y%m%d")
        
        output = order_service.format_orders_for_date(yymmdd)
        if not output:
            await send_reply(f"❌ No orders for today ({now.strftime('%Y-%m-%d')})")
            return
        
        sent = await send_to_cake_channel(output)
        await send_reply("✅ Today's orders sent" if sent else "❌ #cake not found")
    except Exception as e:
        await send_reply(f"❌ Error: {str(e)[:100]}")
        bot_stats.record_error("today_orders")

# ========= BACKGROUND TASKS =========
@tasks.loop(minutes=1)
async def check_reminders() -> None:
    """Check and send due reminders."""
    try:
        now = datetime.now(HK_TZ)
        for user_id, user_rems in list(reminders.items()):
            for r in user_rems[:]:
                if now >= r["time"] and not r.get("sent", False):
                    try:
                        target_user = await bot.fetch_user(TARGET_USER_ID)
                        if not target_user:
                            r["sent"] = True
                            continue

                        summary_only = r.get("summary_only", False)
                        if summary_only:
                            desc = "Today's Pickup:\n"
                            if r.get("phone"):
                                desc += f"📞 {r['phone']}\n"
                            if r.get("deal_method"):
                                desc += f"📍 {r['deal_method']}\n"
                            if r.get("remark"):
                                desc += f"📝 {r['remark']}"
                        else:
                            desc = r["message"][:1024]

                        embed = discord.Embed(
                            title="⏰ Reminder Time!",
                            description=desc,
                            color=discord.Color.blue(),
                        )
                        embed.set_author(name=f"From: {r['author']}")
                        if r.get("jump_url"):
                            embed.add_field(name="Link", value=f"[View]({r['jump_url']})", inline=False)

                        mentions = target_user.mention
                        if summary_only:
                            try:
                                second_user = await bot.fetch_user(SECOND_USER_ID)
                                if second_user:
                                    mentions += f" {second_user.mention}"
                            except:
                                pass
                            await send_today_reminder(embed, mentions)
                        else:
                            channel = bot.get_channel(REMINDER_CHANNEL_ID)
                            if channel:
                                await channel.send(f"{mentions}", embed=embed)

                        r["sent"] = True
                        reminder_service.update_reminder_in_db(user_id, r)
                        bot_stats.record_reminder()
                    except Exception as e:
                        logger.error(f"Reminder send error: {e}")
                        r["sent"] = True
                        bot_stats.record_error("send_reminder")
    except Exception as e:
        logger.error(f"Check reminders error: {e}")
        bot_stats.record_error("check_reminders")

@tasks.loop(hours=1)
async def cleanup_cache() -> None:
    """Remove old data to prevent memory bloat."""
    try:
        now = datetime.now(HK_TZ)
        
        cutoff_reminders = now - timedelta(days=30)
        for user_id, rems in list(reminders.items()):
            reminders[user_id] = [r for r in rems if r["time"] > cutoff_reminders]
            if not reminders[user_id]:
                del reminders[user_id]
        
        logger.info("✅ Cache cleanup complete")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

@tasks.loop(hours=6)
async def periodic_backup() -> None:
    """Backup cache every 6 hours."""
    try:
        await data_persistence.backup_cache_to_db()
    except Exception as e:
        logger.error(f"Backup error: {e}")
        bot_stats.record_error("backup")

@tasks.loop(hours=1)
async def periodic_health_check() -> None:
    """Check system health hourly."""
    try:
        await ensure_mongodb_connected()
        await data_persistence.verify_data_integrity()
    except Exception as e:
        logger.error(f"Health check error: {e}")
        bot_stats.record_error("health_check")

# ========= STARTUP =========
if __name__ == "__main__":
    logger.info("🚀 Starting bot...")
    if init_mongodb():
        keep_alive()
        bot.run(BOT_TOKEN)
    else:
        logger.error("❌ Failed to initialize MongoDB, cannot start bot")
