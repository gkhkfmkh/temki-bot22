from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать розыгрыш", callback_data="a:create")],
            [InlineKeyboardButton(text="🛠 Управление текущим", callback_data="a:manage")],
            [InlineKeyboardButton(text="📍 Чат публикации", callback_data="a:target")],
            [InlineKeyboardButton(text="ℹ️ Инструкция", callback_data="a:help")],
        ]
    )


def manage_keyboard(status: str) -> InlineKeyboardMarkup:
    publish_label = "🚀 Опубликовать" if status == "draft" else "🔄 Обновить пост"
    rows = [
        [InlineKeyboardButton(text="👁 Предпросмотр", callback_data="a:preview")],
        [InlineKeyboardButton(text=publish_label, callback_data="a:publish")],
        [
            InlineKeyboardButton(text="✏️ Заголовок", callback_data="a:edit:title"),
            InlineKeyboardButton(text="🎁 Приз", callback_data="a:edit:prize"),
        ],
        [InlineKeyboardButton(text="📝 Текст и оформление", callback_data="a:edit:body")],
        [
            InlineKeyboardButton(text="🖼 Фото", callback_data="a:edit:photo"),
            InlineKeyboardButton(text="🔘 Текст кнопки", callback_data="a:edit:button"),
        ],
        [
            InlineKeyboardButton(text="⏰ Дата итогов", callback_data="a:edit:deadline"),
            InlineKeyboardButton(text="🏆 Победители", callback_data="a:edit:winners"),
        ],
        [InlineKeyboardButton(text="📢 Условия подписки", callback_data="a:edit:channels")],
        [InlineKeyboardButton(text="📍 Изменить чат", callback_data="a:edit:target")],
        [
            InlineKeyboardButton(text="👥 Участники", callback_data="a:entries"),
            InlineKeyboardButton(text="📥 CSV", callback_data="a:export"),
        ],
    ]
    if status == "draft":
        rows.append([InlineKeyboardButton(text="🗑 Удалить черновик", callback_data="a:discard_confirm")])
    else:
        rows.append([InlineKeyboardButton(text="🏁 Завершить и выбрать", callback_data="a:draw_confirm")])
    rows.append([InlineKeyboardButton(text="⬅️ В панель", callback_data="a:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_draw_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, завершить и выбрать победителей", callback_data="a:draw_yes")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="a:manage")],
        ]
    )


def post_keyboard(giveaway: dict, preview: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    channels = []
    try:
        import json

        channels = json.loads(giveaway.get("channels_json") or "[]")
    except (TypeError, ValueError):
        channels = []

    for channel in channels:
        url = channel.get("url")
        if url:
            label = (channel.get("title") or "Канал")[:45]
            rows.append([InlineKeyboardButton(text=f"📢 {label}", url=url)])

    rows.append(
        [
            InlineKeyboardButton(
                text=giveaway.get("button_text") or "🎟 Участвовать",
                callback_data="preview:noop" if preview else f"join:{giveaway['id']}",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_discard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить черновик", callback_data="a:discard_yes")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="a:manage")],
        ]
    )
