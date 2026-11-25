from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import Settings
from app.database import Database
from app.keyboards import (
    confirm_discard_keyboard,
    confirm_draw_keyboard,
    manage_keyboard,
    panel_keyboard,
    post_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="giveaway")


class CreateGiveaway(StatesGroup):
    title = State()
    prize = State()
    body = State()
    photo = State()
    deadline = State()
    winners = State()
    channels = State()
    button = State()


class EditGiveaway(StatesGroup):
    value = State()
    photo = State()


class SetTarget(StatesGroup):
    value = State()


FIELD_PROMPTS = {
    "title": "Пришлите новый заголовок розыгрыша.",
    "prize": "Пришлите новое описание приза.",
    "body": (
        "Пришлите новый текст поста. Можно использовать HTML-разметку Telegram:\n"
        "<b>жирный</b>, <i>курсив</i>, <u>подчёркнутый</u>, <s>зачёркнутый</s>,\n"
        "<code>моноширинный</code>, <a href=\"https://example.com\">ссылка</a>.\n\n"
        "Эмодзи и пустые строки тоже поддерживаются."
    ),
    "deadline": "Укажите новую дату итогов в формате ДД.ММ.ГГГГ ЧЧ:ММ (время МСК).",
    "winners": "Сколько победителей выбрать? Введите целое число от 1 до 100.",
    "channels": (
        "Пришлите каналы для обязательной подписки — по одному на строке.\n"
        "Публичный канал: <code>@channelname</code>\n"
        "Приватный канал: <code>-1001234567890 | https://t.me/+invite</code>\n"
        "Для отключения проверки подписки отправьте <code>-</code>.\n\n"
        "Бот должен быть администратором каждого канала, чтобы проверять подписку."
    ),
    "button": "Введите текст кнопки участия, например <code>🎟 Участвовать</code>.",
    "target": "Пришлите новый чат публикации: <code>@channelname</code> или <code>-1001234567890</code>.",
}


def now_local(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


def now_iso(settings: Settings) -> str:
    # Store timestamps in UTC so SQLite's lexical deadline comparison stays correct across DST.
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_deadline(value: str, settings: Settings) -> str:
    try:
        parsed = datetime.strptime(value.strip(), "%d.%m.%Y %H:%M")
    except ValueError as exc:
        raise ValueError("Нужен формат ДД.ММ.ГГГГ ЧЧ:ММ, например 31.12.2026 20:00.") from exc
    parsed = parsed.replace(tzinfo=ZoneInfo(settings.timezone))
    if parsed <= now_local(settings):
        raise ValueError("Дата итогов должна быть в будущем.")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def pretty_deadline(value: str, settings: Settings) -> str:
    parsed = datetime.fromisoformat(value).astimezone(ZoneInfo(settings.timezone))
    return parsed.strftime("%d.%m.%Y в %H:%M")


def parse_channels(value: str) -> list[dict[str, str]]:
    value = value.strip()
    if value in {"", "-", "нет", "нету"}:
        return []

    result: list[dict[str, str]] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "|" in line:
            chat_id, url = (part.strip() for part in line.split("|", 1))
        else:
            chat_id, url = line, ""

        if chat_id.startswith("@") and re.fullmatch(r"@[A-Za-z0-9_]{5,32}", chat_id):
            if not url:
                url = f"https://t.me/{chat_id[1:]}"
            title = chat_id
        elif re.fullmatch(r"-100\d{5,}", chat_id):
            if not url:
                raise ValueError(
                    f"Для приватного канала {chat_id} укажите ссылку-приглашение после символа |."
                )
            title = "Канал для подписки"
        else:
            raise ValueError(
                f"Не удалось распознать канал: {line}. Используйте @username или -100... | ссылка."
            )

        if not (url.startswith("https://t.me/") or url.startswith("http://t.me/")):
            raise ValueError(f"Ссылка канала должна вести на t.me: {url}")
        result.append({"chat_id": chat_id, "url": url, "title": title})

    if len(result) > 10:
        raise ValueError("Можно добавить не более 10 каналов.")
    return result


def valid_target(value: str) -> str:
    target = value.strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", target):
        return target
    if re.fullmatch(r"-100\d{5,}", target):
        return target
    raise ValueError("Укажите @username публичного канала или числовой ID вида -1001234567890.")


def message_as_html(message: Message) -> str:
    """Keep Telegram toolbar formatting, while also allowing hand-written HTML tags."""
    raw = (message.text or "").strip()
    manual_html = re.search(
        r"</?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|a|tg-spoiler|blockquote)\b",
        raw,
        flags=re.IGNORECASE,
    )
    if manual_html:
        return raw
    if message.entities:
        return message.html_text.strip()
    return raw


def render_post(giveaway: dict[str, Any], settings: Settings) -> str:
    title = html.escape(giveaway["title"])
    prize = html.escape(giveaway["prize_text"])
    body = giveaway["body_html"].strip()
    deadline = pretty_deadline(giveaway["deadline"], settings)
    text = (
        f"🎉 <b>{title}</b>\n\n"
        f"{body}\n\n"
        f"🎁 <b>Приз:</b>\n{prize}\n\n"
        f"🗓 <b>Итоги:</b> {deadline} МСК"
    )
    if len(text) > 4000:
        raise ValueError(
            f"Итоговый пост слишком длинный ({len(text)} символов). Сократите текст примерно до 4000 символов."
        )
    return text


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_ids


async def safe_edit(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if not callback.message:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.debug("Не удалось отредактировать сообщение панели: %s", exc)
    await callback.answer()


async def panel_text(db: Database) -> str:
    target = await db.get_setting("target_chat_id")
    giveaway = await db.get_current_giveaway()
    status = "нет активного розыгрыша"
    if giveaway:
        status = f"#{giveaway['id']} — {'черновик' if giveaway['status'] == 'draft' else 'опубликован'}"
    return (
        "<b>🎛 ПАНЕЛЬ УПРАВЛЕНИЯ РОЗЫГРЫШАМИ</b>\n\n"
        f"📍 Чат публикации: <code>{html.escape(target or 'не настроен')}</code>\n"
        f"📌 Текущий розыгрыш: {status}\n\n"
        "Выберите действие ниже. Все изменения применяются от имени этого бота."
    )


async def manage_text(db: Database, settings: Settings, giveaway: dict[str, Any]) -> str:
    count = await db.participant_count(int(giveaway["id"]))
    channels = json.loads(giveaway.get("channels_json") or "[]")
    status_names = {"draft": "Черновик", "published": "Опубликован", "closed": "Завершён"}
    target = giveaway.get("target_chat_id") or "не задан"
    photo = "есть" if giveaway.get("photo_file_id") else "нет"
    return (
        f"<b>🛠 УПРАВЛЕНИЕ РОЗЫГРЫШЕМ #{giveaway['id']}</b>\n\n"
        f"Название: <b>{html.escape(giveaway['title'])}</b>\n"
        f"Статус: <b>{status_names.get(giveaway['status'], giveaway['status'])}</b>\n"
        f"Приз: {html.escape(giveaway['prize_text'])}\n"
        f"Итоги: <b>{pretty_deadline(giveaway['deadline'], settings)} МСК</b>\n"
        f"Победителей: <b>{giveaway['winners_count']}</b>\n"
        f"Участников: <b>{count}</b>\n"
        f"Каналов для подписки: <b>{len(channels)}</b>\n"
        f"Фото: <b>{photo}</b>\n"
        f"Чат: <code>{html.escape(str(target))}</code>\n\n"
        "Выберите, что изменить. После сохранения опубликованный пост будет обновлён."
    )


async def send_management(message: Message, db: Database, settings: Settings) -> None:
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await message.answer("Сейчас нет активного розыгрыша. Создайте новый через панель.", reply_markup=panel_keyboard())
        return
    await message.answer(
        await manage_text(db, settings, giveaway),
        reply_markup=manage_keyboard(giveaway["status"]),
    )


async def post_giveaway(bot: Bot, db: Database, settings: Settings, giveaway: dict[str, Any]) -> tuple[bool, str]:
    target = giveaway.get("target_chat_id")
    if not target:
        return False, "Не задан чат публикации. Сначала настройте его в панели администратора."

    try:
        text = render_post(giveaway, settings)
    except ValueError as exc:
        return False, str(exc)

    new_photo_message_id: int | None = None
    try:
        # Send the optional banner separately so the main post can contain up to 4096 characters.
        if giveaway.get("photo_file_id"):
            photo_message = await bot.send_photo(
                chat_id=target,
                photo=giveaway["photo_file_id"],
            )
            new_photo_message_id = photo_message.message_id

        post_message = await bot.send_message(
            chat_id=target,
            text=text,
            reply_markup=post_keyboard(giveaway),
            disable_web_page_preview=True,
        )
    except TelegramAPIError as exc:
        if new_photo_message_id is not None:
            try:
                await bot.delete_message(chat_id=target, message_id=new_photo_message_id)
            except TelegramAPIError:
                pass
        logger.exception("Не удалось опубликовать розыгрыш")
        return False, f"Telegram не принял публикацию: {html.escape(str(exc))}"

    old_chat = giveaway.get("post_chat_id") or target
    old_post_id = giveaway.get("post_message_id")
    old_photo_id = giveaway.get("photo_message_id")

    await db.update_giveaway(int(giveaway["id"]), "status", "published")
    await db.update_giveaway(int(giveaway["id"]), "post_message_id", post_message.message_id)
    await db.update_giveaway(int(giveaway["id"]), "photo_message_id", new_photo_message_id)
    await db.update_giveaway(int(giveaway["id"]), "post_chat_id", str(target))

    # Remove the previous version only after the new version has been sent successfully.
    cleanup_failed = False
    for old_id in (old_post_id, old_photo_id):
        if old_id:
            try:
                await bot.delete_message(chat_id=old_chat, message_id=int(old_id))
            except TelegramAPIError as exc:
                cleanup_failed = True
                logger.warning("Не удалось удалить старую версию поста %s: %s", old_id, exc)

    if not old_post_id:
        return True, "Пост опубликован от имени бота."
    if cleanup_failed:
        return True, "Новая версия опубликована, но старую удалить не удалось. Проверьте права бота на удаление сообщений."
    return True, "Пост успешно обновлён."


async def finalize_giveaway(
    bot: Bot,
    db: Database,
    settings: Settings,
    giveaway_id: int,
    notify_admins: bool = True,
) -> str | None:
    closed = await db.close_and_draw(giveaway_id, now_iso(settings))
    if not closed:
        return None
    giveaway, winners = closed

    chat_id = giveaway.get("post_chat_id") or giveaway.get("target_chat_id")
    if chat_id and giveaway.get("post_message_id"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=int(giveaway["post_message_id"]),
                reply_markup=None,
            )
        except TelegramAPIError:
            logger.warning("Не удалось убрать кнопку участия у завершённого розыгрыша #%s", giveaway_id)

    if winners:
        lines = []
        for index, winner in enumerate(winners, start=1):
            name = html.escape(winner["full_name"])
            username = winner.get("username")
            if username:
                label = f"@{html.escape(username)}"
                mention = f'<a href="https://t.me/{html.escape(username)}">{label}</a>'
            else:
                mention = f'<a href="tg://user?id={winner["user_id"]}">{name}</a>'
            lines.append(f"{index}. {mention}")
        result_text = (
            f"🏁 <b>РОЗЫГРЫШ «{html.escape(giveaway['title'])}» ЗАВЕРШЁН!</b>\n\n"
            "🎉 Победители:\n" + "\n".join(lines)
        )
    else:
        result_text = (
            f"🏁 <b>РОЗЫГРЫШ «{html.escape(giveaway['title'])}» ЗАВЕРШЁН!</b>\n\n"
            "Участников не оказалось, поэтому победители не определены."
        )

    if chat_id:
        try:
            await bot.send_message(chat_id=chat_id, text=result_text, disable_web_page_preview=True)
        except TelegramAPIError:
            logger.exception("Не удалось отправить итоги в чат розыгрыша #%s", giveaway_id)

    if notify_admins:
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"Розыгрыш #{giveaway_id} завершён. Победителей выбрано: {len(winners)}.",
                )
            except TelegramAPIError:
                pass
    return result_text


async def deadline_worker(bot: Bot, db: Database, settings: Settings) -> None:
    while True:
        try:
            expired = await db.expired_giveaways(now_iso(settings))
            for giveaway in expired:
                await finalize_giveaway(bot, db, settings, int(giveaway["id"]))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой проверки дедлайнов")
        await asyncio.sleep(20)


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    await state.clear()
    if not message.from_user:
        return
    if is_admin(message.from_user.id, settings) and message.chat.type == "private":
        await message.answer(await panel_text(db), reply_markup=panel_keyboard())
        return
    await message.answer(
        "Привет! Я бот для проведения розыгрышей.\n\n"
        "Чтобы участвовать, открой пост организатора и нажми кнопку «Участвовать». "
        "Я проверю условия и зарегистрирую заявку."
    )


@router.message(Command("admin"))
async def admin_command(message: Message, db: Database, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        await message.answer("Доступ запрещён.")
        return
    if message.chat.type != "private":
        await message.answer("Откройте личный чат с ботом, чтобы управлять розыгрышами.")
        return
    await message.answer(await panel_text(db), reply_markup=panel_keyboard())


@router.message(Command("id"))
async def id_command(message: Message) -> None:
    await message.answer(f"ID этого чата: <code>{message.chat.id}</code>")


@router.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=panel_keyboard())


@router.callback_query(F.data.startswith("join:"))
async def join_callback(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    if not callback.from_user:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return
    try:
        giveaway_id = int((callback.data or "").split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная ссылка участия.", show_alert=True)
        return

    giveaway = await db.get_giveaway(giveaway_id)
    if not giveaway or giveaway["status"] != "published":
        await callback.answer("Этот розыгрыш уже недоступен.", show_alert=True)
        return

    deadline = datetime.fromisoformat(giveaway["deadline"])
    if deadline <= now_local(settings):
        await finalize_giveaway(bot, db, settings, giveaway_id)
        await callback.answer("Время участия закончилось. Розыгрыш закрыт.", show_alert=True)
        return

    channels = json.loads(giveaway.get("channels_json") or "[]")
    for channel in channels:
        try:
            member = await bot.get_chat_member(chat_id=channel["chat_id"], user_id=callback.from_user.id)
        except TelegramAPIError:
            logger.exception("Не удалось проверить подписку на %s", channel.get("chat_id"))
            await callback.answer(
                "Не удалось проверить подписку. Сообщите организатору, что проверка канала временно недоступна.",
                show_alert=True,
            )
            return

        member_status = getattr(member.status, "value", member.status)
        is_member = member_status in {"creator", "administrator", "member"}
        if member_status == "restricted" and getattr(member, "is_member", False):
            is_member = True
        if not is_member:
            await callback.answer(
                "Сначала подпишитесь на все каналы по кнопкам в посте, затем нажмите «Участвовать» ещё раз.",
                show_alert=True,
            )
            return

    user = callback.from_user
    full_name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "Участник"
    added = await db.add_participant(
        giveaway_id=giveaway_id,
        user_id=user.id,
        username=user.username,
        full_name=full_name,
        joined_at=now_iso(settings),
    )
    await callback.answer(
        "✅ Вы зарегистрированы в розыгрыше! Удачи!" if added else "Вы уже участвуете. Повторная заявка не нужна.",
        show_alert=True,
    )


@router.callback_query(F.data == "preview:noop")
async def preview_noop(callback: CallbackQuery) -> None:
    await callback.answer("Это предпросмотр. Реальная регистрация здесь отключена.", show_alert=True)


@router.callback_query(F.data == "a:home")
async def admin_home(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    await safe_edit(callback, await panel_text(db), panel_keyboard())


@router.callback_query(F.data == "a:help")
async def admin_help(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    text = (
        "<b>📘 КАК ЗАПУСТИТЬ РОЗЫГРЫШ</b>\n\n"
        "1. Добавьте бота администратором в канал или группу публикации.\n"
        "2. В панели задайте чат публикации.\n"
        "3. Создайте розыгрыш: заголовок, приз, текст, баннер, дату, число победителей и условия подписки.\n"
        "4. Откройте предпросмотр и нажмите «Опубликовать».\n"
        "5. Участники нажимают кнопку в посте. Повторные заявки одного аккаунта блокируются.\n"
        "6. В указанный срок бот автоматически завершит розыгрыш и выберет победителей.\n\n"
        "<b>Оформление:</b> в поле текста поддерживается HTML Telegram — жирный, курсив, ссылки, подчёркивание и эмодзи.\n\n"
        "<b>Важно:</b> для автоматической проверки подписок бот должен быть администратором каналов, которые указаны в условиях."
    )
    await safe_edit(callback, text, panel_keyboard())


@router.callback_query(F.data == "a:create")
async def create_start(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    if callback.message and callback.message.chat.type != "private":
        await callback.answer("Откройте личный чат с ботом.", show_alert=True)
        return
    current = await db.get_current_giveaway()
    if current:
        await callback.answer("Сначала завершите текущий розыгрыш или откройте его управление.", show_alert=True)
        if callback.message:
            await callback.message.answer(
                await manage_text(db, settings, current),
                reply_markup=manage_keyboard(current["status"]),
            )
        return
    await state.clear()
    await state.set_state(CreateGiveaway.title)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Создание розыгрыша — шаг 1 из 8</b>\n\n"
            "Введите заголовок, например: <b>Разыгрываем Toyota Camry</b>.\n\n"
            "Для отмены отправьте /cancel."
        )


@router.message(CreateGiveaway.title)
async def create_title(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    value = (message.text or "").strip()
    if not value or len(value) > 120:
        await message.answer("Заголовок должен содержать от 1 до 120 символов. Попробуйте ещё раз.")
        return
    await state.update_data(title=value)
    await state.set_state(CreateGiveaway.prize)
    await message.answer("<b>Шаг 2 из 8.</b> Опишите приз или призовой фонд.")


@router.message(CreateGiveaway.prize)
async def create_prize(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    value = (message.text or "").strip()
    if not value or len(value) > 500:
        await message.answer("Описание приза должно содержать от 1 до 500 символов.")
        return
    await state.update_data(prize_text=value)
    await state.set_state(CreateGiveaway.body)
    await message.answer(
        "<b>Шаг 3 из 8.</b> Пришлите основной текст и условия розыгрыша.\n\n"
        "Поддерживается HTML: <code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
        "<code>&lt;i&gt;курсив&lt;/i&gt;</code>, <code>&lt;a href=\"https://...\"&gt;ссылка&lt;/a&gt;</code>.\n"
        "Эмодзи, списки и переносы строк сохраняются. Не используйте Markdown-разметку.",
    )


@router.message(CreateGiveaway.body)
async def create_body(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    value = message_as_html(message)
    if not value or len(value) > 3500:
        await message.answer("Текст должен содержать от 1 до 3500 символов. Пришлите его ещё раз.")
        return
    await state.update_data(body_html=value)
    await state.set_state(CreateGiveaway.photo)
    await message.answer(
        "<b>Шаг 4 из 8.</b> Отправьте фото/баннер розыгрыша.\n"
        "Либо отправьте /skip, чтобы оставить пост без изображения."
    )


@router.message(CreateGiveaway.photo)
async def create_photo(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    if message.photo:
        await state.update_data(photo_file_id=message.photo[-1].file_id)
    elif (message.text or "").strip().lower() == "/skip":
        await state.update_data(photo_file_id=None)
    else:
        await message.answer("Пришлите изображение как фото или отправьте /skip.")
        return
    await state.set_state(CreateGiveaway.deadline)
    await message.answer(
        "<b>Шаг 5 из 8.</b> Когда завершить розыгрыш?\n"
        "Формат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>, например <code>31.12.2026 20:00</code>. Время — МСК."
    )


@router.message(CreateGiveaway.deadline)
async def create_deadline(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    try:
        value = parse_deadline(message.text or "", settings)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(deadline=value)
    await state.set_state(CreateGiveaway.winners)
    await message.answer("<b>Шаг 6 из 8.</b> Сколько победителей выбрать? Введите число от 1 до 100.")


@router.message(CreateGiveaway.winners)
async def create_winners(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    try:
        value = int((message.text or "").strip())
        if not 1 <= value <= 100:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число от 1 до 100.")
        return
    await state.update_data(winners_count=value)
    await state.set_state(CreateGiveaway.channels)
    await message.answer(
        "<b>Шаг 7 из 8.</b> Условия подписки на каналы.\n\n"
        "Публичный канал — отправьте <code>@channelname</code>.\n"
        "Несколько каналов — каждый с новой строки.\n"
        "Приватный канал — <code>-1001234567890 | https://t.me/+invite</code>.\n"
        "Без проверки подписки — отправьте <code>-</code>.\n\n"
        "Убедитесь, что бот добавлен администратором в каждый канал."
    )


@router.message(CreateGiveaway.channels)
async def create_channels(message: Message, state: FSMContext, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    try:
        channels = parse_channels(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.update_data(channels_json=json.dumps(channels, ensure_ascii=False))
    await state.set_state(CreateGiveaway.button)
    await message.answer(
        "<b>Шаг 8 из 8.</b> Как подписать кнопку участия?\n"
        "Например: <code>🎟 Участвовать</code>.\n"
        "Отправьте /skip для стандартной кнопки."
    )


@router.message(CreateGiveaway.button)
async def create_button(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    value = (message.text or "").strip()
    if value.lower() == "/skip":
        value = "🎟 Участвовать"
    if not value or len(value) > 50:
        await message.answer("Текст кнопки должен содержать от 1 до 50 символов.")
        return

    data = await state.get_data()
    target = await db.get_setting("target_chat_id")
    giveaway_data = {
        **data,
        "button_text": value,
        "target_chat_id": target,
        "created_at": now_iso(settings),
    }
    giveaway_id = await db.create_giveaway(giveaway_data)
    await state.clear()
    giveaway = await db.get_giveaway(giveaway_id)
    await message.answer(
        f"✅ <b>Черновик розыгрыша #{giveaway_id} создан.</b>\n\n"
        + ("Чат публикации уже настроен. Можно посмотреть предпросмотр и опубликовать." if target else "Чат публикации пока не настроен. Укажите его в панели, затем вернитесь к розыгрышу."),
        reply_markup=manage_keyboard("draft"),
    )
    if giveaway:
        logger.info("Admin %s created giveaway #%s", message.from_user.id, giveaway_id)


@router.callback_query(F.data == "a:manage")
async def admin_manage(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await safe_edit(callback, "Активного розыгрыша нет. Создайте новый.", panel_keyboard())
        return
    await safe_edit(callback, await manage_text(db, settings, giveaway), manage_keyboard(giveaway["status"]))


@router.callback_query(F.data == "a:target")
async def target_start(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    current = await db.get_setting("target_chat_id")
    await state.set_state(SetTarget.value)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Чат публикации</b>\n\n"
            f"Сейчас: <code>{html.escape(current or 'не настроен')}</code>\n\n"
            "Пришлите @username публичного канала или ID вида -1001234567890.\n"
            "Добавьте бота администратором с правом публикации сообщений.\n"
            "Команда /id в группе покажет ID чата. Для отмены — /cancel."
        )


@router.message(SetTarget.value)
async def target_save(message: Message, state: FSMContext, db: Database, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    try:
        target = valid_target(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await db.set_setting("target_chat_id", target)
    current = await db.get_current_giveaway()
    if current and current["status"] == "draft" and not current.get("target_chat_id"):
        await db.update_giveaway(int(current["id"]), "target_chat_id", target)
    await state.clear()
    await message.answer(
        f"✅ Чат публикации сохранён: <code>{html.escape(target)}</code>.\n"
        "Это значение будет использоваться для новых розыгрышей.",
        reply_markup=panel_keyboard(),
    )


@router.callback_query(F.data == "a:preview")
async def preview_callback(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await callback.answer("Нет активного розыгрыша.", show_alert=True)
        return
    try:
        text = render_post(giveaway, settings)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if not callback.message:
        await callback.answer("Предпросмотр недоступен.", show_alert=True)
        return
    try:
        if giveaway.get("photo_file_id"):
            await bot.send_photo(chat_id=callback.message.chat.id, photo=giveaway["photo_file_id"])
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="<i>Предпросмотр публикации:</i>\n\n" + text,
            reply_markup=post_keyboard(giveaway, preview=True),
            disable_web_page_preview=True,
        )
    except TelegramAPIError as exc:
        logger.exception("Не удалось отправить предпросмотр")
        await callback.answer(f"Ошибка предпросмотра: {str(exc)[:150]}", show_alert=True)
        return
    await callback.answer("Предпросмотр отправлен в личный чат.")


@router.callback_query(F.data == "a:publish")
async def publish_callback(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await callback.answer("Нет активного розыгрыша.", show_alert=True)
        return
    ok, result = await post_giveaway(bot, db, settings, giveaway)
    await callback.answer(result if len(result) < 190 else result[:187] + "...", show_alert=not ok)
    updated = await db.get_giveaway(int(giveaway["id"]))
    if callback.message and updated:
        await callback.message.answer(
            ("✅ " if ok else "❌ ") + html.escape(result),
            reply_markup=manage_keyboard(updated["status"]),
        )


@router.callback_query(F.data.startswith("a:edit:"))
async def edit_start(callback: CallbackQuery, state: FSMContext, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    field = (callback.data or "").split(":", 2)[2]
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await callback.answer("Нет активного розыгрыша.", show_alert=True)
        return
    if field not in FIELD_PROMPTS:
        await callback.answer("Неизвестное поле.", show_alert=True)
        return
    await state.update_data(giveaway_id=int(giveaway["id"]), field=field)
    if field == "photo":
        await state.set_state(EditGiveaway.photo)
        prompt = "Пришлите новое фото. Для удаления текущего изображения отправьте /remove."
    else:
        await state.set_state(EditGiveaway.value)
        prompt = FIELD_PROMPTS[field]
        if field == "deadline":
            prompt += f"\nТекущее значение: <code>{pretty_deadline(giveaway['deadline'], settings)}</code>"
        elif field == "winners":
            prompt += f"\nСейчас: <code>{giveaway['winners_count']}</code>"
        elif field == "target":
            prompt += f"\nСейчас: <code>{html.escape(giveaway.get('target_chat_id') or 'не задан')}</code>"
    await callback.answer()
    if callback.message:
        await callback.message.answer(prompt + "\n\nДля отмены отправьте /cancel.")


@router.message(EditGiveaway.value)
async def edit_value_save(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    data = await state.get_data()
    field = data.get("field")
    giveaway_id = int(data.get("giveaway_id", 0))
    value = (message.text or "").strip()

    try:
        if field == "title":
            if not value or len(value) > 120:
                raise ValueError("Заголовок должен содержать от 1 до 120 символов.")
            db_field, db_value = "title", value
        elif field == "prize":
            if not value or len(value) > 500:
                raise ValueError("Описание приза должно содержать от 1 до 500 символов.")
            db_field, db_value = "prize_text", value
        elif field == "body":
            value = message_as_html(message)
            if not value or len(value) > 3500:
                raise ValueError("Текст должен содержать от 1 до 3500 символов.")
            db_field, db_value = "body_html", value
        elif field == "deadline":
            db_field, db_value = "deadline", parse_deadline(value, settings)
        elif field == "winners":
            count = int(value)
            if not 1 <= count <= 100:
                raise ValueError
            db_field, db_value = "winners_count", count
        elif field == "channels":
            db_field, db_value = "channels_json", json.dumps(parse_channels(value), ensure_ascii=False)
        elif field == "button":
            if not value or len(value) > 50:
                raise ValueError("Текст кнопки должен содержать от 1 до 50 символов.")
            db_field, db_value = "button_text", value
        elif field == "target":
            db_field, db_value = "target_chat_id", valid_target(value)
        else:
            raise ValueError("Не удалось определить поле для изменения.")
    except (ValueError, TypeError) as exc:
        message_text = str(exc) or "Введите корректное значение."
        await message.answer(message_text)
        return

    await db.update_giveaway(giveaway_id, db_field, db_value)
    await state.clear()
    giveaway = await db.get_giveaway(giveaway_id)
    if not giveaway:
        await message.answer("Розыгрыш не найден.", reply_markup=panel_keyboard())
        return

    note = "✅ Изменение сохранено."
    if giveaway["status"] == "published":
        ok, result = await post_giveaway(bot, db, settings, giveaway)
        note += " " + (result if ok else "Не удалось обновить пост: " + result)
        giveaway = await db.get_giveaway(giveaway_id) or giveaway
    await message.answer(
        html.escape(note) + "\n\n" + await manage_text(db, settings, giveaway),
        reply_markup=manage_keyboard(giveaway["status"]),
    )


@router.message(EditGiveaway.photo)
async def edit_photo_save(message: Message, state: FSMContext, bot: Bot, db: Database, settings: Settings) -> None:
    if not message.from_user or not is_admin(message.from_user.id, settings):
        return
    data = await state.get_data()
    giveaway_id = int(data.get("giveaway_id", 0))
    if message.photo:
        photo_file_id: str | None = message.photo[-1].file_id
    elif (message.text or "").strip().lower() == "/remove":
        photo_file_id = None
    else:
        await message.answer("Пришлите фото или отправьте /remove, чтобы убрать изображение.")
        return

    await db.update_giveaway(giveaway_id, "photo_file_id", photo_file_id)
    await state.clear()
    giveaway = await db.get_giveaway(giveaway_id)
    if not giveaway:
        await message.answer("Розыгрыш не найден.", reply_markup=panel_keyboard())
        return
    note = "✅ Фото обновлено." if photo_file_id else "✅ Фото удалено."
    if giveaway["status"] == "published":
        ok, result = await post_giveaway(bot, db, settings, giveaway)
        note += " " + (result if ok else "Не удалось обновить пост: " + result)
        giveaway = await db.get_giveaway(giveaway_id) or giveaway
    await message.answer(
        html.escape(note) + "\n\n" + await manage_text(db, settings, giveaway),
        reply_markup=manage_keyboard(giveaway["status"]),
    )


@router.callback_query(F.data == "a:entries")
async def entries_callback(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await callback.answer("Нет активного розыгрыша.", show_alert=True)
        return
    participants = await db.list_participants(int(giveaway["id"]))
    await callback.answer()
    if not callback.message:
        return
    if not participants:
        await callback.message.answer("Пока нет участников.")
        return
    lines = []
    for index, participant in enumerate(participants[:50], start=1):
        username = f"@{participant['username']}" if participant.get("username") else "без username"
        lines.append(f"{index}. {html.escape(participant['full_name'])} — {html.escape(username)}")
    suffix = f"\n… и ещё {len(participants) - 50}" if len(participants) > 50 else ""
    await callback.message.answer(
        f"<b>Участники розыгрыша #{giveaway['id']}</b> — всего: {len(participants)}\n\n"
        + "\n".join(lines)
        + suffix
    )


@router.callback_query(F.data == "a:export")
async def export_callback(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await callback.answer("Нет активного розыгрыша.", show_alert=True)
        return
    participants = await db.list_participants(int(giveaway["id"]))
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["user_id", "username", "full_name", "joined_at"])
    for item in participants:
        writer.writerow([item["user_id"], item.get("username") or "", item["full_name"], item["joined_at"]])
    file = BufferedInputFile(
        output.getvalue().encode("utf-8-sig"),
        filename=f"giveaway_{giveaway['id']}_participants.csv",
    )
    await callback.answer("Готово.")
    if callback.message:
        await callback.message.answer_document(file, caption=f"Участники розыгрыша #{giveaway['id']} — {len(participants)} записей")


@router.callback_query(F.data == "a:draw_confirm")
async def draw_confirm(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await callback.answer("Нет активного розыгрыша.", show_alert=True)
        return
    count = await db.participant_count(int(giveaway["id"]))
    await safe_edit(
        callback,
        f"<b>Завершить розыгрыш #{giveaway['id']} досрочно?</b>\n\n"
        f"Сейчас зарегистрировано участников: <b>{count}</b>.\n"
        "После подтверждения бот закроет приём заявок и случайным образом выберет победителей. Отменить выбор нельзя.",
        confirm_draw_keyboard(),
    )


@router.callback_query(F.data == "a:draw_yes")
async def draw_yes(callback: CallbackQuery, bot: Bot, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway:
        await callback.answer("Нет активного розыгрыша.", show_alert=True)
        return
    result = await finalize_giveaway(bot, db, settings, int(giveaway["id"]))
    await callback.answer("Розыгрыш завершён." if result else "Розыгрыш уже закрыт.", show_alert=True)
    if callback.message:
        await callback.message.answer(
            "🏁 Розыгрыш закрыт. Итоги опубликованы в чате розыгрыша.",
            reply_markup=panel_keyboard(),
        )


@router.callback_query(F.data == "a:discard_confirm")
async def discard_confirm(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway or giveaway["status"] != "draft":
        await callback.answer("Удалить можно только черновик.", show_alert=True)
        return
    await safe_edit(
        callback,
        f"<b>Удалить черновик #{giveaway['id']}?</b>\n\n"
        "Черновик и его настройки будут удалены. Опубликованные розыгрыши этой кнопкой удалить нельзя.",
        confirm_discard_keyboard(),
    )


@router.callback_query(F.data == "a:discard_yes")
async def discard_yes(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
        return
    giveaway = await db.get_current_giveaway()
    if not giveaway or giveaway["status"] != "draft":
        await callback.answer("Черновик не найден.", show_alert=True)
        return
    deleted = await db.delete_draft(int(giveaway["id"]))
    await callback.answer("Черновик удалён." if deleted else "Не удалось удалить черновик.", show_alert=True)
    if callback.message:
        await callback.message.answer(await panel_text(db), reply_markup=panel_keyboard())


@router.callback_query(F.data.startswith("a:"))
async def unknown_admin_callback(callback: CallbackQuery, settings: Settings) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None, settings):
        await callback.answer("Доступ запрещён.", show_alert=True)
    else:
        await callback.answer("Неизвестная команда панели. Откройте /admin.", show_alert=True)


@router.message()
async def fallback_message(message: Message, settings: Settings) -> None:
    if message.from_user and is_admin(message.from_user.id, settings) and message.chat.type == "private":
        await message.answer("Откройте панель командой /admin. Для отмены текущего действия — /cancel.")
