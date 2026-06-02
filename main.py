import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ContentType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramBadRequest
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
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import BOT_TOKEN, DATABASE_PATH, LOG_CHANNEL_ID, OWNER_ID
from database import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

db = DatabaseManager(DATABASE_PATH)
router = Router()

BOT_USERNAME = ""


class AdminStates(StatesGroup):
    waiting_upload = State()
    waiting_channel = State()
    waiting_admin = State()
    waiting_broadcast = State()


def now_text() -> str:
    return datetime.now().strftime("%Y/%m/%d %H:%M")


def user_name(user: Any) -> str:
    if not user:
        return "نامشخص"
    return user.full_name or user.first_name or "نامشخص"


def username_text(username: str | None) -> str:
    return f"@{username}" if username else "ندارد"


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="آپلود محتوا", callback_data="admin:upload")
    builder.button(text="مدیریت عضویت اجباری", callback_data="admin:force_join")
    builder.button(text="مدیریت ادمین‌ها", callback_data="admin:admins")
    builder.button(text="آمار", callback_data="admin:stats")
    builder.button(text="گزارش روزانه", callback_data="admin:daily_report")
    builder.button(text="تنظیمات", callback_data="admin:settings")
    builder.adjust(1)
    return builder.as_markup()


def back_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="بازگشت", callback_data="admin:panel")
    return builder.as_markup()


def force_join_menu_keyboard() -> InlineKeyboardMarkup:
    enabled = db.get_force_join_enabled()
    builder = InlineKeyboardBuilder()
    if enabled:
        builder.button(text="غیرفعال کردن عضویت اجباری", callback_data="fj:disable")
    else:
        builder.button(text="فعال کردن عضویت اجباری", callback_data="fj:enable")
    builder.button(text="افزودن کانال", callback_data="fj:add")
    builder.button(text="حذف کانال", callback_data="fj:remove_menu")
    builder.button(text="بازگشت", callback_data="admin:panel")
    builder.adjust(1)
    return builder.as_markup()


def admins_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="افزودن ادمین", callback_data="adm:add")
    builder.button(text="حذف ادمین", callback_data="adm:remove_menu")
    builder.button(text="بازگشت", callback_data="admin:panel")
    builder.adjust(1)
    return builder.as_markup()


def settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="ارسال همگانی", callback_data="set:broadcast")
    builder.button(text="گرفتن بکاپ", callback_data="set:backup")
    builder.button(text="بازگشت", callback_data="admin:panel")
    builder.adjust(1)
    return builder.as_markup()


async def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or db.is_admin(user_id)


async def send_log(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(LOG_CHANNEL_ID, text)
    except TelegramAPIError as e:
        logging.error("خطا در ارسال گزارش به کانال گزارش: %s", e)


async def log_new_user(bot: Bot, message: Message) -> None:
    user = message.from_user
    if not user:
        return
    text = (
        "کاربر جدید وارد شد\n\n"
        f"نام: {user_name(user)}\n"
        f"آیدی: {user.id}\n"
        f"یوزرنیم: {username_text(user.username)}\n\n"
        "----------------"
    )
    await send_log(bot, text)


async def check_missing_channels(bot: Bot, user_id: int) -> list[dict[str, Any]]:
    if not db.get_force_join_enabled():
        return []

    channels = db.get_channels()
    missing: list[dict[str, Any]] = []

    for channel in channels:
        chat_id = channel["chat_id"]
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status not in {
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            }:
                missing.append(channel)
        except TelegramAPIError:
            missing.append(channel)

    return missing


def missing_channels_keyboard(channels: list[dict[str, Any]], code: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    for index, channel in enumerate(channels, start=1):
        url = channel.get("url") or ""
        if url:
            builder.button(text=f"عضویت در کانال {index}", url=url)

    callback_code = code if code else "none"
    builder.button(text="عضو شدم ✅", callback_data=f"join_check:{callback_code}")
    builder.adjust(1)
    return builder.as_markup()


async def show_force_join_message(
    target: Message | CallbackQuery,
    channels: list[dict[str, Any]],
    code: str | None,
) -> None:
    text = (
        "❌ هنوز در همه کانال‌ها عضو نشده‌اید.\n\n"
        "لطفا ابتدا در کانال‌های زیر عضو شوید:"
    )
    keyboard = missing_channels_keyboard(channels, code)

    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text(text, reply_markup=keyboard)
        await target.answer()
    else:
        await target.answer(text, reply_markup=keyboard)


async def ask_mandatory_seen(message: Message | CallbackQuery, code: str) -> None:
    builder = InlineKeyboardBuilder()
    builder.button(text="مشاهده کردم ✅", callback_data=f"seen:{code}")
    builder.adjust(1)
    text = "برای دریافت محتوا ابتدا دکمه زیر را بزنید."

    if isinstance(message, CallbackQuery):
        if message.message:
            await message.message.edit_text(text, reply_markup=builder.as_markup())
        await message.answer()
    else:
        await message.answer(text, reply_markup=builder.as_markup())


def extract_upload_payload(message: Message) -> dict[str, str] | None:
    if message.text and not message.text.startswith("/"):
        return {"content_type": "text", "text": message.text, "file_id": ""}

    if message.photo:
        return {
            "content_type": "photo",
            "text": "",
            "file_id": message.photo[-1].file_id,
        }

    if message.video:
        return {
            "content_type": "video",
            "text": "",
            "file_id": message.video.file_id,
        }

    if message.audio:
        return {
            "content_type": "audio",
            "text": "",
            "file_id": message.audio.file_id,
        }

    if message.voice:
        return {
            "content_type": "voice",
            "text": "",
            "file_id": message.voice.file_id,
        }

    if message.document:
        return {
            "content_type": "document",
            "text": "",
            "file_id": message.document.file_id,
        }

    if message.animation:
        return {
            "content_type": "animation",
            "text": "",
            "file_id": message.animation.file_id,
        }

    if message.sticker:
        return {
            "content_type": "sticker",
            "text": "",
            "file_id": message.sticker.file_id,
        }

    return None


def persian_content_type(content_type: str) -> str:
    mapping = {
        "text": "متن",
        "photo": "عکس",
        "video": "ویدیو",
        "audio": "صدا",
        "voice": "ویس",
        "document": "فایل",
        "animation": "گیف",
        "sticker": "استیکر",
    }
    return mapping.get(content_type, content_type)


async def send_payload(bot: Bot, chat_id: int, payload: dict[str, Any]) -> Message:
    content_type = payload["content_type"]
    file_id = payload.get("file_id") or ""
    text = payload.get("text") or ""

    if content_type == "text":
        return await bot.send_message(chat_id, text)
    if content_type == "photo":
        return await bot.send_photo(chat_id, file_id)
    if content_type == "video":
        return await bot.send_video(chat_id, file_id)
    if content_type == "audio":
        return await bot.send_audio(chat_id, file_id)
    if content_type == "voice":
        return await bot.send_voice(chat_id, file_id)
    if content_type == "document":
        return await bot.send_document(chat_id, file_id)
    if content_type == "animation":
        return await bot.send_animation(chat_id, file_id)
    if content_type == "sticker":
        return await bot.send_sticker(chat_id, file_id)

    return await bot.send_message(chat_id, "نوع محتوا پشتیبانی نمی‌شود.")


async def delete_after_60_seconds(bot: Bot, chat_id: int, message_ids: list[int]) -> None:
    await asyncio.sleep(60)
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            pass


async def deliver_content(bot: Bot, user_id: int, code: str, source_message: Message | None = None) -> None:
    content = db.get_content_by_code(code)
    if not content:
        if source_message:
            await source_message.answer("این لینک معتبر نیست یا حذف شده است.")
        return

    payload = {
        "content_type": content["content_type"],
        "file_id": content["file_id"],
        "text": content["text"],
    }

    sent_content = await send_payload(bot, user_id, payload)

    info_text = (
        "آپلود کننده:\n\n"
        f"نام ادمین: {content['uploader_name']}\n"
        f"شناسه ادمین: {content['uploader_id']}\n"
        f"تاریخ آپلود: {content['created_at']}"
    )
    await bot.send_message(user_id, info_text)

    countdown = await bot.send_message(user_id, "⏳ این محتوا تا ۶۰ ثانیه دیگر حذف می‌شود.")

    db.record_download(user_id=user_id, content_id=content["id"], code=code)

    user = None
    if source_message:
        user = source_message.from_user

    log_text = (
        "کاربر فایل دریافت کرد\n\n"
        f"نام: {user_name(user) if user else str(user_id)}\n"
        f"آیدی: {user_id}\n"
        f"کد فایل: {code}\n\n"
        "----------------"
    )
    await send_log(bot, log_text)

    asyncio.create_task(
        delete_after_60_seconds(
            bot=bot,
            chat_id=user_id,
            message_ids=[sent_content.message_id, countdown.message_id],
        )
    )


async def send_daily_report(bot: Bot) -> None:
    report = db.get_daily_report()
    text = (
        "📊 گزارش روزانه\n\n"
        f"کاربران جدید: {report['new_users']}\n"
        f"کل کاربران: {report['total_users']}\n"
        f"دانلودها: {report['downloads']}\n"
        f"آپلودهای جدید: {report['uploads']}\n"
        f"عضویت‌های موفق: {report['successful_joins']}\n"
        f"بازدید لینک‌ها: {report['link_views']}"
    )
    await send_log(bot, text)


async def send_backup(bot: Bot) -> None:
    backup_path = db.create_backup()
    try:
        file = FSInputFile(backup_path)
        await bot.send_document(
            LOG_CHANNEL_ID,
            file,
            caption=f"بکاپ دیتابیس\nتاریخ: {now_text()}",
        )
    except TelegramAPIError as e:
        logging.error("خطا در ارسال بکاپ: %s", e)
    finally:
        try:
            os.remove(backup_path)
        except OSError:
            pass


@router.message(CommandStart())
async def start_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return

    is_new = db.register_user(
        user_id=message.from_user.id,
        first_name=message.from_user.first_name or "",
        last_name=message.from_user.last_name or "",
        username=message.from_user.username or "",
    )

    if is_new:
        await log_new_user(bot, message)

    code = command.args.strip().upper() if command.args else ""

    if code:
        content = db.get_content_by_code(code)
        if not content:
            await message.answer("این لینک معتبر نیست یا حذف شده است.")
            return

        db.increment_link_view()

        missing_channels = await check_missing_channels(bot, message.from_user.id)
        if missing_channels:
            await show_force_join_message(message, missing_channels, code)
            return

        await ask_mandatory_seen(message, code)
        return

    await message.answer(
        f"سلام {message.from_user.first_name}\n\n"
        "به ربات آپلودر تریاک خوش اومدی 👋"
    )


@router.message(Command("admin"))
@router.message(Command("panel"))
async def admin_command(message: Message) -> None:
    if not message.from_user:
        return

    if not await is_admin(message.from_user.id):
        await message.answer("شما به پنل مدیریت دسترسی ندارید.")
        return

    await message.answer("پنل مدیریت", reply_markup=admin_panel_keyboard())


@router.callback_query(F.data == "admin:panel")
async def admin_panel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.from_user:
        return

    await state.clear()

    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("پنل مدیریت", reply_markup=admin_panel_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:upload")
async def upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_upload)
    text = (
        "محتوای خود را ارسال کنید.\n\n"
        "موارد قابل قبول:\n"
        "متن، عکس، ویدیو، صدا، ویس، فایل، گیف و استیکر\n\n"
        "برای لغو، کلمه «لغو» را بفرستید."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.message(AdminStates.waiting_upload)
async def receive_upload(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    if not await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return

    if message.text and message.text.strip() == "لغو":
        await state.clear()
        await message.answer("عملیات لغو شد.", reply_markup=admin_panel_keyboard())
        return

    payload = extract_upload_payload(message)
    if not payload:
        await message.answer("این نوع محتوا پشتیبانی نمی‌شود. دوباره ارسال کنید.")
        return

    code = db.create_content(
        content_type=payload["content_type"],
        file_id=payload["file_id"],
        text=payload["text"],
        uploader_id=message.from_user.id,
        uploader_name=user_name(message.from_user),
    )

    link = f"https://t.me/{BOT_USERNAME}?start={code}"

    await state.clear()

    await message.answer(
        "محتوا با موفقیت ذخیره شد.\n\n"
        f"کد لینک: {code}\n"
        f"لینک:\n{link}",
        reply_markup=admin_panel_keyboard(),
    )

    log_text = (
        "ادمین فایل جدید آپلود کرد\n\n"
        f"نام ادمین: {user_name(message.from_user)}\n"
        f"نوع فایل: {persian_content_type(payload['content_type'])}\n"
        f"کد لینک: {code}\n\n"
        "----------------"
    )
    await send_log(bot, log_text)


@router.callback_query(F.data == "admin:force_join")
async def force_join_menu(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    channels = db.get_channels()
    enabled = "فعال" if db.get_force_join_enabled() else "غیرفعال"
    text = (
        "مدیریت عضویت اجباری\n\n"
        f"وضعیت: {enabled}\n"
        f"تعداد کانال‌ها: {len(channels)}"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=force_join_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "fj:enable")
async def force_join_enable(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    db.set_force_join_enabled(True)
    await send_log(bot, "عضویت اجباری تغییر کرد\n\nوضعیت: فعال\n\n----------------")
    if callback.message:
        await callback.message.edit_text(
            "عضویت اجباری فعال شد.",
            reply_markup=force_join_menu_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "fj:disable")
async def force_join_disable(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    db.set_force_join_enabled(False)
    await send_log(bot, "عضویت اجباری تغییر کرد\n\nوضعیت: غیرفعال\n\n----------------")
    if callback.message:
        await callback.message.edit_text(
            "عضویت اجباری غیرفعال شد.",
            reply_markup=force_join_menu_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "fj:add")
async def add_channel_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_channel)
    text = (
        "شناسه کانال را ارسال کنید.\n\n"
        "نمونه:\n"
        "@channelusername\n"
        "یا\n"
        "-1001234567890\n\n"
        "ربات باید در کانال عضو باشد. برای کانال خصوصی بهتر است ربات ادمین باشد."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.message(AdminStates.waiting_channel)
async def add_channel_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    if not await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return

    if not message.text:
        await message.answer("لطفا شناسه کانال را به صورت متن ارسال کنید.")
        return

    raw_chat = message.text.strip()

    try:
        chat = await bot.get_chat(raw_chat)
    except TelegramAPIError:
        await message.answer("کانال پیدا نشد. مطمئن شوید ربات داخل کانال باشد.")
        return

    url = ""
    if chat.username:
        url = f"https://t.me/{chat.username}"
    elif chat.invite_link:
        url = chat.invite_link
    else:
        try:
            invite = await bot.create_chat_invite_link(chat.id, name="لینک عضویت اجباری")
            url = invite.invite_link
        except TelegramAPIError:
            await message.answer("برای ساخت لینک کانال خصوصی، ربات باید ادمین کانال باشد.")
            return

    title = chat.title or str(chat.id)
    db.add_channel(chat_id=str(chat.id), title=title, url=url)

    await state.clear()
    await message.answer(
        f"کانال اضافه شد.\n\nنام: {title}\nشناسه: {chat.id}",
        reply_markup=force_join_menu_keyboard(),
    )

    await send_log(
        bot,
        "عضویت اجباری تغییر کرد\n\n"
        f"کانال اضافه شد: {title}\n"
        f"شناسه: {chat.id}\n\n"
        "----------------",
    )


@router.callback_query(F.data == "fj:remove_menu")
async def remove_channel_menu(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    channels = db.get_channels()
    builder = InlineKeyboardBuilder()

    if channels:
        for channel in channels:
            builder.button(
                text=f"حذف {channel['title']}",
                callback_data=f"fj:remove:{channel['id']}",
            )
    builder.button(text="بازگشت", callback_data="admin:force_join")
    builder.adjust(1)

    text = "کانال مورد نظر برای حذف را انتخاب کنید." if channels else "هیچ کانالی ثبت نشده است."
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("fj:remove:"))
async def remove_channel(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    channel_id = int(callback.data.split(":")[-1])
    channel = db.get_channel_by_id(channel_id)
    db.remove_channel(channel_id)

    await send_log(
        bot,
        "عضویت اجباری تغییر کرد\n\n"
        f"کانال حذف شد: {channel['title'] if channel else channel_id}\n\n"
        "----------------",
    )

    if callback.message:
        await callback.message.edit_text("کانال حذف شد.", reply_markup=force_join_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("join_check:"))
async def join_check(callback: CallbackQuery, bot: Bot) -> None:
    code = callback.data.split(":", 1)[1]
    if code == "none":
        code = ""

    missing_channels = await check_missing_channels(bot, callback.from_user.id)
    if missing_channels:
        await show_force_join_message(callback, missing_channels, code if code else None)
        return

    db.increment_successful_join()

    if code:
        await ask_mandatory_seen(callback, code)
    else:
        if callback.message:
            await callback.message.edit_text("عضویت شما تایید شد.")
        await callback.answer("عضویت تایید شد.")


@router.callback_query(F.data.startswith("seen:"))
async def seen_callback(callback: CallbackQuery, bot: Bot) -> None:
    code = callback.data.split(":", 1)[1].upper()

    missing_channels = await check_missing_channels(bot, callback.from_user.id)
    if missing_channels:
        await show_force_join_message(callback, missing_channels, code)
        return

    await callback.answer("تایید شد.")

    if callback.message:
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass

    await deliver_content(bot, callback.from_user.id, code)


@router.callback_query(F.data == "admin:admins")
async def admins_menu(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    if callback.from_user.id != OWNER_ID:
        await callback.answer("فقط مالک ربات می‌تواند ادمین‌ها را مدیریت کند.", show_alert=True)
        return

    admins = db.get_admins()
    lines = ["مدیریت ادمین‌ها\n"]
    if admins:
        for admin in admins:
            lines.append(f"{admin['name']} - {admin['user_id']}")
    else:
        lines.append("ادمینی ثبت نشده است.")

    if callback.message:
        await callback.message.edit_text("\n".join(lines), reply_markup=admins_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "adm:add")
async def add_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != OWNER_ID:
        await callback.answer("فقط مالک ربات دسترسی دارد.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_admin)
    text = (
        "شناسه عددی ادمین جدید را ارسال کنید.\n\n"
        "می‌توانید نام را هم بعد از شناسه بنویسید.\n"
        "نمونه:\n"
        "123456789 علی"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.message(AdminStates.waiting_admin)
async def add_admin_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user or message.from_user.id != OWNER_ID:
        await state.clear()
        await message.answer("فقط مالک ربات دسترسی دارد.")
        return

    if not message.text:
        await message.answer("شناسه عددی را ارسال کنید.")
        return

    parts = message.text.strip().split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        await message.answer("شناسه وارد شده درست نیست.")
        return

    admin_id = int(parts[0])
    admin_name = parts[1] if len(parts) > 1 else str(admin_id)

    try:
        chat = await bot.get_chat(admin_id)
        admin_name = chat.full_name or admin_name
    except TelegramAPIError:
        pass

    db.add_admin(user_id=admin_id, name=admin_name, added_by=message.from_user.id)

    await state.clear()
    await message.answer("ادمین جدید اضافه شد.", reply_markup=admins_menu_keyboard())

    await send_log(
        bot,
        "ادمین جدید اضافه شد\n\n"
        f"نام: {admin_name}\n"
        f"آیدی: {admin_id}\n\n"
        "----------------",
    )


@router.callback_query(F.data == "adm:remove_menu")
async def remove_admin_menu(callback: CallbackQuery) -> None:
    if callback.from_user.id != OWNER_ID:
        await callback.answer("فقط مالک ربات دسترسی دارد.", show_alert=True)
        return

    admins = [admin for admin in db.get_admins() if admin["user_id"] != OWNER_ID]
    builder = InlineKeyboardBuilder()

    if admins:
        for admin in admins:
            builder.button(
                text=f"حذف {admin['name']}",
                callback_data=f"adm:remove:{admin['user_id']}",
            )

    builder.button(text="بازگشت", callback_data="admin:admins")
    builder.adjust(1)

    text = "ادمین مورد نظر برای حذف را انتخاب کنید." if admins else "ادمینی برای حذف وجود ندارد."
    if callback.message:
        await callback.message.edit_text(text, reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("adm:remove:"))
async def remove_admin_callback(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != OWNER_ID:
        await callback.answer("فقط مالک ربات دسترسی دارد.", show_alert=True)
        return

    admin_id = int(callback.data.split(":")[-1])
    db.remove_admin(admin_id)

    await send_log(
        bot,
        "ادمین حذف شد\n\n"
        f"آیدی: {admin_id}\n\n"
        "----------------",
    )

    if callback.message:
        await callback.message.edit_text("ادمین حذف شد.", reply_markup=admins_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:stats")
async def stats_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    stats = db.get_statistics_summary()
    text = (
        "آمار ربات\n\n"
        f"کل کاربران: {stats['total_users']}\n"
        f"کل آپلودها: {stats['total_uploads']}\n"
        f"کل دانلودها: {stats['total_downloads']}\n"
        f"کل ادمین‌ها: {stats['total_admins']}\n"
        f"کل کانال‌های عضویت اجباری: {stats['total_channels']}\n"
        f"کاربران امروز: {stats['daily_users']}\n"
        f"دانلودهای امروز: {stats['daily_downloads']}"
    )

    if callback.message:
        await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:daily_report")
async def manual_daily_report(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    report = db.get_daily_report()
    text = (
        "📊 گزارش روزانه\n\n"
        f"کاربران جدید: {report['new_users']}\n"
        f"کل کاربران: {report['total_users']}\n"
        f"دانلودها: {report['downloads']}\n"
        f"آپلودهای جدید: {report['uploads']}\n"
        f"عضویت‌های موفق: {report['successful_joins']}\n"
        f"بازدید لینک‌ها: {report['link_views']}"
    )

    if callback.message:
        await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.callback_query(F.data == "admin:settings")
async def settings_callback(callback: CallbackQuery) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    if callback.message:
        await callback.message.edit_text("تنظیمات", reply_markup=settings_keyboard())
    await callback.answer()


@router.callback_query(F.data == "set:broadcast")
async def broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_broadcast)
    text = (
        "پیام همگانی را ارسال کنید.\n\n"
        "متن، عکس، ویدیو، صدا، ویس، فایل، گیف و استیکر قابل ارسال است.\n"
        "برای لغو، کلمه «لغو» را بفرستید."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=back_keyboard())
    await callback.answer()


@router.message(AdminStates.waiting_broadcast)
async def broadcast_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.from_user:
        return

    if not await is_admin(message.from_user.id):
        await state.clear()
        await message.answer("دسترسی ندارید.")
        return

    if message.text and message.text.strip() == "لغو":
        await state.clear()
        await message.answer("عملیات لغو شد.", reply_markup=admin_panel_keyboard())
        return

    payload = extract_upload_payload(message)
    if not payload:
        await message.answer("این نوع پیام پشتیبانی نمی‌شود.")
        return

    user_ids = db.get_all_user_ids()
    success = 0
    failed = 0

    status_message = await message.answer("ارسال همگانی شروع شد...")

    for user_id in user_ids:
        try:
            await send_payload(bot, user_id, payload)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            db.mark_user_blocked(user_id, True)
            failed += 1
        except TelegramAPIError:
            failed += 1

    db.increment_broadcast()

    await state.clear()

    try:
        await status_message.edit_text(
            "ارسال همگانی انجام شد.\n\n"
            f"موفق: {success}\n"
            f"ناموفق: {failed}",
            reply_markup=admin_panel_keyboard(),
        )
    except TelegramAPIError:
        await message.answer(
            "ارسال همگانی انجام شد.\n\n"
            f"موفق: {success}\n"
            f"ناموفق: {failed}",
            reply_markup=admin_panel_keyboard(),
        )

    await send_log(
        bot,
        "ارسال همگانی انجام شد\n\n"
        f"موفق: {success}\n"
        f"ناموفق: {failed}\n\n"
        "----------------",
    )


@router.callback_query(F.data == "set:backup")
async def manual_backup(callback: CallbackQuery, bot: Bot) -> None:
    if not await is_admin(callback.from_user.id):
        await callback.answer("دسترسی ندارید.", show_alert=True)
        return

    await callback.answer("در حال تهیه بکاپ...")
    await send_backup(bot)

    if callback.message:
        await callback.message.edit_text("بکاپ به کانال گزارش ارسال شد.", reply_markup=settings_keyboard())


@router.message()
async def unknown_message(message: Message) -> None:
    if not message.from_user:
        return

    db.register_user(
        user_id=message.from_user.id,
        first_name=message.from_user.first_name or "",
        last_name=message.from_user.last_name or "",
        username=message.from_user.username or "",
    )

    await message.answer("برای شروع از /start استفاده کنید.")


async def scheduler_daily_report(bot: Bot) -> None:
    await send_daily_report(bot)


async def scheduler_backup(bot: Bot) -> None:
    await send_backup(bot)


async def main() -> None:
    global BOT_USERNAME

    db.initialize()
    db.add_admin(user_id=OWNER_ID, name="مالک ربات", added_by=OWNER_ID)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=None),
    )

    me = await bot.get_me()
    BOT_USERNAME = me.username or ""

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone="Asia/Tehran")
    scheduler.add_job(
        scheduler_daily_report,
        "interval",
        hours=24,
        args=[bot],
        next_run_time=datetime.now() + timedelta(hours=24),
        id="daily_report",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduler_backup,
        "interval",
        hours=24,
        args=[bot],
        next_run_time=datetime.now() + timedelta(hours=24),
        id="daily_backup",
        replace_existing=True,
    )
    scheduler.start()

    logging.info("ربات با موفقیت روشن شد.")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
