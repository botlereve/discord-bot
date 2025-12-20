import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import re
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

# ========= 環境變數 =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID", "0"))
TODAY_REMINDER_CHANNEL_ID = int(os.getenv("TODAY_REMINDER_CHANNEL_ID", "0"))
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))
SECOND_USER_ID = int(os.getenv("SECOND_USER_ID", "0"))
BOT_COMMAND_CHANNEL_ID = int(os.getenv("BOT_COMMAND_CHANNEL_ID", "0"))
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

# 內存 cache
reminders = {}
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
    """解析取貨日期，支援多種格式。"""
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

def parse_order_content(text: str):
    """
    Extract items from order content.
    Improved to handle WhatsApp *Bold* and implicit quantities.
    """
    if "訂單內容" not in text:
        return []

    content_part = text.split("訂單內容")[1]

    # --- FIX: Remove WhatsApp asterisks (*) immediately ---
    content_part = content_part.replace("*", "")

    # Stop at next section (Total, Date, Method)
    for keyword in ["總數", "取貨日期", "交收方式"]:
        if keyword in content_part:
            content_part = content_part.split(keyword)[0]

    content_part = content_part.strip()
    items = []

    # Process line by line
    lines = content_part.split('\n')

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Filter out separator lines or symbols
        # If line is just "---" or "===" or "【】", skip it
        if all(c in '-=*_ []【】' for c in line):
            continue

        # Detect Quantity
        # Looks for x 1, × 1, * 1 at the end of the line
        qty = 1
        product = line

        # Regex: match x/×/* followed by digits at the very end
        match = re.search(r'[\s×x\*]+(\d+)$', line, re.IGNORECASE)

        if match:
            try:
                qty_str = match.group(1)
                qty = int(qty_str)
                # Remove the " x 1" part from the product name
                product = line[:match.start()].strip()
            except:
                qty = 1

        # Clean up Product Name
        # Remove leading bullets (*, -) or numbers (1.)
        product = product.lstrip('*-•1234567890. ')

        # Final check to ensure it's a real product
        if len(product) > 1:
            items.append(f"{product} × {qty}")

    return items

def consolidate_items(items_list):
    """Consolidate duplicate items."""
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

# ---------- 事件 / 指令 ----------

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

    # SYNC SLASH COMMANDS
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"❌ Failed to sync slash commands: {e}")

    load_reminders_from_db()
    load_orders_from_db()
    check_reminders.start()

@bot.event
async def on_message(message: discord.Message):
    """所有新訊息：如果有【訂單資料】，自動設定提醒 + 儲存訂單。"""
    if message.author == bot.user:
        return

    if "【訂單資料】" in message.content:
        await process_order_message(message)

    await bot.process_commands(message)

async def process_order_message(message: discord.Message):
    """自動幫【訂單資料】設定 2 日前 + 當日提醒，並儲存訂單。"""
    full_text = message.content
    pickup, deal, phone, remark = extract_fields(full_text)
    dt_pickup, yymmdd = parse_pickup_date(pickup)

    if not dt_pickup:
        await send_reply(
            f"⚠️ Found 【訂單資料】 but pickup date not recognized.\n"
            f" Detected pickup: {pickup or '(not found)'}"
        )
        return

    # 儲存訂單
    add_order(
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

    # 2 日前提醒（自動化）
    two_days_before = dt_pickup - timedelta(days=2)

    if two_days_before > now:
        add_reminder(
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
        add_reminder(
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

@bot.command(name="time")
async def set_reminder_time(ctx, hours: int, minutes: int = 0):
    """!time h m：reply 一條訊息，設定 h 小時 m 分鐘後提醒一次。"""
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!time h m`.")
        return

    try:
        now = datetime.now(HK_TZ)
        reminder_time = now + timedelta(hours=hours, minutes=minutes)

        replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        full_text = replied.content
        pickup, deal, phone, remark = extract_fields(full_text)

        user_id = ctx.author.id

        add_reminder(
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

@bot.command(name="tdy")
async def show_today_orders(ctx):
    """!tdy：顯示今日嘅所有訂單詳情（author, phone, method, remark）。"""
    try:
        now = datetime.now(HK_TZ)
        yymmdd = now.strftime("%y%m%d")
        orders = orders_cache.get(yymmdd, [])

        if not orders:
            await send_reply(f"❌ No orders for today ({now.strftime('%Y-%m-%d')}).")
            return

        msg_lines = [f"📋 **Today's Orders** ({now.strftime('%Y-%m-%d')}) - Total: {len(orders)}"]
        msg_lines.append("=" * 60)

        for i, order in enumerate(orders, 1):
            msg_lines.append(f"\n**#{i}**")
            msg_lines.append(f" Author: {order['author']}")
            msg_lines.append(f" Phone: {order['phone'] or 'N/A'}")
            msg_lines.append(f" Method: {order['deal_method'] or 'N/A'}")
            msg_lines.append(f" Remark: {order['remark'] or 'N/A'}")
            if order["jump_url"]:
                msg_lines.append(f" [View Message]({order['jump_url']})")

        full_text = "\n".join(msg_lines)

        # Send to TODAY_REMINDER_CHANNEL_ID
        if TODAY_REMINDER_CHANNEL_ID > 0:
            channel = bot.get_channel(TODAY_REMINDER_CHANNEL_ID)
            if channel:
                for chunk in [full_text[i:i+1990] for i in range(0, len(full_text), 1990)]:
                    await channel.send(chunk)
                await send_reply("✅ Today's orders sent.")
            else:
                await send_reply("❌ TODAY_REMINDER_CHANNEL_ID not found.")
        else:
            await send_reply("❌ TODAY_REMINDER_CHANNEL_ID not set.")

    except Exception as e:
        await send_reply(f"❌ Failed to show today's orders: {e}")

@bot.command(name="d")
async def check_order_details(ctx, date_arg: str):
    """
    !d yymmdd：顯示指定日期嘅訂單詳情（author, phone, method, remark）
    !d yymm：顯示指定月份嘅訂單詳情
    Results sent to #reminders channel
    """
    try:
        if len(date_arg) not in [4, 6]:
            await send_reply("❌ Invalid format. Use `!d yymmdd` or `!d yymm`.")
            return

        if len(date_arg) == 4:
            # !d yymm - month
            yymm = date_arg
            matching = {k: v for k, v in orders_cache.items() if k.startswith(yymm)}

            if not matching:
                await send_reply(f"❌ No orders found for {yymm}.")
                return

            msg_lines = [f"📋 **Orders for {yymm}**"]
            msg_lines.append("=" * 60)

            for yymmdd in sorted(matching.keys()):
                orders = matching[yymmdd]
                try:
                    dt = datetime.strptime(yymmdd, "%y%m%d")
                    date_str = dt.strftime("%Y-%m-%d")
                except:
                    date_str = yymmdd

                msg_lines.append(f"\n**📅 {date_str}** ({len(orders)} orders)")

                for i, order in enumerate(orders, 1):
                    msg_lines.append(f" #{i} - {order['author']} | 📞 {order['phone'] or 'N/A'} | 📍 {order['deal_method'] or 'N/A'}")
                    if order["remark"]:
                        msg_lines.append(f" 📝 {order['remark']}")

        else:  # len == 6
            # !d yymmdd - specific date
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
            msg_lines.append("=" * 60)

            for i, order in enumerate(orders, 1):
                msg_lines.append(f"\n**#{i}**")
                msg_lines.append(f"👤 Author: {order['author']}")
                msg_lines.append(f"📞 Phone: {order['phone'] or 'N/A'}")
                msg_lines.append(f"📍 Method: {order['deal_method'] or 'N/A'}")
                msg_lines.append(f"📝 Remark: {order['remark'] or 'N/A'}")
                if order["jump_url"]:
                    msg_lines.append(f"🔗 [View Message]({order['jump_url']})")

        # Send to REMINDER_CHANNEL
        full_text = "\n".join(msg_lines)

        if REMINDER_CHANNEL_ID > 0:
            channel = bot.get_channel(REMINDER_CHANNEL_ID)
            if channel:
                for chunk in [full_text[i:i+1990] for i in range(0, len(full_text), 1990)]:
                    await channel.send(chunk)
                await send_reply("✅ Results sent to #reminders.")
            else:
                await send_reply("❌ #reminders channel not found.")
        else:
            await send_reply("❌ REMINDER_CHANNEL_ID not set.")

    except Exception as e:
        await send_reply(f"❌ Failed to check orders: {e}")

@bot.command(name="c")
async def check_order_contents(ctx, date_arg: str):
    """
    !c yymmdd：顯示指定日期嘅訂單內容及所需數量
    !c yymm：顯示指定月份嘅訂單內容及所需數量
    """
    try:
        if len(date_arg) not in [4, 6]:
            await send_reply("❌ Invalid format. Use `!c yymmdd` or `!c yymm`.")
            return

        if len(date_arg) == 4:
            # !c yymm - month
            yymm = date_arg
            matching = {k: v for k, v in orders_cache.items() if k.startswith(yymm)}

            if not matching:
                await send_reply(f"❌ No orders found for {yymm}.")
                return

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
                    items = parse_order_content(order["full_message"])
                    consolidated = consolidate_items(items)

                    for product, qty in consolidated.items():
                        daily_items[product] = daily_items.get(product, 0) + qty
                        total_all_items[product] = total_all_items.get(product, 0) + qty

                msg_lines.append(
                    f"\n📅 **{date_str}** (Total: {sum(daily_items.values())} 件)"
                )

                for product, qty in sorted(daily_items.items()):
                    msg_lines.append(f" - {product} × {qty}")

            msg_lines.append("\n" + "=" * 60)
            msg_lines.append(f"**Month Total: {sum(total_all_items.values())} 件**")

            for product, qty in sorted(total_all_items.items()):
                msg_lines.append(f" - {product} × {qty}")

        else:  # len == 6
            # !c yymmdd - specific date
            yymmdd = date_arg
            orders = orders_cache.get(yymmdd, [])

            if not orders:
                try:
                    dt = datetime.strptime(yymmdd, "%y%m%d")
                    date_str = dt.strftime("%Y年%m月%d日")
                except:
                    date_str = yymmdd

                await send_reply(f"❌ No orders found for {date_str}.")
                return

            try:
                dt = datetime.strptime(yymmdd, "%y%m%d")
                date_str = dt.strftime("%Y年%m月%d日")
            except:
                date_str = yymmdd

            msg_lines = [f"📋 **Orders for {date_str}**"]

            all_items = {}

            for order in orders:
                items = parse_order_content(order["full_message"])
                consolidated = consolidate_items(items)

                for product, qty in consolidated.items():
                    all_items[product] = all_items.get(product, 0) + qty

            for product, qty in sorted(all_items.items()):
                msg_lines.append(f"- {product} × {qty}")

            msg_lines.append("")
            msg_lines.append("=" * 60)
            msg_lines.append(f"**總數： {sum(all_items.values())}件**")

        # Send to #cake channel
        full_text = "\n".join(msg_lines)

        for guild in bot.guilds:
            for channel in guild.text_channels:
                if channel.name.lower() == "cake":
                    for chunk in [full_text[i:i+1990] for i in range(0, len(full_text), 1990)]:
                        await channel.send(chunk)
                    await send_reply("✅ Order contents sent to #cake.")
                    return

        await send_reply("❌ #cake channel not found.")

    except Exception as e:
        await send_reply(f"❌ Failed to check orders: {e}")

@bot.tree.command(
    name="cake_order",
    description="🎂 Interactive Cake Ordering System"
)
async def cake_order(interaction: discord.Interaction):
    """Open cake ordering interface"""
    try:
        embed = discord.Embed(
            title="🎂 Cake Order System",
            description="Select Size → Type → Product → Add to Cart",
            color=discord.Color.gold()
        )

        embed.add_field(name="Coming Soon", value="Interface will be added soon!")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=False
        )

    except Exception as e:
        await interaction.response.send_message(
            f"❌ Error: {str(e)[:100]}",
            ephemeral=True
        )

    print(f"Cake order error: {e}")

# ---------- 定時檢查 ----------

@tasks.loop(minutes=1)
async def check_reminders():
    """每分鐘檢查有冇到時間嘅提醒。"""
    now = datetime.now(HK_TZ)

    for user_id, user_rems in list(reminders.items()):
        for r in user_rems[:]:
            if now >= r["time"] and not r.get("sent", False):
                try:
                    target_user = await bot.fetch_user(TARGET_USER_ID)

                    if not target_user:
                        r["sent"] = True
                        update_reminder_in_db(user_id, r)
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
                        if TODAY_REMINDER_CHANNEL_ID > 0:
                            channel = bot.get_channel(TODAY_REMINDER_CHANNEL_ID)
                            if channel:
                                await channel.send(f"{mentions}", embed=embed)
                    else:
                        channel = bot.get_channel(REMINDER_CHANNEL_ID)
                        if channel:
                            await channel.send(f"{mentions} Reminder:", embed=embed)

                    r["sent"] = True
                    update_reminder_in_db(user_id, r)

                except Exception as e:
                    print(f"Reminder failed: {e}")
                    r["sent"] = True
                    update_reminder_in_db(user_id, r)

# ---------- 啟動 ----------

bot.run(BOT_TOKEN)
