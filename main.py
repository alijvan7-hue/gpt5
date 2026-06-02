import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ContentType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, BOT_USERNAME, LOG_CHANNEL_ID, OWNER_ID, TIMEZONE
from database import db


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("ربات")

router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

bot_username_cache = BOT_USERNAME or ""


class UploadState(StatesGroup):
    waiting_content = State()


class ChannelState(StatesGroup):
    waiting_channel = State()


class AdminState(StatesGroup):
    waiting_admin_id = State()


class BroadcastState(StatesGroup):
    waiting_message = State()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def admin_panel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="آپلود محتوا")],
            [
                KeyboardButton(text="مدیریت عضویت اجباری"),
                KeyboardButton(text="مدیریت ادمین‌ها"),
            ],
            [KeyboardButton(text="آمار"), KeyboardButton(text="گزارش روزانه")],
            [KeyboardButton(text="تنظیمات")],
            [KeyboardButton(text="بازگشت")],
        ],
        resize_keyboard=True,
    )


def back_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="بازگشت")]],
        resize_keyboard=True,
    )


def force_join_keyboard(channels: list[dict[str, Any]], code: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for index, channel in enumerate(channels, start=1):
        link = channel.get("invite_link") or "https://t.me/"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"عضویت در کانال {index}",
                    url=link,
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="عضو شدم ✅", callback_data=f"fj:{code}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def force_join_manage_keyboard() -> InlineKeyboardMarkup:
    enabled = db.is_force_join_enabled()
    toggle_text = "غیرفعال کردن عضویت اجباری" if enabled else "فعال کردن عضویت اجباری"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="افزودن کانال", callback_data="channel:add")],
            [InlineKeyboardButton(text="حذف کانال", callback_data="channel:remove_list")],
            [InlineKeyboardButton(text=toggle_text, callback_data="channel:toggle")],
            [InlineKeyboardButton(text="لیست کانال‌ها", callback_data="channel:list")],
        ]
    )


def admin_manage_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="افزودن ادمین", callback_data="admin:add")],
            [InlineKeyboardButton(text="حذف ادمین", callback_data="admin:remove_list")],
            [InlineKeyboardButton(text="لیست ادمین‌ها", callback_data="admin:list")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ارسال همگانی", callback_data="settings:broadcast")],
        ]
    )


async def send_log(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(LOG_CHANNEL_ID, text)
    except Exception as exc:
        logger.warning("ارسال لاگ ناموفق بود: %s", exc)


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_admin(user_id: int) -> bool:
    return db.is_admin(user_id)


async def ensure_admin(message: Message) -> bool:
    if not message.from_user:
        return False
    if not is_admin(message.from_user.id):
        await message.answer("شما دسترسی ادمین ندارید.")
        return False
    return True


async def ensure_owner_message(message: Message) -> bool:
    if not message.from_user:
        return False
    if not is_owner(message.from_user.id):
        await message.answer("فقط مالک ربات اجازه انجام این کار را دارد.")
        return False
    return True


async def ensure_owner_callback(callback: CallbackQuery) -> bool:
    if not callback.from_user:
        return False
    if not is_owner(callback.from_user.id):
        await callback.answer("فقط مالک ربات اجازه انجام این کار را دارد.", show_alert=True)
        return False
    return True


async def register_user_and_log(bot: Bot, message: Message) -> None:
    if not message.from_user:
        return

    user = message.from_user
    created = db.register_user(
        user_id=user.id,
        first_name=user.full_name or user.first_name or "",
        username=user.username or "",
    )

    if created:
        username = f"@{user.username}" if user.username else "ندارد"
        await send_log(
            bot,
            "کاربر جدید وارد شد\n"
            f"نام: {user.full_name}\n"
            f"آیدی: {user.id}\n"
            f"یوزرنیم: {username}",
        )


async def check_missing_channels(bot: Bot, user_id: int) -> list[dict[str, Any]]:
    if not db.is_force_join_enabled():
        return []

    channels = db.get_active_channels()
    missing: list[dict[str, Any]] = []

    for channel in channels:
        chat_id = channel["chat_id"]
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            status = member.status

            if status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                missing.append(channel)
                continue

            if status == ChatMemberStatus.RESTRICTED:
                if getattr(member, "is_member", True) is False:
                    missing.append(channel)
                    continue

        except Exception:
            missing.append(channel)

    return missing


async def send_force_join_message(message: Message, code: str, missing: list[dict[str, Any]]) -> None:
    await message.answer(
        "❌ هنوز در همه کانال‌ها عضو نشده‌اید.\n"
        "لطفا ابتدا در کانال‌های زیر عضو شوید.",
        reply_markup=force_join_keyboard(missing, code),
    )


async def edit_force_join_message(callback: CallbackQuery, code: str, missing: list[dict[str, Any]]) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(
                "❌ هنوز در همه کانال‌ها عضو نشده‌اید.\n"
                "لطفا ابتدا در کانال‌های زیر عضو شوید.",
                reply_markup=force_join_keyboard(missing, code),
            )
        except TelegramBadRequest:
            await callback.message.answer(
                "❌ هنوز در همه کانال‌ها عضو نشده‌اید.\n"
                "لطفا ابتدا در کانال‌های زیر عضو شوید.",
                reply_markup=force_join_keyboard(missing, code),
            )


async def delete_later(bot: Bot, chat_id: int, message_ids: list[int], seconds: int = 60) -> None:
    await asyncio.sleep(seconds)
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


async def deliver_content(bot: Bot, chat_id: int, user_id: int, code: str) -> None:
    content = db.get_content(code)
    if not content:
        await bot.send_message(chat_id, "این لینک معتبر نیست یا محتوا حذف شده است.")
        return

    sent_message: Message | None = None
    content_type = content["content_type"]

    try:
        if content_type == "text":
            sent_message = await bot.send_message(chat_id, content["text"] or "")
        elif content_type == "photo":
            sent_message = await bot.send_photo(chat_id, content["file_id"])
        elif content_type == "video":
            sent_message = await bot.send_video(chat_id, content["file_id"])
        elif content_type == "audio":
            sent_message = await bot.send_audio(chat_id, content["file_id"])
        elif content_type == "voice":
            sent_message = await bot.send_voice(chat_id, content["file_id"])
        elif content_type == "document":
            sent_message = await bot.send_document(chat_id, content["file_id"])
        elif content_type == "animation":
            sent_message = await bot.send_animation(chat_id, content["file_id"])
        elif content_type == "sticker":
            sent_message = await bot.send_sticker(chat_id, content["file_id"])
        else:
            await bot.send_message(chat_id, "نوع محتوا پشتیبانی نمی‌شود.")
            return
    except TelegramForbiddenError:
        return

    db.record_download(user_id=user_id, code=code)

    info_message = await bot.send_message(
        chat_id,
        "آپلود کننده:\n"
        f"نام ادمین: {content['uploader_name']}\n"
        f"شناسه ادمین: {content['uploader_id']}\n"
        f"تاریخ آپلود: {content['uploaded_at']}",
    )

    countdown_message = await bot.send_message(
        chat_id,
        "⏳ این محتوا تا ۶۰ ثانیه دیگر حذف می‌شود.",
    )

    ids_to_delete = []
    if sent_message:
        ids_to_delete.append(sent_message.message_id)
    ids_to_delete.append(countdown_message.message_id)

    asyncio.create_task(delete_later(bot, chat_id, ids_to_delete, 60))

    user = db.get_user(user_id)
    user_name = user["first_name"] if user else "نامشخص"
    await send_log(
        bot,
        "کاربر فایل دریافت کرد\n"
        f"نام: {user_name}\n"
        f"آیدی: {user_id}\n"
        f"کد فایل: {code}",
    )


async def handle_content_link(bot: Bot, message: Message, code: str) -> None:
    if not message.from_user:
        return

    content = db.get_content(code)
    if not content:
        await message.answer("این لینک معتبر نیست یا محتوا حذف شده است.")
        return

    db.record_link_view()

    missing = await check_missing_channels(bot, message.from_user.id)
    if missing:
        await send_force_join_message(message, code, missing)
        return

    await deliver_content(bot, message.chat.id, message.from_user.id, code)


@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    await register_user_and_log(bot, message)

    args = (command.args or "").strip()
    if args:
        await handle_content_link(bot, message, args.upper())
        return

    if not message.from_user:
        return

    if is_admin(message.from_user.id):
        await message.answer(
            "پنل مدیریت ربات آماده است.",
            reply_markup=admin_panel_keyboard(),
        )
        return

    await message.answer(
        f"سلام {message.from_user.first_name}\n"
        "به ربات آپلودر تریاک خوش اومدی 👋"
    )


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not await ensure_admin(message):
        return

    await message.answer(
        "پنل مدیریت ربات آماده است.",
        reply_markup=admin_panel_keyboard(),
    )


@router.message(F.text == "بازگشت")
async def back_handler(message: Message, state: FSMContext) -> None:
    await state.clear()

    if message.from_user and is_admin(message.from_user.id):
        await message.answer("به پنل مدیریت برگشتید.", reply_markup=admin_panel_keyboard())
    else:
        await message.answer("عملیات لغو شد.")


@router.message(F.text == "آپلود محتوا")
async def upload_button(message: Message, state: FSMContext) -> None:
    if not await ensure_admin(message):
        return

    await state.set_state(UploadState.waiting_content)
    await message.answer(
        "محتوا را ارسال کنید.\n"
        "می‌توانید متن، عکس، ویدیو، صدا، ویس، فایل، GIF یا استیکر بفرستید.",
        reply_markup=back_keyboard(),
    )


@router.message(UploadState.waiting_content)
async def receive_upload(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await state.clear()
        return

    content_type = ""
    file_id = None
    text = None

    if message.content_type == ContentType.TEXT and message.text:
        content_type = "text"
        text = message.text
    elif message.content_type == ContentType.PHOTO and message.photo:
        content_type = "photo"
        file_id = message.photo[-1].file_id
    elif message.content_type == ContentType.VIDEO and message.video:
        content_type = "video"
        file_id = message.video.file_id
    elif message.content_type == ContentType.AUDIO and message.audio:
        content_type = "audio"
        file_id = message.audio.file_id
    elif message.content_type == ContentType.VOICE and message.voice:
        content_type = "voice"
        file_id = message.voice.file_id
    elif message.content_type == ContentType.DOCUMENT and message.document:
        content_type = "document"
        file_id = message.document.file_id
    elif message.content_type == ContentType.ANIMATION and message.animation:
        content_type = "animation"
        file_id = message.animation.file_id
    elif message.content_type == ContentType.STICKER and message.sticker:
        content_type = "sticker"
        file_id = message.sticker.file_id
    else:
        await message.answer("این نوع محتوا پشتیبانی نمی‌شود. لطفا دوباره ارسال کنید.")
        return

    code = db.create_content(
        content_type=content_type,
        file_id=file_id,
        text=text,
        uploader_id=message.from_user.id,
        uploader_name=message.from_user.full_name,
    )

    link = f"https://t.me/{bot_username_cache}?start={code}"

    await state.clear()
    await message.answer(
        "محتوا با موفقیت ذخیره شد.\n"
        f"کد لینک: {code}\n"
        f"لینک:\n{link}",
        reply_markup=admin_panel_keyboard(),
    )

    type_names = {
        "text": "Text",
        "photo": "Photo",
        "video": "Video",
        "audio": "Audio",
        "voice": "Voice",
        "document": "Document",
        "animation": "GIF",
        "sticker": "Sticker",
    }

    await send_log(
        bot,
        "ادمین فایل جدید آپلود کرد\n"
        f"نام ادمین: {message.from_user.full_name}\n"
        f"نوع فایل: {type_names.get(content_type, content_type)}\n"
        f"کد لینک: {code}",
    )


@router.message(F.text == "مدیریت عضویت اجباری")
async def force_join_button(message: Message) -> None:
    if not await ensure_admin(message):
        return

    if not await ensure_owner_message(message):
        return

    status = "فعال" if db.is_force_join_enabled() else "غیرفعال"
    await message.answer(
        f"مدیریت عضویت اجباری\nوضعیت فعلی: {status}",
        reply_markup=force_join_manage_keyboard(),
    )


@router.callback_query(F.data == "channel:add")
async def channel_add_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_owner_callback(callback):
        return

    await state.set_state(ChannelState.waiting_channel)
    if callback.message:
        await callback.message.answer(
            "آیدی عددی کانال یا یوزرنیم کانال را ارسال کنید.\n"
            "نمونه:\n"
            "@mychannel\n"
            "-1001234567890\n\n"
            "ربات باید داخل کانال باشد و برای کانال خصوصی باید اجازه ساخت لینک دعوت داشته باشد.",
            reply_markup=back_keyboard(),
        )
    await callback.answer()


@router.message(ChannelState.waiting_channel)
async def receive_channel(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await state.clear()
        return

    raw = (message.text or "").strip()
    if not raw:
        await message.answer("مقدار وارد شده درست نیست.")
        return

    chat_ref: str | int = raw
    if raw.startswith("-100") or raw.lstrip("-").isdigit():
        chat_ref = int(raw)

    try:
        chat = await bot.get_chat(chat_ref)
    except Exception:
        await message.answer("کانال پیدا نشد. مطمئن شوید ربات داخل کانال است.")
        return

    invite_link = ""
    if getattr(chat, "username", None):
        invite_link = f"https://t.me/{chat.username}"
    else:
        try:
            new_link = await bot.create_chat_invite_link(
                chat_id=chat.id,
                name="لینک عضویت ربات",
            )
            invite_link = new_link.invite_link
        except Exception:
            invite_link = getattr(chat, "invite_link", "") or ""

    if not invite_link:
        await message.answer(
            "برای این کانال لینک عضویت ساخته نشد. "
            "ربات را ادمین کنید یا کانال را عمومی کنید."
        )
        return

    db.add_channel(
        chat_id=str(chat.id),
        title=chat.title or str(chat.id),
        invite_link=invite_link,
    )

    await state.clear()
    await message.answer("کانال با موفقیت اضافه شد.", reply_markup=admin_panel_keyboard())

    await send_log(
        bot,
        "عضویت اجباری تغییر کرد\n"
        f"کانال اضافه شد: {chat.title or chat.id}",
    )


@router.callback_query(F.data == "channel:toggle")
async def channel_toggle_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not await ensure_owner_callback(callback):
        return

    new_value = not db.is_force_join_enabled()
    db.set_force_join_enabled(new_value)
    status = "فعال شد" if new_value else "غیرفعال شد"

    if callback.message:
        await callback.message.edit_text(
            f"عضویت اجباری {status}.",
            reply_markup=force_join_manage_keyboard(),
        )

    await send_log(bot, f"عضویت اجباری تغییر کرد\nوضعیت جدید: {status}")
    await callback.answer()


@router.callback_query(F.data == "channel:list")
async def channel_list_callback(callback: CallbackQuery) -> None:
    if not await ensure_owner_callback(callback):
        return

    channels = db.get_channels()
    if not channels:
        text = "هیچ کانالی ثبت نشده است."
    else:
        lines = ["لیست کانال‌ها:"]
        for ch in channels:
            status = "فعال" if ch["active"] else "غیرفعال"
            lines.append(f"{ch['id']}. {ch['title']} | {ch['chat_id']} | {status}")
        text = "\n".join(lines)

    if callback.message:
        await callback.message.answer(text)
    await callback.answer()


@router.callback_query(F.data == "channel:remove_list")
async def channel_remove_list_callback(callback: CallbackQuery) -> None:
    if not await ensure_owner_callback(callback):
        return

    channels = db.get_channels()
    if not channels:
        if callback.message:
            await callback.message.answer("هیچ کانالی برای حذف وجود ندارد.")
        await callback.answer()
        return

    rows = []
    for ch in channels:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"حذف {ch['title']}",
                    callback_data=f"channel:delete:{ch['id']}",
                )
            ]
        )

    if callback.message:
        await callback.message.answer(
            "کانال مورد نظر برای حذف را انتخاب کنید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("channel:delete:"))
async def channel_delete_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not await ensure_owner_callback(callback):
        return

    channel_id = int(callback.data.split(":")[-1])
    db.remove_channel(channel_id)

    if callback.message:
        await callback.message.edit_text("کانال حذف شد.")

    await send_log(bot, "عضویت اجباری تغییر کرد\nیک کانال حذف شد.")
    await callback.answer()


@router.message(F.text == "مدیریت ادمین‌ها")
async def admins_button(message: Message) -> None:
    if not await ensure_admin(message):
        return

    if not await ensure_owner_message(message):
        return

    await message.answer("مدیریت ادمین‌ها", reply_markup=admin_manage_keyboard())


@router.callback_query(F.data == "admin:add")
async def admin_add_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_owner_callback(callback):
        return

    await state.set_state(AdminState.waiting_admin_id)
    if callback.message:
        await callback.message.answer(
            "آیدی عددی کاربر را برای ادمین شدن ارسال کنید.",
            reply_markup=back_keyboard(),
        )
    await callback.answer()


@router.message(AdminState.waiting_admin_id)
async def receive_admin_id(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await state.clear()
        return

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("آیدی باید عددی باشد.")
        return

    admin_id = int(raw)
    user = db.get_user(admin_id)
    first_name = user["first_name"] if user else "ادمین"

    db.add_admin(
        user_id=admin_id,
        first_name=first_name,
        added_by=message.from_user.id,
    )

    await state.clear()
    await message.answer("ادمین جدید اضافه شد.", reply_markup=admin_panel_keyboard())

    await send_log(
        bot,
        "ادمین جدید اضافه شد\n"
        f"آیدی ادمین: {admin_id}",
    )


@router.callback_query(F.data == "admin:list")
async def admin_list_callback(callback: CallbackQuery) -> None:
    if not await ensure_owner_callback(callback):
        return

    admins = db.get_admins()
    lines = ["لیست ادمین‌ها:"]
    for admin in admins:
        owner_mark = "مالک" if admin["user_id"] == OWNER_ID else "ادمین"
        lines.append(f"{admin['user_id']} | {admin['first_name']} | {owner_mark}")

    if callback.message:
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "admin:remove_list")
async def admin_remove_list_callback(callback: CallbackQuery) -> None:
    if not await ensure_owner_callback(callback):
        return

    admins = [a for a in db.get_admins() if a["user_id"] != OWNER_ID]
    if not admins:
        if callback.message:
            await callback.message.answer("ادمین قابل حذف وجود ندارد.")
        await callback.answer()
        return

    rows = []
    for admin in admins:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"حذف {admin['first_name']} - {admin['user_id']}",
                    callback_data=f"admin:delete:{admin['user_id']}",
                )
            ]
        )

    if callback.message:
        await callback.message.answer(
            "ادمین مورد نظر برای حذف را انتخاب کنید.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:delete:"))
async def admin_delete_callback(callback: CallbackQuery) -> None:
    if not await ensure_owner_callback(callback):
        return

    admin_id = int(callback.data.split(":")[-1])
    if admin_id == OWNER_ID:
        await callback.answer("مالک قابل حذف نیست.", show_alert=True)
        return

    db.remove_admin(admin_id)

    if callback.message:
        await callback.message.edit_text("ادمین حذف شد.")
    await callback.answer()


@router.message(F.text == "آمار")
async def statistics_button(message: Message) -> None:
    if not await ensure_admin(message):
        return

    stats = db.get_statistics_summary()
    await message.answer(
        "آمار ربات\n"
        f"کل کاربران: {stats['total_users']}\n"
        f"کل آپلودها: {stats['total_uploads']}\n"
        f"کل دانلودها: {stats['total_downloads']}\n"
        f"کل ادمین‌ها: {stats['total_admins']}\n"
        f"کل کانال‌های عضویت اجباری: {stats['total_channels']}\n"
        f"کاربران امروز: {stats['daily_users']}\n"
        f"دانلودهای امروز: {stats['daily_downloads']}"
    )


def make_daily_report_text() -> str:
    report = db.get_daily_report()
    return (
        "📊 گزارش روزانه\n"
        f"کاربران جدید: {report['new_users']}\n"
        f"کل کاربران: {report['total_users']}\n"
        f"دانلودها: {report['downloads']}\n"
        f"آپلودهای جدید: {report['uploads']}\n"
        f"عضویت‌های موفق: {report['successful_joins']}\n"
        f"بازدید لینک‌ها: {report['link_views']}"
    )


@router.message(F.text == "گزارش روزانه")
async def daily_report_button(message: Message) -> None:
    if not await ensure_admin(message):
        return

    await message.answer(make_daily_report_text())


@router.message(F.text == "تنظیمات")
async def settings_button(message: Message) -> None:
    if not await ensure_admin(message):
        return

    await message.answer("تنظیمات ربات", reply_markup=settings_keyboard())


@router.callback_query(F.data == "settings:broadcast")
async def broadcast_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await ensure_owner_callback(callback):
        return

    await state.set_state(BroadcastState.waiting_message)
    if callback.message:
        await callback.message.answer(
            "پیام همگانی را ارسال کنید.\n"
            "همان پیام برای همه کاربران کپی می‌شود.",
            reply_markup=back_keyboard(),
        )
    await callback.answer()


@router.message(BroadcastState.waiting_message)
async def receive_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await state.clear()
        return

    users = db.get_all_users()
    sent = 0
    failed = 0

    await message.answer("ارسال همگانی شروع شد. لطفا صبر کنید.")

    for user in users:
        try:
            await bot.copy_message(
                chat_id=user["user_id"],
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1

    await state.clear()
    await message.answer(
        "ارسال همگانی انجام شد.\n"
        f"ارسال موفق: {sent}\n"
        f"ناموفق: {failed}",
        reply_markup=admin_panel_keyboard(),
    )

    await send_log(
        bot,
        "ارسال همگانی انجام شد\n"
        f"ارسال موفق: {sent}\n"
        f"ناموفق: {failed}",
    )


@router.callback_query(F.data.startswith("fj:"))
async def force_join_check_callback(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.from_user:
        return

    code = callback.data.split(":", 1)[1].upper()
    content = db.get_content(code)
    if not content:
        await callback.answer("لینک معتبر نیست.", show_alert=True)
        return

    missing = await check_missing_channels(bot, callback.from_user.id)
    if missing:
        await edit_force_join_message(callback, code, missing)
        await callback.answer("هنوز عضویت کامل نیست.", show_alert=True)
        return

    db.record_successful_join()

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await deliver_content(bot, callback.message.chat.id, callback.from_user.id, code)

    await callback.answer()


async def scheduled_daily_report(bot: Bot) -> None:
    try:
        await bot.send_message(LOG_CHANNEL_ID, make_daily_report_text())
        logger.info("گزارش روزانه ارسال شد.")
    except Exception as exc:
        logger.warning("ارسال گزارش روزانه ناموفق بود: %s", exc)


async def scheduled_backup(bot: Bot) -> None:
    try:
        backup_path = db.create_backup()
        await bot.send_document(
            LOG_CHANNEL_ID,
            FSInputFile(backup_path),
            caption=f"پشتیبان پایگاه داده\nتاریخ: {now_text()}",
        )
        logger.info("پشتیبان پایگاه داده ارسال شد.")
    except Exception as exc:
        logger.warning("ارسال پشتیبان ناموفق بود: %s", exc)


async def on_startup(bot: Bot) -> None:
    global bot_username_cache

    db.init_database()
    me = await bot.get_me()
    bot_username_cache = me.username or bot_username_cache

    logger.info("ربات با موفقیت روشن شد.")
    await send_log(
        bot,
        "ربات روشن شد\n"
        f"یوزرنیم ربات: @{bot_username_cache}\n"
        f"زمان: {now_text()}",
    )

    scheduler.add_job(
        scheduled_daily_report,
        trigger="interval",
        hours=24,
        args=[bot],
        next_run_time=datetime.now() + timedelta(hours=24),
        id="daily_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        scheduled_backup,
        trigger="interval",
        hours=24,
        args=[bot],
        next_run_time=datetime.now() + timedelta(hours=24),
        id="daily_backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("توکن ربات تنظیم نشده است.")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await on_startup(bot)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
