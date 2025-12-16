import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import re

# ========= 從 Replit Secrets 讀取設定 =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID"))
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID"))
SECOND_USER_ID = int(os.getenv("SECOND_USER_ID"))
BOT_COMMAND_CHANNEL_ID = int(os.getenv("BOT_COMMAND_CHANNEL_ID"))
# ========================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ⭐ 設定時區為香港時間
HK_TZ = pytz.timezone("Asia/Hong_Kong")

reminders = {}


def extract_fields(text: str):
    """
    從原訊息提取：
    - 「取貨日期」後面第一行
    - 「交收方式」後面第一行
    - 「聯絡人電話」後面第一行
    - 「Remark」後面第一行
    """
    pickup = None
    deal = None
    phone = None
    remark = None

    def _after_keyword(s: str, keyword: str):
        if keyword not in s:
            return None
        part = s.split(keyword, 1)[1]
        part = part.lstrip(":： ").strip()
        return part.splitlines()[0].strip() if part else None

    pickup = _after_keyword(text, "取貨日期")
    deal   = _after_keyword(text, "交收方式")
    phone  = _after_keyword(text, "聯絡人電話")
    remark = _after_keyword(text, "Remark")

    return pickup, deal, phone, remark


def parse_pickup_date(pickup_str: str):
    """
    從「取貨日期」欄位解析日期，支援多種格式。
    例如：
    - "2025年12月19日" → 251219
    - "2025-12-19" → 251219
    - "19/12/2025" → 251219
    - "12/19" → 251219（假設當年，月/日）
    - "19/12" → 251219（假設當年，日/月）
    返回：(yymmdd_str, datetime_obj) 或 (None, None)
    """
    if not pickup_str:
        return None, None

    try:
        # 試著匹配 "2025年12月19日" 格式
        match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", pickup_str)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            yy = year % 100
            yymmdd = f"{yy:02d}{month:02d}{day:02d}"
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return yymmdd, dt

        # 試著匹配 "2025-12-19" 格式（年-月-日）
        match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", pickup_str)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            yy = year % 100
            yymmdd = f"{yy:02d}{month:02d}{day:02d}"
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return yymmdd, dt

        # 試著匹配 "19/12/2025" 格式（日/月/年）
        match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", pickup_str)
        if match:
            day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            yy = year % 100
            yymmdd = f"{yy:02d}{month:02d}{day:02d}"
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return yymmdd, dt

        # 試著匹配 "12/19" 或 "19/12" 格式（假設當年）
        # 邏輯：如果第一個數字 > 12，就假設係「日/月」；否則假設「月/日」
        match = re.search(r"(\d{1,2})/(\d{1,2})", pickup_str)
        if match:
            first_num = int(match.group(1))
            second_num = int(match.group(2))
            year = datetime.now(HK_TZ).year
            
            if first_num > 12:
                # 假設係「日/月」格式
                day, month = first_num, second_num
            else:
                # 假設係「月/日」格式
                month, day = first_num, second_num
            
            yy = year % 100
            yymmdd = f"{yy:02d}{month:02d}{day:02d}"
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return yymmdd, dt

    except Exception as e:
        print(f"⚠ 解析日期失敗: {e}")

    return None, None


@bot.event
async def on_ready():
    print(f"✅ 已登入為：{bot.user} (ID: {bot.user.id})")
    check_reminders.start()


# -------- Helper Function：發送回覆到 BOT_COMMAND_CHANNEL --------
async def send_reply(message: str):
    """將所有 bot 回覆發去 bot command channel。"""
    channel = bot.get_channel(BOT_COMMAND_CHANNEL_ID)
    if channel:
        await channel.send(message)
    else:
        print(f"⚠ 找不到 bot command channel (ID: {BOT_COMMAND_CHANNEL_ID})")


# -------- Helper Function：新增提醒 --------
def add_reminder(user_id: int, reminder_time: datetime, message: str, author: str, jump_url: str, 
                 pickup_date: str, deal_method: str, phone: str, remark: str, summary_only: bool):
    """統一新增提醒的 function。"""
    if user_id not in reminders:
        reminders[user_id] = []

    reminders[user_id].append({
        "time": reminder_time,
        "message": message,
        "author": author,
        "jump_url": jump_url,
        "pickup_date": pickup_date,
        "deal_method": deal_method,
        "phone": phone,
        "remark": remark,
        "summary_only": summary_only,
    })


# -------- Helper Function：處理訂單訊息 --------
async def process_order_message(message):
    """
    處理包含【訂單資料】的訊息（新訊息或舊訊息都用呢個）
    """
    full_text = message.content
    pickup, deal, phone, remark = extract_fields(full_text)
    
    yymmdd_pickup, dt_pickup = parse_pickup_date(pickup)
    
    if yymmdd_pickup and dt_pickup:
        user_id = message.author.id
        hk_now = datetime.now(HK_TZ)
        
        # ✅ 設定 !r（2 天前）
        two_days_before = dt_pickup - timedelta(days=2)
        if two_days_before > hk_now:
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
                summary_only=False
            )
            print(f"✅ Auto-set !r for message: {pickup}")
        
        # ✅ 設定 !t（當日）
        if dt_pickup > hk_now:
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
                summary_only=True
            )
            print(f"✅ Auto-set !t for message: {pickup}")


# -------- 自動化：監聽所有訊息，如果包含「【訂單資料】」自動設定提醒 --------
@bot.event
async def on_message(message):
    """
    監聽所有訊息，如果包含「【訂單資料】」就自動設定 !r 和 !t。
    """
    # 忽略 bot 自己的訊息
    if message.author == bot.user:
        await bot.process_commands(message)
        return

    # 檢查訊息是否包含「【訂單資料】」
    if "【訂單資料】" in message.content:
        await process_order_message(message)

    # 處理一般指令
    await bot.process_commands(message)


# -------- 指令 1：!time 小時 分鐘（可選，一般提醒） --------
@bot.command(name="time")
async def set_reminder_time(ctx, hours: int, minutes: int = 0):
    """
    用法：
    1. 先「回覆」你想被提醒的那則訊息
    2. 再輸入：!time 小時 分鐘
       例：!time 2 30  （2 小時 30 分鐘後提醒）
    回覆發到：bot command channel
    """
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!time hours minutes`")
        return

    try:
        hk_now = datetime.now(HK_TZ)
        reminder_time = hk_now + timedelta(hours=hours, minutes=minutes)
        
        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)

        full_text = replied_msg.content
        pickup, deal, phone, remark = extract_fields(full_text)

        user_id = ctx.author.id
        add_reminder(user_id, reminder_time, full_text, str(replied_msg.author), 
                     replied_msg.jump_url, pickup, deal, phone, remark, False)

        await send_reply(
            f"✅ Reminder set for {reminder_time.strftime('%Y-%m-%d %H:%M')}"
        )
    except Exception as e:
        await send_reply(f"❌ Failed to set reminder：{e}")


# -------- 指令 2：!r yymmdd（一般提醒，會出現在 !list） --------
@bot.command(name="r")
async def set_reminder_r(ctx, yymmdd: str):
    """
    用法：
    1. 先「回覆」你想被提醒的那則訊息
    2. 再輸入：!r yymmdd
       例：!r 251217  （代表 2025-12-17）
    預設提醒時間：當日 09:00
    如果距離現在少於 2 天，立即發送提醒
    回覆發到：bot command channel
    """
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!r yymmdd`, e.g. `!r 251217`")
        return

    try:
        date_obj = datetime.strptime(yymmdd, "%y%m%d")
        target_dt = HK_TZ.localize(datetime(
            year=date_obj.year,
            month=date_obj.month,
            day=date_obj.day,
            hour=9,
            minute=0
        ))
    except ValueError:
        await send_reply("❌ Invalid date format. Use `!r 251217` (6-digit format).")
        return

    hk_now = datetime.now(HK_TZ)
    if target_dt <= hk_now:
        await send_reply("❌ The date has already passed. Please use a future date.")
        return

    try:
        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)

        full_text = replied_msg.content
        pickup, deal, phone, remark = extract_fields(full_text)

        user_id = ctx.author.id
        add_reminder(user_id, target_dt, full_text, str(replied_msg.author),
                     replied_msg.jump_url, pickup, deal, phone, remark, False)

        # ⭐ 檢查是否少於 2 天：如果係，立即發送提醒
        time_diff = target_dt - hk_now
        
        # 計算總小時數（更準確）
        total_hours = time_diff.total_seconds() / 3600
        
        print(f"DEBUG: time_diff.days = {time_diff.days}, total_hours = {total_hours}")
        
        if total_hours < 48:  # 少於 48 小時 = 少於 2 天
            # 立即發送提醒
            await send_reply(
                f"⚠️ **Less than 2 days away - Sending reminder immediately**"
            )
            
            target_user = await bot.fetch_user(TARGET_USER_ID)
            
            # 發送 Embed
            channel = bot.get_channel(REMINDER_CHANNEL_ID)
            if channel and target_user:
                embed = discord.Embed(
                    title="⏰ Reminder Time!",
                    description=full_text,
                    color=discord.Color.blue()
                )
                embed.set_author(name=f"From: {replied_msg.author}")
                embed.set_footer(
                    text=f"Time: {target_dt.strftime('%Y-%m-%d %H:%M')}"
                )

                if replied_msg.jump_url:
                    embed.description += f"\n\n[🔗 Original message]({replied_msg.jump_url})"

                await channel.send(f"{target_user.mention} Reminder：", embed=embed)
                
                print(f"DEBUG: Sent reminder immediately")
        else:
            # 正常情況：在設定時間才提醒
            await send_reply(
                f"✅ Reminder set for {target_dt.strftime('%Y-%m-%d %H:%M')}"
            )
            print(f"DEBUG: Scheduled reminder for later")
            
    except Exception as e:
        print(f"ERROR: {e}")
        await send_reply(f"❌ Failed to set reminder：{e}")


# -------- 指令 3：!t yymmdd（今日交收/送貨 摘要提醒） --------
@bot.command(name="t")
async def set_summary_reminder(ctx, yymmdd: str):
    """
    用法：
    1. 先「回覆」你想被提醒的那則訊息
    2. 再輸入：!t yymmdd
       例：!t 251217  （代表 2025-12-17）
    功能：
       嗰日 09:00 發一條摘要提醒，並 Tag 兩個固定用戶
    回覆發到：bot command channel
    """
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!t yymmdd`, e.g. `!t 251217`")
        return

    try:
        date_obj = datetime.strptime(yymmdd, "%y%m%d")
        target_dt = HK_TZ.localize(datetime(
            year=date_obj.year,
            month=date_obj.month,
            day=date_obj.day,
            hour=9,
            minute=0
        ))
    except ValueError:
        await send_reply("❌ Invalid date format. Use `!t 251217` (6-digit format).")
        return

    hk_now = datetime.now(HK_TZ)
    if target_dt <= hk_now:
        await send_reply("❌ The date has already passed. Please use a future date.")
        return

    try:
        replied_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)

        full_text = replied_msg.content
        pickup, deal, phone, remark = extract_fields(full_text)

        user_id = ctx.author.id
        add_reminder(user_id, target_dt, full_text, str(replied_msg.author),
                     replied_msg.jump_url, pickup, deal, phone, remark, True)

        await send_reply(
            f"✅ Summary reminder set for {target_dt.strftime('%Y-%m-%d %H:%M')}"
        )
    except Exception as e:
        await send_reply(f"❌ Failed to set reminder：{e}")


# -------- 指令 4：!list 只列出「非摘要」提醒 --------
@bot.command(name="list")
async def list_reminders(ctx):
    """
    用法：直接打 !list
    功能：列出「你自己」目前所有未來的一般提醒（!r / !time）
    回覆發到：bot command channel
    """
    user_id = ctx.author.id
    hk_now = datetime.now(HK_TZ)

    if user_id not in reminders or len(reminders[user_id]) == 0:
        await send_reply("📭 You have no future reminders.")
        return

    future_reminders = [
        r for r in reminders[user_id]
        if r["time"] > hk_now and not r.get("summary_only", False)
    ]

    if not future_reminders:
        await send_reply("📭 You have no future reminders.")
        return

    future_reminders.sort(key=lambda r: r["time"])

    lines = []
    for idx, r in enumerate(future_reminders, start=1):
        time_str = r["time"].strftime("%Y-%m-%d %H:%M")

        pickup = r.get("pickup_date")
        deal   = r.get("deal_method")
        phone  = r.get("phone")
        remark = r.get("remark")

        info_parts = []
        if pickup:
            info_parts.append(f"Pickup: {pickup}")
        if deal:
            info_parts.append(f"Method: {deal}")
        if phone:
            info_parts.append(f"Phone: {phone}")
        if remark:
            info_parts.append(f"Remark: {remark}")

        if info_parts:
            preview = " ｜ ".join(info_parts)
        else:
            base = r["message"]
            preview = (base[:30] + "…") if len(base) > 30 else base

        line = f"{idx}. {time_str} ｜ {preview}"
        if r.get("jump_url"):
            line += f" ｜ [Original message]({r['jump_url']})"
        lines.append(line)

    text = "📝 **Future Reminder：**\n" + "\n".join(lines)
    await send_reply(text)


# -------- 指令 5：!listtdy 列出「今日所有 !t 摘要提醒」 --------
@bot.command(name="listtdy")
async def list_today_summaries(ctx):
    """
    用法：直接打 !listtdy
    功能：列出「你自己」今天所有用 !t 設定的摘要提醒
    回覆發到：bot command channel
    """
    user_id = ctx.author.id
    hk_now = datetime.now(HK_TZ)

    if user_id not in reminders or len(reminders[user_id]) == 0:
        await send_reply("📭 You have no summary reminders today.")
        return

    today_y = hk_now.year
    today_m = hk_now.month
    today_d = hk_now.day

    today_summaries = []
    for r in reminders[user_id]:
        t = r["time"]
        if (
            r.get("summary_only", False)
            and t.year == today_y
            and t.month == today_m
            and t.day == today_d
            and t >= hk_now
        ):
            today_summaries.append(r)

    if not today_summaries:
        await send_reply("📭 You have no future summary reminders today.")
        return

    today_summaries.sort(key=lambda r: r["time"])

    lines = []
    for idx, r in enumerate(today_summaries, start=1):
        time_str = r["time"].strftime("%H:%M")

        phone  = r.get("phone")
        deal   = r.get("deal_method")
        remark = r.get("remark")

        info_parts = []
        if phone:
            info_parts.append(f"Phone: {phone}")
        if deal:
            info_parts.append(f"Method: {deal}")
        if remark:
            info_parts.append(f"Remark: {remark}")

        preview = " ｜ ".join(info_parts) if info_parts else "(No details)"

        line = f"{idx}. {time_str} ｜ {preview}"
        if r.get("jump_url"):
            line += f" ｜ [Original message]({r['jump_url']})"
        lines.append(line)

    text = "📝 **Today's Summary Reminders：**\n" + "\n".join(lines)
    await send_reply(text)


# -------- 指令 6：!scan days（手動掃描舊訊息） --------
@bot.command(name="scan")
async def scan_old_messages_cmd(ctx, days: int = 7):
    """
    用法：!scan [days]
    功能：掃描過去 N 天的訊息，自動設定【訂單資料】的提醒
    例子：
    - !scan      （掃描過去 7 天）
    - !scan 14   （掃描過去 14 天）
    - !scan 30   （掃描過去 30 天）
    回覆發到：bot command channel
    """
    if days < 1 or days > 365:
        await send_reply("❌ Days must be between 1 and 365.")
        return

    await send_reply(f"🔍 Scanning messages from the past {days} days... This may take a moment.")
    
    try:
        scan_before_date = datetime.now(HK_TZ) - timedelta(days=days)
        count = 0
        
        # 掃描所有頻道
        for channel in ctx.guild.text_channels:
            try:
                print(f"🔍 Scanning channel: {channel.name}")
                
                async for message in channel.history(limit=None, after=scan_before_date):
                    if "【訂單資料】" in message.content and message.author != bot.user:
                        await process_order_message(message)
                        count += 1
                        
            except discord.Forbidden:
                print(f"⚠ No permission to read {channel.name}")
            except Exception as e:
                print(f"⚠ Error scanning {channel.name}: {e}")
        
        await send_reply(f"✅ Scan completed! Found and processed {count} messages with 【訂單資料】")
        
    except Exception as e:
        print(f"ERROR: {e}")
        await send_reply(f"❌ Scan failed：{e}")


# -------- 指令 7：!commands 顯示所有指令同例子 --------
@bot.command(name="commands")
async def show_commands(ctx):
    """
    用法：直接打 !commands
    功能：顯示所有指令同例子
    回覆發到：bot command channel
    """
    help_text = """
📚 **Reminder Bot Commands**

**🤖 Auto Features：**
When message contains 「【訂單資料】」, Bot will automatically set:
- ✅ `!r` reminder (2 days before pickup at 09:00)
- ✅ `!t` summary reminder (on pickup day at 09:00)

**Manual Reminder Commands：**

| Command | Usage | Example | Description |
|---------|-------|---------|-------------|
| `!time` | !time hours minutes | !time 2 30 | Remind after 2h 30m |
| `!r` | !r yymmdd | !r 260101 | Remind on 2026-01-01 09:00 |
| `!t` | !t yymmdd | !t 260101 | Summary reminder on 2026-01-01 09:00 |

**View Reminders：**

| Command | Usage | Description |
|---------|-------|-------------|
| `!list` | !list | View all future reminders (!time / !r) |
| `!listtdy` | !listtdy | View today's summary reminders (!t) |

**Scan Old Messages：**

| Command | Usage | Description |
|---------|-------|-------------|
| `!scan` | !scan [days] | Scan past N days for 【訂單資料】 messages |

**How to use：**
1️⃣ **Reply** to the message you want to be reminded about
2️⃣ Enter the command above
3️⃣ Bot will reply in #bot-command channel

**Auto-extracted info：**
📦 Pickup Date、📍 Delivery Method、📞 Phone、📝 Remark

**Supported date formats：**
- `2025年12月19日`
- `2025-12-19`
- `19/12/2025`
- `12/19`
- `19/12`

**Special：**
⚠️ If `!r` date is less than 2 days away, reminder will be sent immediately!
"""
    await send_reply(help_text)


# -------- 背景任務：每分鐘檢查有冇要提醒 --------
@tasks.loop(minutes=1)
async def check_reminders():
    """每分鐘檢查是否有提醒到時間。"""
    hk_now = datetime.now(HK_TZ)

    for user_id, user_reminders in list(reminders.items()):
        for reminder in user_reminders[:]:
            if hk_now >= reminder["time"]:
                try:
                    channel = bot.get_channel(REMINDER_CHANNEL_ID)
                    target_user = await bot.fetch_user(TARGET_USER_ID)

                    if channel is None:
                        print("⚠ Reminder channel not found. Check REMINDER_CHANNEL_ID.")
                        user_reminders.remove(reminder)
                        continue

                    summary_only = reminder.get("summary_only", False)

                    if summary_only:
                        # 今日交收/送貨 摘要
                        lines = ["Today's Pickup/Delivery："]

                        if reminder.get("phone"):
                            lines.append(f"📞 Phone：{reminder['phone']}")
                        if reminder.get("deal_method"):
                            lines.append(f"📍 Method：{reminder['deal_method']}")
                        if reminder.get("remark"):
                            lines.append(f"📝 Remark：{reminder['remark']}")

                        desc = "\n".join(lines) if len(lines) > 1 else reminder["message"]
                    else:
                        # 一般提醒：原訊息
                        desc = reminder["message"]

                    embed = discord.Embed(
                        title="⏰ Reminder Time!",
                        description=desc,
                        color=discord.Color.blue()
                    )
                    embed.set_author(name=f"From: {reminder['author']}")
                    embed.set_footer(
                        text=f"Time: {reminder['time'].strftime('%Y-%m-%d %H:%M')}"
                    )

                    if reminder.get("jump_url"):
                        embed.description += f"\n\n[🔗 Original message]({reminder['jump_url']})"

                    # 建立 mentions：一般提醒只 Tag TARGET_USER，!t 再多 Tag SECOND_USER_ID
                    second_user = None
                    if summary_only:
                        second_user = await bot.fetch_user(SECOND_USER_ID)

                    mentions = target_user.mention
                    if second_user:
                        mentions += f" {second_user.mention}"

                    await channel.send(f"{mentions} Reminder：", embed=embed)

                    user_reminders.remove(reminder)

                except Exception as e:
                    print(f"Reminder failed: {e}")


bot.run(BOT_TOKEN)
