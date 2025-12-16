import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import re

# Replit keep-alive
from keep_alive import keep_alive

# ========= 從環境變數讀取設定 =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID"))
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID"))
SECOND_USER_ID = int(os.getenv("SECOND_USER_ID"))
BOT_COMMAND_CHANNEL_ID = int(os.getenv("BOT_COMMAND_CHANNEL_ID"))
# =====================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ⭐ 設定香港時間
HK_TZ = pytz.timezone("Asia/Hong_Kong")

# 內存儲存所有提醒
reminders = {}


def extract_fields(text: str):
    """從原訊息中提取取貨日期、交收方式、電話、Remark。"""
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
    deal = _after_keyword(text, "交收方式")
    phone = _after_keyword(text, "聯絡人電話")
    remark = _after_keyword(text, "Remark")

    return pickup, deal, phone, remark


def parse_pickup_date(pickup_str: str):
    """
    從「取貨日期」解析日期，支援：
    - 2025年12月19日
    - 2025-12-19
    - 19/12/2025
    - 12/19  (月/日，當年)
    - 19/12  (日/月，當年)
    回傳 (yymmdd, datetime) 或 (None, None)
    """
    if not pickup_str:
        return None, None

    try:
        # 2025年12月19日
        match = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", pickup_str)
        if match:
            year, month, day = map(int, match.groups())
            yy = year % 100
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return f"{yy:02d}{month:02d}{day:02d}", dt

        # 2025-12-19
        match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", pickup_str)
        if match:
            year, month, day = map(int, match.groups())
            yy = year % 100
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return f"{yy:02d}{month:02d}{day:02d}", dt

        # 19/12/2025 (日/月/年)
        match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", pickup_str)
        if match:
            day, month, year = map(int, match.groups())
            yy = year % 100
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return f"{yy:02d}{month:02d}{day:02d}", dt

        # 12/19 或 19/12 (當年)
        match = re.search(r"(\d{1,2})/(\d{1,2})", pickup_str)
        if match:
            first, second = map(int, match.groups())
            year = datetime.now(HK_TZ).year
            if first > 12:
                day, month = first, second  # 日/月
            else:
                month, day = first, second  # 月/日
            yy = year % 100
            dt = HK_TZ.localize(datetime(year, month, day, 9, 0))
            return f"{yy:02d}{month:02d}{day:02d}", dt

    except Exception as e:
        print(f"⚠ parse_pickup_date error: {e}")

    return None, None


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    check_reminders.start()


async def send_reply(message: str):
    """統一把回覆送到 BOT_COMMAND_CHANNEL。"""
    channel = bot.get_channel(BOT_COMMAND_CHANNEL_ID)
    if channel:
        await channel.send(message)
    else:
        print(f"⚠ BOT_COMMAND_CHANNEL_ID not found: {BOT_COMMAND_CHANNEL_ID}")


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
    """統一建立提醒的結構。"""
    if user_id not in reminders:
        reminders[user_id] = []
    reminders[user_id].append(
        {
            "time": reminder_time,
            "message": message,
            "author": author,
            "jump_url": jump_url,
            "pickup_date": pickup_date,
            "deal_method": deal_method,
            "phone": phone,
            "remark": remark,
            "summary_only": summary_only,
        }
    )


async def process_order_message(message: discord.Message):
    """處理包含【訂單資料】的訊息（新訊息或 !scan 掃描到）。"""
    full_text = message.content
    pickup, deal, phone, remark = extract_fields(full_text)
    yymmdd_pickup, dt_pickup = parse_pickup_date(pickup)

    if not (yymmdd_pickup and dt_pickup):
        await send_reply(
            f"⚠️ Message contains 【訂單資料】 but pickup date not recognized.\n"
            f"   Detected pickup date: {pickup or '(not found)'}"
        )
        return

    user_id = message.author.id
    now = datetime.now(HK_TZ)

    # ---------- 自動 !r（提早兩日） ----------
    two_days_before = dt_pickup - timedelta(days=2)
    if two_days_before > now:
        # 正常情況：提醒時間仍在未來 → 照常加入 reminder
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
            f"✅ Auto-set !r Reminder: {two_days_before.strftime('%Y-%m-%d %H:%M')}\n"
            f"   📅 Pickup: {pickup}"
        )
    else:
        # 特殊情況：已過「兩日前」但仍未到取貨日 → 立即補發提醒
        if dt_pickup > now:
            channel = bot.get_channel(REMINDER_CHANNEL_ID)
            target_user = await bot.fetch_user(TARGET_USER_ID)
            if channel and target_user:
                embed = discord.Embed(
                    title="⏰ Reminder Time! (auto from scan)",
                    description=full_text,
                    color=discord.Color.orange(),
                )
                embed.set_author(name=f"From: {message.author}")
                embed.set_footer(text=f"Pickup: {dt_pickup.strftime('%Y-%m-%d %H:%M')}")
                if message.jump_url:
                    embed.description += f"\n\n[🔗 Original message]({message.jump_url})"
                await channel.send(f"{target_user.mention} Reminder:", embed=embed)
                await send_reply(
                    "⚠️ Scan found order less than 2 days from pickup – "
                    "sent reminder immediately."
                )

    # ---------- 自動 !t（當日摘要提醒） ----------
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
            f"✅ 已設定 !t 當日提醒: {dt_pickup.strftime('%Y-%m-%d %H:%M')}\n"
            f"   📅 Pickup: {pickup}"
        )


@bot.event
async def on_message(message: discord.Message):
    """監聽所有訊息，自動處理【訂單資料】。"""
    if message.author == bot.user:
        await bot.process_commands(message)
        return

    if "【訂單資料】" in message.content:
        await process_order_message(message)

    await bot.process_commands(message)


@bot.command(name="time")
async def set_reminder_time(ctx, hours: int, minutes: int = 0):
    """!time 小時 分鐘：回覆某訊息後設定 X 小時後提醒。"""
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!time hours minutes`.")
        return

    try:
        now = datetime.now(HK_TZ)
        reminder_time = now + timedelta(hours=hours, minutes=minutes)
        replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)

        full_text = replied.content
        pickup, deal, phone, remark = extract_fields(full_text)

        user_id = ctx.author.id
        add_reminder(
            user_id,
            reminder_time,
            full_text,
            str(replied.author),
            replied.jump_url,
            pickup,
            deal,
            phone,
            remark,
            False,
        )

        await send_reply(f"✅ Reminder set for {reminder_time.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        await send_reply(f"❌ Failed to set reminder: {e}")


@bot.command(name="r")
async def set_reminder_r(ctx, yymmdd: str):
    """
    !r yymmdd：例如 !r 251217 → 2025-12-17 09:00
    若距離現在少於 2 日，立即發送提醒。
    """
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!r yymmdd`.")
        return

    try:
        date_obj = datetime.strptime(yymmdd, "%y%m%d")
        target_dt = HK_TZ.localize(
            datetime(date_obj.year, date_obj.month, date_obj.day, 9, 0)
        )
    except ValueError:
        await send_reply("❌ Invalid date format. Use `!r 251217` (6 digits).")
        return

    now = datetime.now(HK_TZ)
    if target_dt <= now:
        await send_reply("❌ The date has already passed. Use a future date.")
        return

    try:
        replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        full_text = replied.content
        pickup, deal, phone, remark = extract_fields(full_text)

        user_id = ctx.author.id
        add_reminder(
            user_id,
            target_dt,
            full_text,
            str(replied.author),
            replied.jump_url,
            pickup,
            deal,
            phone,
            remark,
            False,
        )

        # < 2 days → 即時提醒
        diff = target_dt - now
        hours = diff.total_seconds() / 3600
        if hours < 48:
            await send_reply("⚠️ Less than 2 days away – sending reminder immediately.")
            channel = bot.get_channel(REMINDER_CHANNEL_ID)
            target_user = await bot.fetch_user(TARGET_USER_ID)
            if channel and target_user:
                embed = discord.Embed(
                    title="⏰ Reminder Time!",
                    description=full_text,
                    color=discord.Color.blue(),
                )
                embed.set_author(name=f"From: {replied.author}")
                embed.set_footer(text=f"Time: {target_dt.strftime('%Y-%m-%d %H:%M')}")
                if replied.jump_url:
                    embed.description += f"\n\n[🔗 Original message]({replied.jump_url})"
                await channel.send(f"{target_user.mention} Reminder:", embed=embed)
        else:
            await send_reply(f"✅ Reminder set for {target_dt.strftime('%Y-%m-%d %H:%M')}")
    except Exception as e:
        await send_reply(f"❌ Failed to set reminder: {e}")


@bot.command(name="t")
async def set_summary_reminder(ctx, yymmdd: str):
    """!t yymmdd：當日 09:00 發摘要提醒並 Tag 兩個用戶。"""
    if ctx.message.reference is None:
        await send_reply("❌ Please reply to a message first, then use `!t yymmdd`.")
        return

    try:
        date_obj = datetime.strptime(yymmdd, "%y%m%d")
        target_dt = HK_TZ.localize(
            datetime(date_obj.year, date_obj.month, date_obj.day, 9, 0)
        )
    except ValueError:
        await send_reply("❌ Invalid date format. Use `!t 251217` (6 digits).")
        return

    now = datetime.now(HK_TZ)
    if target_dt <= now:
        await send_reply("❌ The date has already passed. Use a future date.")
        return

    try:
        replied = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        full_text = replied.content
        pickup, deal, phone, remark = extract_fields(full_text)

        user_id = ctx.author.id
        add_reminder(
            user_id,
            target_dt,
            full_text,
            str(replied.author),
            replied.jump_url,
            pickup,
            deal,
            phone,
            remark,
            True,
        )

        await send_reply(
            f"✅ Summary reminder set for {target_dt.strftime('%Y-%m-%d %H:%M')}"
        )
    except Exception as e:
        await send_reply(f"❌ Failed to set summary reminder: {e}")


@bot.command(name="list")
async def list_reminders(ctx):
    """!list：顯示此用戶全部未來的一般提醒（!time / !r）。"""
    user_id = ctx.author.id
    now = datetime.now(HK_TZ)

    if user_id not in reminders or not reminders[user_id]:
        await send_reply("📭 You have no future reminders.")
        return

    future = [
        r
        for r in reminders[user_id]
        if r["time"] > now and not r.get("summary_only", False)
    ]
    if not future:
        await send_reply("📭 You have no future reminders.")
        return

    future.sort(key=lambda r: r["time"])
    lines = []
    for idx, r in enumerate(future, start=1):
        t_str = r["time"].strftime("%Y-%m-%d %H:%M")
        pickup = r.get("pickup_date")
        deal = r.get("deal_method")
        phone = r.get("phone")
        remark = r.get("remark")

        parts = []
        if pickup:
            parts.append(f"Pickup: {pickup}")
        if deal:
            parts.append(f"Method: {deal}")
        if phone:
            parts.append(f"Phone: {phone}")
        if remark:
            parts.append(f"Remark: {remark}")

        if parts:
            preview = " ｜ ".join(parts)
        else:
            base = r["message"]
            preview = base[:30] + "…" if len(base) > 30 else base

        line = f"{idx}. {t_str} ｜ {preview}"
        if r.get("jump_url"):
            line += f" ｜ [Original message]({r['jump_url']})"
        lines.append(line)

    await send_reply("📝 **Future Reminder:**\n" + "\n".join(lines))


@bot.command(name="listtdy")
async def list_today_summaries(ctx):
    """!listtdy：顯示此用戶今天全部 !t 摘要提醒。"""
    user_id = ctx.author.id
    now = datetime.now(HK_TZ)

    if user_id not in reminders or not reminders[user_id]:
        await send_reply("📭 You have no summary reminders today.")
        return

    y, m, d = now.year, now.month, now.day
    today = []
    for r in reminders[user_id]:
        t = r["time"]
        if (
            r.get("summary_only", False)
            and t.year == y
            and t.month == m
            and t.day == d
            and t >= now
        ):
            today.append(r)

    if not today:
        await send_reply("📭 You have no future summary reminders today.")
        return

    today.sort(key=lambda r: r["time"])
    lines = []
    for idx, r in enumerate(today, start=1):
        t_str = r["time"].strftime("%H:%M")
        phone = r.get("phone")
        deal = r.get("deal_method")
        remark = r.get("remark")

        parts = []
        if phone:
            parts.append(f"Phone: {phone}")
        if deal:
            parts.append(f"Method: {deal}")
        if remark:
            parts.append(f"Remark: {remark}")

        preview = " ｜ ".join(parts) if parts else "(No details)"
        line = f"{idx}. {t_str} ｜ {preview}"
        if r.get("jump_url"):
            line += f" ｜ [Original message]({r['jump_url']})"
        lines.append(line)

    await send_reply("📝 **Today's Summary Reminders:**\n" + "\n".join(lines))


@bot.command(name="scan")
async def scan_old_messages_cmd(ctx, days: int = 7):
    """
    !scan [days]：手動掃描過去 N 日所有頻道含【訂單資料】的舊訊息。
    """
    if days < 1 or days > 365:
        await send_reply("❌ Days must be between 1 and 365.")
        return

    await send_reply(
        f"🔍 Scanning messages from the past {days} days... This may take a while."
    )

    try:
        after_dt = datetime.now(HK_TZ) - timedelta(days=days)
        count = 0

        for channel in ctx.guild.text_channels:
            try:
                async for msg in channel.history(limit=None, after=after_dt):
                    if "【訂單資料】" in msg.content and msg.author != bot.user:
                        await process_order_message(msg)
                        count += 1
            except discord.Forbidden:
                print(f"⚠ No permission to read {channel.name}")
            except Exception as e:
                print(f"⚠ Error scanning {channel.name}: {e}")

        await send_reply(
            f"✅ Scan completed. Processed {count} messages with 【訂單資料】."
        )
    except Exception as e:
        await send_reply(f"❌ Scan failed: {e}")


@bot.command(name="commands")
async def show_commands(ctx):
    """!commands：顯示所有指令。"""
    text = """
📚 **Reminder Bot Commands**

Auto:
- Detects 「【訂單資料】」 and auto-sets:
  - !r (2 days before, 09:00)
  - !t (pickup day, 09:00)

Manual:
- `!time h m`  → reply a message, remind after h hours m minutes
- `!r yymmdd` → reply a message, remind on that date 09:00
- `!t yymmdd` → reply a message, summary reminder on that date 09:00

View:
- `!list`    → all future reminders (!time / !r)
- `!listtdy` → today's summary reminders (!t)
- `!scan [d]`→ scan past d days for 【訂單資料】 (default 7)

Special:
- If `!r` date is less than 2 days away, reminder is sent immediately.
"""
    await send_reply(text)


@tasks.loop(minutes=1)
async def check_reminders():
    """每分鐘檢查是否有提醒到時間。"""
    now = datetime.now(HK_TZ)

    for user_id, user_reminders in list(reminders.items()):
        for r in user_reminders[:]:
            if now >= r["time"]:
                try:
                    channel = bot.get_channel(REMINDER_CHANNEL_ID)
                    target_user = await bot.fetch_user(TARGET_USER_ID)
                    if not channel or not target_user:
                        user_reminders.remove(r)
                        continue

                    summary_only = r.get("summary_only", False)
                    if summary_only:
                        lines = ["Today's Pickup/Delivery:"]
                        if r.get("phone"):
                            lines.append(f"📞 Phone: {r['phone']}")
                        if r.get("deal_method"):
                            lines.append(f"📍 Method: {r['deal_method']}")
                        if r.get("remark"):
                            lines.append(f"📝 Remark: {r['remark']}")
                        desc = "\n".join(lines) if len(lines) > 1 else r["message"]
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

                    second_user = None
                    if summary_only:
                        try:
                            second_user = await bot.fetch_user(SECOND_USER_ID)
                        except Exception:
                            second_user = None

                    mentions = target_user.mention
                    if second_user:
                        mentions += f" {second_user.mention}"

                    await channel.send(f"{mentions} Reminder:", embed=embed)
                    user_reminders.remove(r)
                except Exception as e:
                    print(f"Reminder failed: {e}")
                    user_reminders.remove(r)


# 啟動 Replit keep-alive，再啟動 bot
keep_alive()
bot.run(BOT_TOKEN)
