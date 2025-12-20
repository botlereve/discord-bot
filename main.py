# ============================================================
# INTEGRATED BOT - COMMAND-CORRECTED VERSION
# Your commands FIXED to match original behavior:
# !d = show order DATE/details (who ordered, phone, location)
# !c = show order COUNT/contents (cake types and quantities)
# ============================================================

import os
import discord
from discord.ext import commands, tasks
from discord import ui, app_commands, SelectOption, Interaction
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

# ========= KEEP ALIVE (Optional) =========
try:
    from keep_alive import keep_alive
except ImportError:
    def keep_alive():
        pass

# ========= LOGGING SETUP =========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========= ENVIRONMENT VARIABLES =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID", "0") or "0")
TODAY_REMINDER_CHANNEL_ID = int(os.getenv("TODAY_REMINDER_CHANNEL_ID", "0") or "0")
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0") or "0")
SECOND_USER_ID = int(os.getenv("SECOND_USER_ID", "0") or "0")
BOT_COMMAND_CHANNEL_ID = int(os.getenv("BOT_COMMAND_CHANNEL_ID", "0") or "0")
MONGODB_URI = os.getenv("MONGODB_URI")

# ========= BOT SETUP =========
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.description = "Order & Reminder Management Bot + Cake Ordering System"
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
user_carts: Dict[int, List[dict]] = {}
user_order_details: Dict[int, dict] = {}

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
    
    def __init__(self):
        self.cache = orders_cache

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
        if yymmdd not in self.cache:
            self.cache[yymmdd] = []
        
        # Check for duplicates
        if any(o["jump_url"] == jump_url for o in self.cache[yymmdd]):
            return False
        
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
        return True

    def save_order_to_db(self, order: dict) -> None:
        """Save order to MongoDB."""
        try:
            if orders_collection is None:
                return
            orders_collection.insert_one(order)
        except Exception as e:
            logger.warning(f"Error saving order to DB: {e}")

    def format_orders_detail(self, yymmdd: str) -> Optional[str]:
        """
        Format orders for !d output (show order details: who, phone, location, remark)
        This shows: Author, Phone, Delivery Method, Remark, Link
        """
        orders = self.cache.get(yymmdd, [])
        if not orders:
            return None

        try:
            dt = datetime.strptime(yymmdd, "%y%m%d")
            date_str = dt.strftime("%Y年%m月%d日")
        except:
            date_str = yymmdd

        lines = [f"📋 **Orders for {date_str}** - Total: {len(orders)}"]
        lines.append("=" * 60)

        for i, order in enumerate(orders, 1):
            lines.append(f"\n**Order #{i}**")
            lines.append(f"👤 Author: {order['author']}")
            lines.append(f"📞 Phone: {order['phone'] or 'N/A'}")
            lines.append(f"📍 Delivery: {order['deal_method'] or 'N/A'}")
            lines.append(f"📝 Remark: {order['remark'] or 'N/A'}")
            if order["jump_url"]:
                lines.append(f"🔗 [View]({order['jump_url']})")

        return "\n".join(lines)

    def format_orders_content(self, yymmdd: str) -> Optional[str]:
        """
        Format orders for !c output (show order contents: what cakes and quantities)
        This shows: consolidated items × quantities
        """
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

        lines = [f"📋 **Orders for {date_str}**"]
        
        for product, qty in sorted(all_items.items()):
            lines.append(f"- {product} × {qty}")

        lines.append("=" * 60)
        lines.append(f"**總數： {sum(all_items.values())}件**")

        return "\n".join(lines)

    def format_month_detail(self, yymm: str) -> Optional[str]:
        """Format all orders detail for a month (!d yymm)."""
        matching = {k: v for k, v in self.cache.items() if k.startswith(yymm)}
        if not matching:
            return None

        msg_lines = [f"📋 **Orders for {yymm}**"]
        msg_lines.append("=" * 60)

        for yymmdd in sorted(matching.keys()):
            orders = matching[yymmdd]
            
            try:
                dt = datetime.strptime(yymmdd, "%y%m%d")
                date_str = dt.strftime("%Y年%m月%d日")
            except:
                date_str = yymmdd

            msg_lines.append(f"\n**📅 {date_str}** ({len(orders)} orders)")
            for i, order in enumerate(orders, 1):
                msg_lines.append(
                    f" #{i} 📞 {order['phone'] or 'N/A'} | 📍 {order['deal_method'] or 'N/A'}"
                )
                if order['remark']:
                    msg_lines.append(f"    📝 {order['remark']}")

        return "\n".join(msg_lines)

    def format_month_content(self, yymm: str) -> Optional[str]:
        """Format all orders content for a month (!c yymm)."""
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
                msg_lines.append(f"  - {product} × {qty}")

        msg_lines.append("\n" + "=" * 60)
        msg_lines.append(f"**Month Total: {sum(total_all_items.values())} 件**")
        for product, qty in sorted(total_all_items.items()):
            msg_lines.append(f"  - {product} × {qty}")

        return "\n".join(msg_lines)

order_service = OrderService()

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
            
            if yymmdd not in orders_cache:
                orders_cache[yymmdd] = []
            
            o = doc.copy()
            o.pop("_id", None)
            orders_cache[yymmdd].append(o)
        
        total = sum(len(v) for v in orders_cache.values())
        logger.info(f"✅ Loaded {total} orders")
    except Exception as e:
        logger.warning(f"Error loading orders: {e}")

# ========= UTILITY FUNCTIONS =========
async def send_reply(message: str) -> None:
    """Send reply to BOT_COMMAND_CHANNEL."""
    try:
        if BOT_COMMAND_CHANNEL_ID == 0:
            logger.warning("BOT_COMMAND_CHANNEL_ID not set")
            return
        channel = bot.get_channel(BOT_COMMAND_CHANNEL_ID)
        if channel:
            await channel.send(message)
        else:
            logger.warning(f"Command channel not found: {BOT_COMMAND_CHANNEL_ID}")
    except Exception as e:
        logger.error(f"Error sending reply: {e}")

async def send_today_reminder(embed: discord.Embed, mentions: str = "") -> None:
    """Send reminder to TODAY_REMINDER_CHANNEL."""
    try:
        if TODAY_REMINDER_CHANNEL_ID == 0:
            return
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
    
    return False

# ========= CAKE ORDER PRODUCTS =========
PRODUCTS = {
    "Mini": {
        "瑪德蓮 Madeleine": {
            "經典檸檬 Classic Lemon": 22,
            "雙重朱古力 Double Chocolate": 32,
            "小山園脆殼焙茶 Koyamaen Crispy Tea": 32,
            "雙重開心果 Double Pistachio (Limited) ✨️": 36,
        },
        "達克瓦茲 Dacquoise": {
            "粟米忌廉 Corn Cream (Limited) ✨️": 34,
        },
        "費南雪 Financier / 可麗露 Canelé": {
            "費南雪 Financier": 22,
            "可麗露 Canelé": 26,
            "咖啡費南雪 Coffee Financier (Limited) ✨️": 26,
        }
    },
    "3\"": {
        "法式蛋糕 French Pastry": {
            "威士忌長胡椒朱古力拿破崙 Whiskey Long Pepper Chocolate Mille Feuille": 62,
            "柚香金萱烏龍聖多諾黑 Yuzu Jin Xuan Oolong St. Honoré": 58,
            "栗子蜜柑蒙布朗 Mont Blanc": 58,
            "蘋果酥盒 Apple Box": 52,
        }
    },
    "6\"": {
        "節慶蛋糕 / Whole Cake": {
            "威士忌長胡椒朱古力拿破崙 Whiskey Long Pepper": 438,
            "柚香金萱烏龍聖多諾黑 Yuzu Jin Xuan Oolong": 388,
            "法式蘋果撻 French Apple Tart": 408,
            "栗子蜜柑蒙布朗 Mont Blanc": 408,
        }
    },
    "8\"": {
        "節慶蛋糕 / Whole Cake": {
            "柚香金萱烏龍聖多諾黑 Yuzu Jin Xuan Oolong": 588,
            "焦糖咖啡千層蛋糕 Caramel Coffee Crêpe Cake": 608,
            "芒草伯爵茶千層蛋糕 Earl Grey Mango Crêpe": 618,
            "薄荷朱古力拿破崙 Mint Chocolate Mille Feuille": 618,
            "威士忌長胡椒朱古力拿破崙 Whiskey Long Pepper": 688,
        }
    }
}

# ========= CAKE ORDER COMPONENTS =========
class SizeSelect(ui.Select):
    def __init__(self):
        options = [
            SelectOption(label="Mini ($22-$36)", value="Mini", emoji="🍡"),
            SelectOption(label="3\" French Pastry ($52-$62)", value="3\"", emoji="🥐"),
            SelectOption(label="6\" Whole Cake ($388-$438)", value="6\"", emoji="🎂"),
            SelectOption(label="8\" Whole Cake ($588-$688)", value="8\"", emoji="🍰"),
        ]
        super().__init__(
            placeholder="📏 Select Size",
            options=options,
            custom_id="size_select"
        )
    
    async def callback(self, interaction: Interaction):
        await interaction.response.defer()

class TypeSelect(ui.Select):
    def __init__(self, size: str):
        options = []
        if size in PRODUCTS:
            for type_name in PRODUCTS[size].keys():
                options.append(SelectOption(label=type_name, value=type_name))
        
        super().__init__(
            placeholder="📋 Select Type",
            options=options[:25],
            custom_id="type_select"
        )
    
    async def callback(self, interaction: Interaction):
        await interaction.response.defer()

class ProductSelect(ui.Select):
    def __init__(self, size: str, type_name: str):
        options = []
        if size in PRODUCTS and type_name in PRODUCTS[size]:
            for product_name, price in PRODUCTS[size][type_name].items():
                label = f"{product_name[:75]} (${price})"
                if len(label) > 100:
                    label = f"{product_name[:60]}... (${price})"
                options.append(SelectOption(label=label, value=f"{product_name}|{price}"))
        
        super().__init__(
            placeholder="🍰 Select Product",
            options=options[:25],
            custom_id="product_select"
        )
    
    async def callback(self, interaction: Interaction):
        await interaction.response.defer()

class OrderBuilderView(ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.selected_size = None
        self.selected_type = None
        self.selected_product = None
        self.selected_price = None
        self.quantity = 1
        
        self.add_item(SizeSelect())
        self.add_item(ConfirmButton(self, "size"))

class ConfirmButton(ui.Button):
    def __init__(self, view, step):
        super().__init__(label="✓ Confirm", style=discord.ButtonStyle.green)
        self.view = view
        self.step = step
    
    async def callback(self, interaction: Interaction):
        if self.step == "size":
            size_select = None
            for item in self.view.children:
                if isinstance(item, SizeSelect):
                    size_select = item
                    break
            
            if size_select and size_select.values:
                self.view.selected_size = size_select.values[0]
                self.view.clear_items()
                self.view.add_item(SizeSelect())
                self.view.add_item(TypeSelect(self.view.selected_size))
                self.view.add_item(ConfirmButton(self.view, "type"))
                await interaction.response.edit_message(view=self.view)
            else:
                await interaction.response.defer()
        
        elif self.step == "type":
            type_select = None
            for item in self.view.children:
                if isinstance(item, TypeSelect):
                    type_select = item
                    break
            
            if type_select and type_select.values:
                self.view.selected_type = type_select.values[0]
                self.view.clear_items()
                self.view.add_item(SizeSelect())
                self.view.add_item(TypeSelect(self.view.selected_size))
                self.view.add_item(ProductSelect(self.view.selected_size, self.view.selected_type))
                self.view.add_item(ConfirmButton(self.view, "product"))
                await interaction.response.edit_message(view=self.view)
            else:
                await interaction.response.defer()
        
        elif self.step == "product":
            product_select = None
            for item in self.view.children:
                if isinstance(item, ProductSelect):
                    product_select = item
                    break
            
            if product_select and product_select.values:
                product_value = product_select.values[0]
                self.view.selected_product, price_str = product_value.split("|")
                self.view.selected_price = int(price_str)
                
                self.view.clear_items()
                self.view.add_item(AddToCartButton(self.view))
                self.view.add_item(ViewCartButton(self.view.user_id))
                await interaction.response.edit_message(view=self.view)
            else:
                await interaction.response.defer()

class AddToCartButton(ui.Button):
    def __init__(self, view):
        super().__init__(label="🛒 Add to Cart", style=discord.ButtonStyle.green)
        self.view = view
    
    async def callback(self, interaction: Interaction):
        user_id = self.view.user_id
        
        if user_id not in user_carts:
            user_carts[user_id] = []
        
        item = {
            "size": self.view.selected_size,
            "product": self.view.selected_product,
            "quantity": self.view.quantity,
            "unit_price": self.view.selected_price,
            "subtotal": self.view.selected_price * self.view.quantity
        }
        
        user_carts[user_id].append(item)
        
        embed = discord.Embed(
            title="✅ Added to Cart",
            description=f"{self.view.selected_size} {self.view.selected_product}\n"
                       f"Qty: {self.view.quantity} × ${self.view.selected_price} = ${item['subtotal']}",
            color=discord.Color.green()
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ViewCartButton(ui.Button):
    def __init__(self, user_id):
        super().__init__(label="👀 View Cart", style=discord.ButtonStyle.blurple)
        self.user_id = user_id
    
    async def callback(self, interaction: Interaction):
        cart = user_carts.get(self.user_id, [])
        
        if not cart:
            await interaction.response.send_message("Your cart is empty!", ephemeral=True)
            return
        
        cart_lines = []
        total_price = 0
        total_qty = 0
        
        for i, item in enumerate(cart, 1):
            line = f"{i}. {item['size']} {item['product']}\n   Qty: {item['quantity']} × ${item['unit_price']} = ${item['subtotal']}"
            cart_lines.append(line)
            total_price += item['subtotal']
            total_qty += item['quantity']
        
        cart_text = "🛒 **Your Cart:**\n\n" + "\n\n".join(cart_lines)
        cart_text += f"\n\n**TOTAL: ${total_price} ({total_qty} items)**"
        
        view = CheckoutView(self.user_id)
        await interaction.response.send_message(cart_text, view=view, ephemeral=True)

class OrderDetailsModal(ui.Modal, title="訂單詳情 Order Details"):
    phone_number = ui.TextInput(
        label="聯絡人電話 Phone Number (8 digits)",
        placeholder="e.g., 12345678",
        required=True,
        min_length=8,
        max_length=8
    )
    
    pickup_date = ui.TextInput(
        label="取貨日期 Pickup Date (YYYY-MM-DD)",
        placeholder="e.g., 2025-12-25",
        required=True
    )
    
    pickup_time = ui.TextInput(
        label="取貨時間 Pickup Time (Optional)",
        placeholder="e.g., 14:00 或 TBC",
        required=False
    )
    
    delivery_location = ui.TextInput(
        label="交收方式 Delivery Location",
        placeholder="e.g., 尖沙咀 或 荃灣Studio",
        required=True
    )
    
    remark = ui.TextInput(
        label="Remark (Optional)",
        placeholder="Special requests",
        required=False,
        style=discord.TextStyle.paragraph
    )
    
    async def on_submit(self, interaction: Interaction):
        user_order_details[interaction.user.id] = {
            "phone": self.phone_number.value,
            "pickup_date": self.pickup_date.value,
            "pickup_time": self.pickup_time.value or "TBC",
            "delivery_location": self.delivery_location.value,
            "remark": self.remark.value or "無"
        }
        await interaction.response.defer()

class CheckoutView(ui.View):
    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id
    
    @ui.button(label="📅 Add Delivery Details", style=discord.ButtonStyle.blurple)
    async def add_details(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_modal(OrderDetailsModal())
    
    @ui.button(label="✅ Finalize Order", style=discord.ButtonStyle.green)
    async def finalize(self, interaction: Interaction, button: ui.Button):
        cart = user_carts.get(self.user_id, [])
        
        if not cart:
            await interaction.response.send_message("Cart is empty!", ephemeral=True)
            return
        
        details = user_order_details.get(self.user_id, {})
        phone = details.get("phone", "[PHONE]")
        pickup_date = details.get("pickup_date", "[DATE]")
        pickup_time = details.get("pickup_time", "[TIME]")
        delivery_location = details.get("delivery_location", "[LOCATION]")
        remark = details.get("remark", "無")
        
        items_text = []
        total_price = 0
        total_qty = 0
        
        for item in cart:
            items_text.append(f"{item['size']} {item['product']} × {item['quantity']} (${item['unit_price']} each = ${item['subtotal']})")
            total_price += item['subtotal']
            total_qty += item['quantity']
        
        order_text = f"""Thanks for your purchase!✨️ 
【訂單資料】
聯絡人電話： {phone}
訂單內容：
{chr(10).join(items_text)}
總數： ${total_price} （{total_qty}件）
取貨日期： {pickup_date}
取貨時間： {pickup_time}
交收方式： {delivery_location}
Remark： {remark}"""
        
        embed = discord.Embed(
            title="📦 Order Summary",
            description=f"```\n{order_text}\n```",
            color=discord.Color.gold()
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        user_carts[self.user_id] = []
        user_order_details[self.user_id] = {}
    
    @ui.button(label="🗑️ Clear Cart", style=discord.ButtonStyle.red)
    async def clear(self, interaction: Interaction, button: ui.Button):
        user_carts[self.user_id] = []
        await interaction.response.send_message("Cart cleared!", ephemeral=True)

# ========= EVENTS =========
@bot.event
async def on_ready() -> None:
    """Bot startup event."""
    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    load_reminders_from_db()
    load_orders_from_db()
    check_reminders.start()

@bot.event
async def on_message(message: discord.Message) -> None:
    """Handle incoming messages."""
    if message.author == bot.user:
        return

    if "【訂單資料】" in message.content:
        await process_order_message(message)
    
    await bot.process_commands(message)

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
            if dt_pickup > now and REMINDER_CHANNEL_ID > 0:
                channel = bot.get_channel(REMINDER_CHANNEL_ID)
                try:
                    target_user = await bot.fetch_user(TARGET_USER_ID)
                except:
                    target_user = None
                
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

@bot.command(name="d", help="Check order details: !d yymmdd or !d yymm")
async def check_order_details(ctx, date_arg: str) -> None:
    """Check order details (author, phone, location, remark)."""
    try:
        if not validator.validate_date_arg(date_arg):
            await send_reply("❌ Invalid format. Use `!d yymmdd` or `!d yymm`")
            return
        
        if len(date_arg) == 6:
            output = order_service.format_orders_detail(date_arg)
        else:
            output = order_service.format_month_detail(date_arg)
        
        if not output:
            await send_reply(f"❌ No orders found for {date_arg}")
            return
        
        sent = await send_to_cake_channel(output)
        await send_reply("✅ Results sent to #cake" if sent else "❌ #cake not found")
    except Exception as e:
        logger.error(f"Check order details error: {e}")
        await send_reply(f"❌ Error: {str(e)[:100]}")

@bot.command(name="c", help="Check order contents: !c yymmdd or !c yymm")
async def check_order_contents(ctx, date_arg: str) -> None:
    """Check order contents (cakes and quantities)."""
    try:
        if not validator.validate_date_arg(date_arg):
            await send_reply("❌ Invalid format. Use `!c yymmdd` or `!c yymm`")
            return
        
        if len(date_arg) == 6:
            output = order_service.format_orders_content(date_arg)
        else:
            output = order_service.format_month_content(date_arg)
        
        if not output:
            await send_reply(f"❌ No orders found for {date_arg}")
            return
        
        sent = await send_to_cake_channel(output)
        await send_reply("✅ Results sent to #cake" if sent else "❌ #cake not found")
    except Exception as e:
        logger.error(f"Check order contents error: {e}")
        await send_reply(f"❌ Error: {str(e)[:100]}")

@bot.command(name="tdy", help="Show today's orders")
async def show_today_orders(ctx) -> None:
    """Show today's orders."""
    try:
        now = datetime.now(HK_TZ)
        yymmdd = now.strftime("%y%m%d")
        
        output = order_service.format_orders_content(yymmdd)
        if not output:
            await send_reply(f"❌ No orders for today ({now.strftime('%Y-%m-%d')})")
            return
        
        sent = await send_to_cake_channel(output)
        await send_reply("✅ Today's orders sent" if sent else "❌ #cake not found")
    except Exception as e:
        await send_reply(f"❌ Error: {str(e)[:100]}")

# ========= NEW CAKE ORDER COMMAND =========
@bot.tree.command(name="cake_order", description="🎂 Complete Smart Cake Order System - 21 Products!")
async def cake_order(interaction: discord.Interaction):
    """Complete smart order builder with all cake products"""
    try:
        view = OrderBuilderView(interaction.user.id)
        
        embed = discord.Embed(
            title="🎂 Complete Smart Cake Order System",
            description="**21 Products | 4 Sizes | Full Features**\n\n"
                       "🍡 **Mini** ($22-$36) — 8 Petite Cakes\n"
                       "🥐 **3\" French Pastry** ($52-$62) — 4 Cakes\n"
                       "🎂 **6\" Whole Cake** ($388-$438) — 4 Cakes\n"
                       "🍰 **8\" Whole Cake** ($588-$688) — 5 Cakes\n\n"
                       "✨️ Limited editions available!\n\n"
                       "**Select Size → Type → Product → Quantity → Checkout**",
            color=discord.Color.gold()
        )
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
        
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)[:100]}", ephemeral=True)
        logger.error(f"Cake order error: {e}")

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
                        try:
                            target_user = await bot.fetch_user(TARGET_USER_ID)
                        except:
                            target_user = None
                        
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
                        if summary_only and TODAY_REMINDER_CHANNEL_ID > 0:
                            try:
                                second_user = await bot.fetch_user(SECOND_USER_ID)
                                if second_user:
                                    mentions += f" {second_user.mention}"
                            except:
                                pass
                            await send_today_reminder(embed, mentions)
                        elif REMINDER_CHANNEL_ID > 0:
                            channel = bot.get_channel(REMINDER_CHANNEL_ID)
                            if channel:
                                await channel.send(f"{mentions}", embed=embed)

                        r["sent"] = True
                        reminder_service.update_reminder_in_db(user_id, r)
                    except Exception as e:
                        logger.error(f"Reminder send error: {e}")
                        r["sent"] = True
    except Exception as e:
        logger.error(f"Check reminders error: {e}")

# ========= STARTUP =========
if __name__ == "__main__":
    logger.info("🚀 Starting bot...")
    if MONGODB_URI:
        init_mongodb()
    else:
        logger.warning("⚠️ MONGODB_URI not set - running without database")
    
    try:
        keep_alive()
    except:
        pass
    
    bot.run(BOT_TOKEN)
