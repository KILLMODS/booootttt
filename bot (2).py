import os
import re
import html
import sqlite3
import secrets
import logging
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, ContextTypes, filters,
)

# ============================================================
# Конфигурация
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "9201147290:AAEasqRd2JsJ5mBTUiFGg-sQwDR7QsfyslU")
PANEL_IMAGE = Path(__file__).parent / "panel.png"
DB_PATH = Path(__file__).parent / "bot.db"
SUPPORT_URL = "https://t.me/lalpol"
ADMIN_IDS = [7459502130, 8509358597]

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# База данных
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            accepted INTEGER DEFAULT 0,
            deals_count INTEGER DEFAULT 0,
            trust_score TEXT DEFAULT '—',
            username TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            value TEXT NOT NULL,
            bank TEXT DEFAULT '',
            UNIQUE(user_id, type)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deal_code TEXT UNIQUE NOT NULL,
            creator_id INTEGER NOT NULL,
            seller_id INTEGER,
            category TEXT,
            item_name TEXT,
            item_link TEXT,
            description TEXT,
            amount REAL,
            currency TEXT,
            status TEXT DEFAULT 'pending_seller',
            seller_payment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Миграции: добавляем недостающие колонки в существующие таблицы
    def column_exists(table: str, column: str) -> bool:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return any(row[1] == column for row in cur.fetchall())

    if not column_exists("users", "username"):
        conn.execute("ALTER TABLE users ADD COLUMN username TEXT DEFAULT ''")
    if not column_exists("users", "deals_count"):
        conn.execute("ALTER TABLE users ADD COLUMN deals_count INTEGER DEFAULT 0")
    if not column_exists("users", "trust_score"):
        conn.execute("ALTER TABLE users ADD COLUMN trust_score TEXT DEFAULT '—'")
    if not column_exists("users", "accepted"):
        conn.execute("ALTER TABLE users ADD COLUMN accepted INTEGER DEFAULT 0")

    conn.commit()
    conn.close()

def get_user(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else {"user_id": user_id, "accepted": 0, "deals_count": 0, "trust_score": "—", "username": ""}

def set_accepted(user_id: int, username: str = ""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO users (user_id, accepted, username) VALUES (?, 1, ?)", (user_id, username))
    conn.commit()
    conn.close()

def set_deals_count(user_id: int, count: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.execute("UPDATE users SET deals_count = ? WHERE user_id = ?", (count, user_id))
    conn.commit()
    conn.close()

def set_trust_score(user_id: int, score: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.execute("UPDATE users SET trust_score = ? WHERE user_id = ?", (score, user_id))
    conn.commit()
    conn.close()

def get_payments(user_id: int) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM payments WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def add_payment(user_id: int, ptype: str, value: str, bank: str = "") -> bool:
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM payments WHERE user_id = ?", (user_id,)).fetchone()[0]
    if count >= 2:
        conn.close()
        return False
    try:
        conn.execute("INSERT INTO payments (user_id, type, value, bank) VALUES (?, ?, ?, ?)",
                     (user_id, ptype, value, bank))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def delete_payment(user_id: int, ptype: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM payments WHERE user_id = ? AND type = ?", (user_id, ptype))
    conn.commit()
    conn.close()

def get_deal_by_code(code: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM deals WHERE deal_code = ?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_latest_deal_for_user(uid: int) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM deals WHERE creator_id = ? AND status IN ('pending_payment','seller_confirmed') ORDER BY id DESC LIMIT 1",
        (uid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def attach_seller_to_deal(code: str, seller_id: int) -> dict | None:
    deal = get_deal_by_code(code)
    if not deal:
        return None
    payments = get_payments(seller_id)
    if not payments:
        return deal
    pay_text = "\n".join([
        f"{PAYMENT_LABELS[p['type']]}: {p['value']} ({p['bank']})"
        for p in payments
    ])
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE deals SET seller_id = ?, seller_payment = ?, status = 'pending_payment' WHERE id = ?",
        (seller_id, pay_text, deal["id"])
    )
    conn.commit()
    conn.close()
    return get_deal_by_code(code)

# ============================================================
# Вспомогательные функции
# ============================================================
def btn(text: str, cb: str = None, url: str = None, style: str = None) -> InlineKeyboardButton:
    kw = {"text": text}
    if cb: kw["callback_data"] = cb
    if url: kw["url"] = url
    if style: kw["api_kwargs"] = {"style": style}
    return InlineKeyboardButton(**kw)

def esc(text: str) -> str:
    return html.escape(str(text))

def luhn_check(card: str) -> bool:
    digits = [int(c) for c in card if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    for i in range(len(digits) - 2, -1, -2):
        digits[i] *= 2
        if digits[i] > 9:
            digits[i] = digits[i] // 10 + digits[i] % 10
    return sum(digits) % 10 == 0

def mask_card(n: str) -> str:
    return f"{n[:4]} **** **** {n[-4:]}"

PAYMENT_LABELS = {"card": "💳 Карта", "sbp": "📱 СБП", "crypto": "🪙 CryptoBot"}
CURRENCY_LABELS = {"RUB": "🇷🇺 Рубли (RUB)", "TON": "💎 TON"}

# ============================================================
# Состояния
# ============================================================
CARD_NUM, CARD_BANK, SBP_PHONE, SBP_BANK = range(4)
CAT, ITEM_NAME, ITEM_DESC, CURRENCY, AMOUNT, CONFIRM = range(10, 16)

# ============================================================
# Клавиатуры
# ============================================================
def terms_kb():
    return InlineKeyboardMarkup([[btn("✅ Принять пользовательское соглашение", "accept_terms", style="success")]])

def main_menu_kb():
    return InlineKeyboardMarkup([
        [btn("➕ Создать сделку", "create_deal", style="success")],
        [btn("👤 Профиль", "profile", style="primary"), btn("📋 Мои сделки", "my_deals", style="primary")],
        [btn("💳 Способы оплаты", "payments", style="primary"), btn("ℹ️ Info", "info", style="primary")],
        [btn("🆘 Поддержка", "support", style="danger")],
    ])

def info_kb():
    return InlineKeyboardMarkup([
        [btn("📄 Пользовательское соглашение", "show_terms", style="primary")],
        [btn("🆘 Поддержка", url=SUPPORT_URL, style="danger")],
        [btn("⬅️ Назад", "back_main", style="primary")],
    ])

def support_kb():
    return InlineKeyboardMarkup([
        [btn("💬 Написать в поддержку", url=SUPPORT_URL, style="success")],
        [btn("⬅️ Назад", "back_main", style="primary")],
    ])

def payments_kb(payments: list[dict]):
    rows = []
    for p in payments:
        label = PAYMENT_LABELS.get(p["type"], p["type"])
        if p["type"] == "card":
            label += f" {mask_card(p['value'])}"
        elif p["type"] == "sbp":
            label += f" {p['value']}"
        rows.append([btn(label, f"manage_{p['type']}", style="primary")])
    if len(payments) < 2:
        rows.append([btn("➕ Добавить способ оплаты", "add_payment", style="success")])
    rows.append([btn("⬅️ Назад", "back_main", style="primary")])
    return InlineKeyboardMarkup(rows)

def manage_payment_kb(ptype: str):
    return InlineKeyboardMarkup([
        [btn("✏️ Изменить", f"edit_{ptype}", style="primary"), btn("🗑 Удалить", f"del_{ptype}", style="danger")],
        [btn("⬅️ Назад", "payments", style="primary")],
    ])

def payment_methods_kb(existing: list[dict]):
    existing_types = {p["type"] for p in existing}
    rows = []
    if "card" not in existing_types:
        rows.append([btn("💳 Банковская карта", "pay_card", style="primary")])
    if "sbp" not in existing_types:
        rows.append([btn("📱 СБП (по номеру)", "pay_sbp", style="primary")])
    if "crypto" not in existing_types:
        rows.append([btn("🪙 CryptoBot", "pay_crypto", style="primary")])
    rows.append([btn("⬅️ Назад", "payments", style="primary")])
    return InlineKeyboardMarkup(rows)

def deal_payment_methods_kb(existing: list[dict], deal_code: str):
    existing_types = {p["type"] for p in existing}
    rows = []
    if "card" not in existing_types:
        rows.append([btn("💳 Банковская карта", f"deal_pay_card_{deal_code}", style="primary")])
    if "sbp" not in existing_types:
        rows.append([btn("📱 СБП (по номеру)", f"deal_pay_sbp_{deal_code}", style="primary")])
    if "crypto" not in existing_types:
        rows.append([btn("🪙 CryptoBot", f"deal_pay_crypto_{deal_code}", style="primary")])
    return InlineKeyboardMarkup(rows)

def back_kb(target="back_main"):
    return InlineKeyboardMarkup([[btn("⬅️ Назад", target, style="primary")]])

def category_kb():
    return InlineKeyboardMarkup([
        [btn("🖼 NFT", "cat_nft", style="primary")],
        [btn("⭐ Звёзды", "cat_stars", style="primary")],
        [btn("🛠 Услуга", "cat_service", style="primary")],
        [btn("👤 Аккаунты", "cat_account", style="primary")],
        [btn("⬅️ Назад", "back_main", style="primary")],
    ])

def currency_kb():
    return InlineKeyboardMarkup([
        [btn("🇷🇺 Рубли (RUB)", "cur_RUB", style="primary")],
        [btn("💎 TON", "cur_TON", style="primary")],
        [btn("⬅️ Отмена", "back_main", style="primary")],
    ])

def deal_confirm_kb():
    return InlineKeyboardMarkup([
        [btn("✅ Создать сделку", "confirm_deal", style="success")],
        [btn("✏️ Изменить", "edit_deal", style="primary")],
        [btn("⬅️ Отмена", "back_main", style="danger")],
    ])

def edit_deal_kb():
    return InlineKeyboardMarkup([
        [btn("Название товара", "edit_field_name", style="primary")],
        [btn("Описание", "edit_field_desc", style="primary")],
        [btn("Сумма", "edit_field_amount", style="primary")],
        [btn("⬅️ Назад к проверке", "back_to_confirm", style="primary")],
    ])

def admin_kb():
    return InlineKeyboardMarkup([
        [btn("📊 Выдать сделки", "admin_set_deals", style="primary")],
        [btn("⭐ Выдать доверие", "admin_set_trust", style="primary")],
        [btn("⬅️ Назад", "back_main", style="primary")],
    ])

def seller_action_kb(deal_code: str, buyer_id: int, buyer_username: str | None):
    """Кнопка подарка через username (безопасно) или без неё, если username нет."""
    rows = []
    if buyer_username:
        rows.append([btn("🎁 Передать подарок", url=f"https://t.me/{buyer_username}", style="success")])
    rows.append([btn("🔍 Проверить сделку", f"check_deal_{deal_code}", style="primary")])
    return InlineKeyboardMarkup(rows)

def deal_offer_kb(code: str):
    return InlineKeyboardMarkup([
        [btn("✅ Подтвердить участие", f"seller_confirm_{code}", style="success")],
        [btn("ℹ️ Информация о покупателе", f"seller_info_{code}", style="primary")],
    ])

def no_requisites_kb(code: str):
    return InlineKeyboardMarkup([
        [btn("➕ Добавить реквизиты", f"deal_add_req_{code}", style="success")],
    ])

# ============================================================
# Тексты
# ============================================================
WELCOME = (
    "👋 Добро пожаловать в <b>FunPay Guarant Bot</b>!\n\n"
    "Перед началом работы необходимо принять пользовательское соглашение.\n\n"
    "📄 Ознакомиться с условиями: /terms"
)
TERMS = (
    "<b>Пользовательское соглашение</b>\n\n"
    "1. Бот является официальным гарант-ботом FunPay.\n"
    "2. Используя бота, вы соглашаетесь с правилами сервиса.\n"
    "3. Все сделки проводятся через гаранта.\n"
    "4. Администрация не несёт ответственности за действия третьих лиц.\n\n"
    "Нажмите «Принять», чтобы продолжить."
)

def accepted_text(name: str) -> str:
    return (
        f"👋 <b>Здравствуйте, {esc(name)}!</b>\n\n"
        "<b>FunPay Guarant Bot</b> — официальный гарант-бот платформы FunPay.\n\n"
        "<blockquote>Здесь вы можете безопасно проводить сделки между покупателем и продавцом: "
        "средства замораживаются до подтверждения выполнения условий.</blockquote>\n\n"
        "<b>Что доступно:</b>\n"
        "• ➕ Создание сделки с выбором категории и валюты\n"
        "• 👤 Профиль со статистикой и доверием\n"
        "• 💳 До двух способов оплаты\n"
        "• 📋 История сделок\n\n"
        "Выберите действие ниже 👇"
    )

INFO_TEXT = (
    "<b>ℹ️ О боте</b>\n\n"
    "<b>FunPay Guarant Bot</b> — официальный гарант-бот платформы FunPay.\n\n"
    "<blockquote>Бот предназначен для безопасного проведения сделок между покупателем "
    "и продавцом. Средства замораживаются до подтверждения выполнения условий "
    "обеими сторонами.</blockquote>\n\n"
    "🔒 Все операции проходят через гаранта.\n"
    "📄 Пользуясь ботом, вы принимаете пользовательское соглашение."
)

def deal_offer_text(deal: dict) -> str:
    return (
        f"<b>📄 Вам предложена сделка</b>\n\n"
        f"<b>📦 Товар:</b> {esc(deal['item_name'])}\n"
        f"<b>📝 Описание:</b> {esc(deal['description'])}\n"
        f"<b>💰 Сумма:</b> <code>{deal['amount']} {deal['currency']}</code>\n\n"
        f"<b>💳 Ваши реквизиты:</b>\n"
        f"<pre>{esc(deal['seller_payment'] or '—')}</pre>\n\n"
        f"<blockquote>Нажмите кнопку ниже, чтобы подтвердить участие. "
        f"После подтверждения покупатель получит уведомление и сможет оплатить.</blockquote>"
    )

def no_requisites_text(deal: dict) -> str:
    return (
        f"<b>⚠️ У вас не добавлены реквизиты</b>\n\n"
        f"<b>📦 Товар:</b> {esc(deal['item_name'])}\n"
        f"<b>💰 Сумма:</b> <code>{deal['amount']} {deal['currency']}</code>\n\n"
        f"<blockquote>Чтобы получить оплату, добавьте хотя бы один способ: "
        f"карту, СБП или CryptoBot. Нажмите кнопку ниже — откроется панель добавления.</blockquote>"
    )

# ============================================================
# Старт / соглашение
# ============================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0].startswith("deal_"):
        code = args[0].replace("deal_", "")
        deal = get_deal_by_code(code)
        if not deal or deal["status"] not in ("pending_seller", "pending_payment"):
            await update.message.reply_text("❌ Сделка не найдена или уже обработана.")
            return

        if update.effective_user.id == deal["creator_id"]:
            await update.message.reply_text(
                "⚠️ <b>Это ваша собственная сделка.</b>\n\n"
                "<blockquote>Ссылка предназначена для продавца. "
                "Отправьте её тому, кто будет выполнять условия.</blockquote>",
                parse_mode="HTML"
            )
            return

        seller_id = update.effective_user.id
        payments = get_payments(seller_id)
        if not payments:
            await update.message.reply_text(
                no_requisites_text(deal),
                reply_markup=no_requisites_kb(code),
                parse_mode="HTML"
            )
            return

        deal = attach_seller_to_deal(code, seller_id)
        if not deal:
            await update.message.reply_text("❌ Сделка не найдена.")
            return
        await update.message.reply_text(
            deal_offer_text(deal),
            reply_markup=deal_offer_kb(code),
            parse_mode="HTML"
        )
        return

    user = get_user(update.effective_user.id)
    if user["accepted"]:
        with open(PANEL_IMAGE, "rb") as f:
            await update.message.reply_photo(
                f, caption=accepted_text(update.effective_user.first_name),
                reply_markup=main_menu_kb(), parse_mode="HTML"
            )
        return
    await update.message.reply_text(WELCOME, reply_markup=terms_kb(), parse_mode="HTML")

async def cmd_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(TERMS, reply_markup=terms_kb(), parse_mode="HTML")

async def accept_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Соглашение принято")
    set_accepted(q.from_user.id, q.from_user.username or "")

    caption = accepted_text(q.from_user.first_name)
    try:
        with open(PANEL_IMAGE, "rb") as f:
            await q.message.edit_media(
                media=InputMediaPhoto(media=f, caption=caption, parse_mode="HTML"),
                reply_markup=main_menu_kb(),
            )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            try:
                await q.message.delete()
            except Exception:
                pass
            with open(PANEL_IMAGE, "rb") as f:
                await q.message.chat.send_photo(
                    f, caption=caption, reply_markup=main_menu_kb(), parse_mode="HTML"
                )
        else:
            try:
                await q.message.delete()
            except Exception:
                pass
            with open(PANEL_IMAGE, "rb") as f:
                await q.message.chat.send_photo(
                    f, caption=caption, reply_markup=main_menu_kb(), parse_mode="HTML"
                )

# ============================================================
# Панель добавления реквизитов в контексте сделки
# ============================================================
async def deal_add_req(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.replace("deal_add_req_", "")
    deal = get_deal_by_code(code)
    if not deal:
        await q.answer("Сделка не найдена", show_alert=True)
        return
    payments = get_payments(q.from_user.id)
    if len(payments) >= 2:
        await q.answer("Уже добавлено 2 способа оплаты", show_alert=True)
        return
    context.user_data["deal_req_code"] = code
    await q.message.edit_text(
        "<b>➕ Добавление реквизитов</b>\n\n"
        "<blockquote>Выберите способ получения оплаты. После добавления вы вернётесь к сделке автоматически.</blockquote>",
        reply_markup=deal_payment_methods_kb(payments, code),
        parse_mode="HTML"
    )

async def deal_pay_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.replace("deal_pay_card_", "")
    context.user_data["deal_req_code"] = code
    context.user_data["req_return"] = "deal"
    await q.message.edit_text(
        "💳 <b>Введите номер банковской карты</b>\n\n"
        "Пример: <code>4276 1234 5678 9010</code>\n"
        "Поддерживаются Visa, MasterCard, МИР (13–19 цифр).\n\n"
        "Для отмены: /cancel",
        parse_mode="HTML"
    )
    return CARD_NUM

async def deal_pay_sbp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.replace("deal_pay_sbp_", "")
    context.user_data["deal_req_code"] = code
    context.user_data["req_return"] = "deal"
    await q.message.edit_text(
        "📱 <b>Введите номер телефона для СБП</b>\n\n"
        "Пример: <code>+7 999 123-45-67</code> или <code>89991234567</code>\n\n"
        "Для отмены: /cancel",
        parse_mode="HTML"
    )
    return SBP_PHONE

async def deal_pay_crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.replace("deal_pay_crypto_", "")
    uid = q.from_user.id
    if add_payment(uid, "crypto", "", ""):
        await q.answer("CryptoBot подключён", show_alert=True)
    else:
        await q.answer("Не удалось добавить (лимит или дубликат)", show_alert=True)
        return
    deal = attach_seller_to_deal(code, uid)
    if not deal:
        await q.message.edit_text("❌ Сделка не найдена.")
        return
    await q.message.edit_text(
        deal_offer_text(deal),
        reply_markup=deal_offer_kb(code),
        parse_mode="HTML"
    )

# ============================================================
# Создание сделки
# ============================================================
async def create_deal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.clear()
    context.user_data["edit_mode"] = None
    await q.message.delete()
    await q.message.chat.send_message(
        "<b>➕ Создание сделки</b>\n\n"
        "<blockquote>Выберите категорию товара. От этого зависит форма описания.</blockquote>",
        reply_markup=category_kb(), parse_mode="HTML"
    )
    return CAT

async def cat_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat = q.data.replace("cat_", "")
    context.user_data["deal_cat"] = cat
    prompts = {
        "nft": "<b>🖼 NFT</b>\n\n<blockquote>Введите название NFT. Можно вставить ссылку на маркетплейс — она будет сохранена как название.</blockquote>",
        "stars": "<b>⭐ Звёзды</b>\n\n<blockquote>Введите количество звёзд (только число).</blockquote>",
        "service": "<b>🛠 Услуга</b>\n\n<blockquote>Введите название услуги.</blockquote>",
        "account": "<b>👤 Аккаунты</b>\n\n<blockquote>Введите, какой именно аккаунт (платформа, логин/ссылка и т.д.).</blockquote>",
    }
    await q.message.delete()
    await q.message.chat.send_message(prompts[cat], parse_mode="HTML")
    return ITEM_NAME

async def item_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Введите текст.")
        return ITEM_NAME
    cat = context.user_data.get("deal_cat", "nft")
    edit_mode = context.user_data.get("edit_mode")

    if cat == "stars" and edit_mode is None:
        if not text.isdigit():
            await update.message.reply_text("❌ Введите только число звёзд.")
            return ITEM_NAME
        context.user_data["deal_item"] = f"{text} звёзд"
        context.user_data["deal_desc"] = f"Покупка {text} Telegram Stars"
        await update.message.reply_text(
            "<b>✅ Принято</b>\n\n<blockquote>Теперь выберите валюту сделки.</blockquote>",
            reply_markup=currency_kb(), parse_mode="HTML"
        )
        return CURRENCY

    context.user_data["deal_item"] = text
    if edit_mode == "name":
        context.user_data["edit_mode"] = None
        return await _show_confirm(update, context)
    await update.message.reply_text(
        "<b>✅ Название принято</b>\n\n"
        "<blockquote>Введите описание товара. Чем подробнее — тем понятнее продавцу.</blockquote>",
        parse_mode="HTML"
    )
    return ITEM_DESC

async def item_desc_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["deal_desc"] = update.message.text.strip()
    if context.user_data.get("edit_mode") == "desc":
        context.user_data["edit_mode"] = None
        return await _show_confirm(update, context)
    await update.message.reply_text(
        "<b>✅ Описание сохранено</b>\n\n"
        "<blockquote>Выберите валюту, в которой будет указана сумма сделки.</blockquote>",
        reply_markup=currency_kb(), parse_mode="HTML"
    )
    return CURRENCY

async def currency_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cur = q.data.replace("cur_", "")
    context.user_data["deal_currency"] = cur
    label = CURRENCY_LABELS.get(cur, cur)
    await q.message.delete()
    await q.message.chat.send_message(
        f"<b>💱 Валюта: {label}</b>\n\n"
        f"<blockquote>Введите сумму сделки в {label}. "
        f"Используйте точку или запятую для копеек/дробной части.</blockquote>",
        parse_mode="HTML"
    )
    return AMOUNT

async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите корректную сумму (например, 1500 или 12.5):")
        return AMOUNT
    context.user_data["deal_amount"] = amount
    if context.user_data.get("edit_mode") == "amount":
        context.user_data["edit_mode"] = None
    return await _show_confirm(update, context)

async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data
    summary = (
        f"<b>📋 Проверьте сделку</b>\n\n"
        f"<b>Категория:</b> {d['deal_cat'].upper()}\n"
        f"<b>Товар:</b> {esc(d['deal_item'])}\n"
        f"<b>Описание:</b>\n<blockquote>{esc(d['deal_desc'])}</blockquote>\n"
        f"<b>Сумма:</b> <code>{d['deal_amount']} {d['deal_currency']}</code>\n\n"
        f"<blockquote expandable>После создания вы получите ссылку для продавца. "
        f"Продавец перейдёт по ней, подтвердит участие, и вы сможете оплатить. "
        f"Если что-то не так — нажмите «Изменить».</blockquote>"
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(summary, reply_markup=deal_confirm_kb(), parse_mode="HTML")
    else:
        await update.message.reply_text(summary, reply_markup=deal_confirm_kb(), parse_mode="HTML")
    return CONFIRM

async def edit_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.edit_text("<b>✏️ Что изменить?</b>", reply_markup=edit_deal_kb(), parse_mode="HTML")
    return CONFIRM

async def edit_field_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["edit_mode"] = "name"
    await q.message.edit_text("Введите новое название товара:", parse_mode="HTML")
    return ITEM_NAME

async def edit_field_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["edit_mode"] = "desc"
    await q.message.edit_text("Введите новое описание:", parse_mode="HTML")
    return ITEM_DESC

async def edit_field_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["edit_mode"] = "amount"
    await q.message.edit_text("Введите новую сумму:", parse_mode="HTML")
    return AMOUNT

async def back_to_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    return await _show_confirm(update, context)

async def confirm_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = context.user_data
    uid = q.from_user.id
    code = secrets.token_urlsafe(8)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO deals (deal_code, creator_id, category, item_name, item_link, description, amount, currency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (code, uid, d["deal_cat"], d["deal_item"], d.get("deal_item", ""), d["deal_desc"], d["deal_amount"], d["deal_currency"])
    )
    conn.commit()
    conn.close()
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=deal_{code}"
    await q.message.delete()
    await q.message.chat.send_message(
        f"<b>✅ Сделка создана!</b>\n\n"
        f"<b>Код:</b> <code>{code}</code>\n"
        f"<b>Товар:</b> {esc(d['deal_item'])}\n"
        f"<b>Сумма:</b> <code>{d['deal_amount']} {d['deal_currency']}</code>\n\n"
        f"<b>🔗 Ссылка для продавца:</b>\n"
        f"<pre>{esc(link)}</pre>\n\n"
        f"<blockquote expandable>Отправьте эту ссылку продавцу. "
        f"После того как он подтвердит участие, вы получите уведомление с реквизитами для оплаты. "
        f"Не передавайте ссылку третьим лицам.</blockquote>",
        parse_mode="HTML"
    )
    return ConversationHandler.END

# ============================================================
# Информация о покупателе
# ============================================================
async def seller_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.replace("seller_info_", "")
    deal = get_deal_by_code(code)
    if not deal:
        await q.answer("Сделка не найдена", show_alert=True)
        return
    seller = get_user(deal["creator_id"])
    text = (
        f"<b>👤 Информация о покупателе</b>\n\n"
        f"<b>🆔 ID:</b> <code>{deal['creator_id']}</code>\n"
        f"<b>📊 Сделок:</b> {seller.get('deals_count', 0)}\n"
        f"<b>⭐ Доверие:</b> {esc(seller.get('trust_score', '—'))}\n\n"
        f"<blockquote>Этот пользователь создал сделку. "
        f"Проверьте детали перед подтверждением.</blockquote>"
    )
    await q.message.reply_text(text, parse_mode="HTML")

# ============================================================
# Продавец подтверждает
# ============================================================
async def seller_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Вы подтвердили сделку")
    code = q.data.replace("seller_confirm_", "")
    deal = get_deal_by_code(code)
    if not deal:
        await q.message.edit_text("❌ Сделка не найдена.")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE deals SET status = 'seller_confirmed' WHERE id = ?", (deal["id"],))
    conn.commit()
    conn.close()
    await context.bot.send_message(
        deal["creator_id"],
        f"🔔 <b>Продавец подтвердил сделку!</b>\n\n"
        f"<b>Товар:</b> {esc(deal['item_name'])}\n"
        f"<b>Сумма:</b> <code>{deal['amount']} {deal['currency']}</code>\n\n"
        f"<b>💳 Реквизиты продавца:</b>\n<pre>{esc(deal['seller_payment'])}</pre>\n\n"
        f"<blockquote>После оплаты подтвердите её, чтобы продавец получил уведомление.</blockquote>",
        parse_mode="HTML"
    )
    await q.message.edit_text("✅ Вы подтвердили сделку. Ожидайте оплаты.")

# ============================================================
# Секретная команда — уведомление продавцу
# ============================================================
async def secret_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    deal = get_latest_deal_for_user(uid)
    if not deal:
        await update.message.reply_text("❌ Нет сделок, ожидающих подтверждения оплаты.")
        return
    seller_pays = get_payments(deal["seller_id"])
    if not seller_pays:
        await update.message.reply_text("⚠️ У продавца не заполнены реквизиты.")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE deals SET status = 'paid' WHERE id = ?", (deal["id"],))
    conn.commit()
    conn.close()

    # Получаем username покупателя для безопасной кнопки
    buyer_username = None
    try:
        buyer = await context.bot.get_chat(deal["creator_id"])
        buyer_username = buyer.username
    except Exception as e:
        logger.warning(f"Не удалось получить username покупателя: {e}")

    kb = seller_action_kb(deal["deal_code"], deal["creator_id"], buyer_username)

    if buyer_username:
        contact_line = f"<b>👤 Покупатель:</b> @{esc(buyer_username)}"
    else:
        contact_line = (
            f"<b>👤 ID покупателя:</b> <code>{deal['creator_id']}</code>\n"
            f"<blockquote>У покупателя нет username. Найдите его по ID вручную.</blockquote>"
        )

    await context.bot.send_message(
        deal["seller_id"],
        f"💰 <b>Покупатель оплатил сделку!</b>\n\n"
        f"<b>📦 Товар:</b> {esc(deal['item_name'])}\n"
        f"<b>💵 Сумма:</b> <code>{deal['amount']} {deal['currency']}</code>\n\n"
        f"{contact_line}\n\n"
        f"<blockquote><b>Средства заморожены на счёте гаранта (бота).</b>\n"
        f"Передайте товар покупателю. После передачи гарант переведёт оплату "
        f"на ваши реквизиты, указанные ниже.</blockquote>\n\n"
        f"<b>💳 Ваши реквизиты для получения оплаты:</b>\n"
        f"<pre>{esc(deal['seller_payment'])}</pre>",
        reply_markup=kb, parse_mode="HTML"
    )
    await update.message.reply_text("✅ Оплата подтверждена. Продавец уведомлён и получил инструкции.")

async def check_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    code = q.data.replace("check_deal_", "")
    deal = get_deal_by_code(code)
    if not deal:
        await q.answer("Сделка не найдена", show_alert=True)
        return
    status_map = {"pending_seller": "Ожидает продавца", "pending_payment": "Ожидает оплаты",
                  "seller_confirmed": "Продавец подтвердил", "paid": "Оплачено", "completed": "Завершена"}
    await q.answer(f"Статус: {status_map.get(deal['status'], deal['status'])}", show_alert=True)

# ============================================================
# Админ-панель
# ============================================================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас нет доступа.")
        return
    await update.message.reply_text(
        "<b>🛠 Админ-панель</b>\n\n<blockquote>Управление статистикой пользователей.</blockquote>",
        reply_markup=admin_kb(), parse_mode="HTML"
    )

async def admin_set_deals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.edit_text(
        "<b>📊 Выдать сделки</b>\n\n"
        "<blockquote>Отправьте: <code>USER_ID количество</code></blockquote>",
        parse_mode="HTML"
    )
    context.user_data["admin_action"] = "deals"

async def admin_set_trust(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.edit_text(
        "<b>⭐ Выдать доверие</b>\n\n"
        "<blockquote>Отправьте: <code>USER_ID значение</code></blockquote>",
        parse_mode="HTML"
    )
    context.user_data["admin_action"] = "trust"

async def admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("admin_action")
    if not action:
        return
    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await update.message.reply_text("❌ Формат: USER_ID значение")
        return
    try:
        target_id = int(parts[0])
    except ValueError:
        await update.message.reply_text("❌ USER_ID должен быть числом.")
        return
    if action == "deals":
        try:
            count = int(parts[1])
        except ValueError:
            await update.message.reply_text("❌ Количество должно быть числом.")
            return
        set_deals_count(target_id, count)
        await update.message.reply_text(f"✅ Пользователю <code>{target_id}</code> выдано <b>{count}</b> сделок.", parse_mode="HTML")
    elif action == "trust":
        set_trust_score(target_id, parts[1])
        await update.message.reply_text(f"✅ Пользователю <code>{target_id}</code> установлено доверие: <b>{esc(parts[1])}</b>.", parse_mode="HTML")
    context.user_data.pop("admin_action", None)

# ============================================================
# Главное меню
# ============================================================
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    d = q.data

    if d == "accept_terms":
        return

    await q.answer()
    uid = q.from_user.id

    if d == "back_main":
        with open(PANEL_IMAGE, "rb") as f:
            await q.message.delete()
            await q.message.chat.send_photo(
                f, caption=accepted_text(q.from_user.first_name),
                reply_markup=main_menu_kb(), parse_mode="HTML"
            )
        return

    if d == "profile":
        u = get_user(uid)
        text = (
            f"<b>👤 Профиль</b>\n\n"
            f"<b>🆔 ID:</b> <code>{uid}</code>\n"
            f"<b>👤 Имя:</b> {esc(q.from_user.full_name)}\n"
            f"<b>📊 Сделок:</b> {u['deals_count']}\n"
            f"<b>⭐ Доверие:</b> {esc(u['trust_score'])}"
        )
        await q.message.delete()
        await q.message.chat.send_message(text, reply_markup=back_kb(), parse_mode="HTML")

    elif d == "info":
        await q.message.delete()
        await q.message.chat.send_message(INFO_TEXT, reply_markup=info_kb(), parse_mode="HTML")

    elif d == "show_terms":
        await q.message.delete()
        await q.message.chat.send_message(TERMS, reply_markup=back_kb("back_info"), parse_mode="HTML")

    elif d == "back_info":
        await q.message.delete()
        await q.message.chat.send_message(INFO_TEXT, reply_markup=info_kb(), parse_mode="HTML")

    elif d == "support":
        await q.message.delete()
        await q.message.chat.send_message(
            "<b>🆘 Поддержка</b>\n\n<blockquote>Нажмите кнопку ниже, чтобы связаться с нами.</blockquote>",
            reply_markup=support_kb(), parse_mode="HTML")

    elif d == "payments":
        payments = get_payments(uid)
        await q.message.delete()
        text = "<b>💳 Способы оплаты</b>\n\n"
        if not payments:
            text += "<blockquote>У вас пока нет добавленных способов оплаты.\nМожно добавить до <b>двух</b>.</blockquote>"
        else:
            text += "<blockquote>Нажмите на способ, чтобы изменить или удалить его.</blockquote>"
        await q.message.chat.send_message(text, reply_markup=payments_kb(payments), parse_mode="HTML")

    elif d.startswith("manage_"):
        ptype = d.replace("manage_", "")
        payments = get_payments(uid)
        p = next((x for x in payments if x["type"] == ptype), None)
        if not p:
            await q.answer("Способ оплаты не найден", show_alert=True)
            return
        if ptype == "card":
            detail = f"<b>💳 Карта:</b> <code>{mask_card(p['value'])}</code>\n<b>🏦 Банк:</b> {esc(p['bank'])}"
        elif ptype == "sbp":
            detail = f"<b>📱 СБП:</b> <code>{esc(p['value'])}</code>\n<b>🏦 Банк:</b> {esc(p['bank'])}"
        else:
            detail = "<b>🪙 CryptoBot</b> подключён"
        await q.message.delete()
        await q.message.chat.send_message(
            f"<b>{PAYMENT_LABELS[ptype]}</b>\n\n{detail}",
            reply_markup=manage_payment_kb(ptype), parse_mode="HTML")

    elif d.startswith("del_"):
        ptype = d.replace("del_", "")
        delete_payment(uid, ptype)
        await q.answer("Способ оплаты удалён", show_alert=True)
        payments = get_payments(uid)
        await q.message.delete()
        await q.message.chat.send_message(
            "<b>💳 Способы оплаты</b>", reply_markup=payments_kb(payments), parse_mode="HTML")

    elif d.startswith("edit_"):
        ptype = d.replace("edit_", "")
        delete_payment(uid, ptype)
        payments = get_payments(uid)
        await q.message.delete()
        await q.message.chat.send_message(
            f"Выберите новый способ вместо <b>{PAYMENT_LABELS[ptype]}</b>:",
            reply_markup=payment_methods_kb(payments), parse_mode="HTML")

    elif d == "add_payment":
        payments = get_payments(uid)
        if len(payments) >= 2:
            await q.answer("Максимум 2 способа оплаты", show_alert=True)
            return
        await q.message.delete()
        await q.message.chat.send_message(
            "<b>Выберите способ оплаты:</b>\n\n"
            "💳 <b>Карта</b> — номер карты (Visa/MasterCard/МИР) и банк.\n"
            "📱 <b>СБП</b> — номер телефона и банк.\n"
            "🪙 <b>CryptoBot</b> — подключение в один клик.",
            reply_markup=payment_methods_kb(payments), parse_mode="HTML")

    elif d == "pay_crypto":
        payments = get_payments(uid)
        if len(payments) >= 2:
            await q.answer("Максимум 2 способа", show_alert=True)
            return
        if add_payment(uid, "crypto", "", ""):
            await q.answer("CryptoBot подключён", show_alert=True)
        else:
            await q.answer("CryptoBot уже добавлен", show_alert=True)
        payments = get_payments(uid)
        await q.message.delete()
        await q.message.chat.send_message("<b>💳 Способы оплаты</b>", reply_markup=payments_kb(payments), parse_mode="HTML")

    elif d == "my_deals":
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM deals WHERE creator_id = ? OR seller_id = ? ORDER BY id DESC LIMIT 10",
            (uid, uid)
        ).fetchall()
        conn.close()
        if not rows:
            await q.answer("У вас пока нет сделок", show_alert=True)
            return
        lines = ["<b>📋 Мои сделки (последние 10)</b>\n"]
        for r in rows:
            r = dict(r)
            role = "покупатель" if r["creator_id"] == uid else "продавец"
            lines.append(f"<code>{r['deal_code']}</code> — {esc(r['item_name'])} — {r['amount']} {r['currency']} ({role})")
        await q.message.delete()
        await q.message.chat.send_message("\n".join(lines), reply_markup=back_kb(), parse_mode="HTML")

# ============================================================
# Оплата: карта / СБП (общие сценарии)
# ============================================================
async def pay_card_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("deal_req_code", None)
    context.user_data.pop("req_return", None)
    await q.message.delete()
    await q.message.chat.send_message(
        "💳 <b>Введите номер банковской карты</b>\n\n"
        "Пример: <code>4276 1234 5678 9010</code>\n"
        "Поддерживаются Visa, MasterCard, МИР (13–19 цифр).\n\n"
        "Для отмены: /cancel",
        reply_markup=back_kb("payments"), parse_mode="HTML")
    return CARD_NUM

async def card_num_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "")
    if not luhn_check(text):
        await update.message.reply_text("❌ Некорректный номер. Попробуйте снова.\n\n/cancel — отмена")
        return CARD_NUM
    context.user_data["tmp_card"] = text
    await update.message.reply_text("✅ Номер принят.\n\n🏦 Введите название банка (например, <b>Сбербанк</b>):", parse_mode="HTML")
    return CARD_BANK

async def card_bank_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bank = update.message.text.strip()
    uid = update.effective_user.id
    if not add_payment(uid, "card", context.user_data.pop("tmp_card"), bank):
        await update.message.reply_text(
            "❌ Не удалось сохранить (лимит 2 способа).",
            reply_markup=back_kb("payments"), parse_mode="HTML"
        )
        return ConversationHandler.END
    await update.message.reply_text("✅ <b>Карта сохранена!</b>", parse_mode="HTML")
    await _resume_deal_if_pending(update, context)
    return ConversationHandler.END

async def pay_sbp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("deal_req_code", None)
    context.user_data.pop("req_return", None)
    await q.message.delete()
    await q.message.chat.send_message(
        "📱 <b>Введите номер телефона для СБП</b>\n\n"
        "Пример: <code>+7 999 123-45-67</code> или <code>89991234567</code>\n\n"
        "Для отмены: /cancel",
        reply_markup=back_kb("payments"), parse_mode="HTML")
    return SBP_PHONE

async def sbp_phone_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    digits = re.sub(r"\D", "", phone)
    if len(digits) != 11 or digits[0] not in ("7", "8"):
        await update.message.reply_text("❌ Некорректный номер. Введите 11 цифр, начиная с +7 или 8.\n\n/cancel — отмена")
        return SBP_PHONE
    context.user_data["tmp_sbp"] = phone
    await update.message.reply_text("✅ Номер принят.\n\n🏦 Введите название банка:", parse_mode="HTML")
    return SBP_BANK

async def sbp_bank_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bank = update.message.text.strip()
    uid = update.effective_user.id
    if not add_payment(uid, "sbp", context.user_data.pop("tmp_sbp"), bank):
        await update.message.reply_text(
            "❌ Не удалось сохранить (лимит 2 способа).",
            reply_markup=back_kb("payments"), parse_mode="HTML"
        )
        return ConversationHandler.END
    await update.message.reply_text("✅ <b>СБП сохранён!</b>", parse_mode="HTML")
    await _resume_deal_if_pending(update, context)
    return ConversationHandler.END

async def _resume_deal_if_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = context.user_data.pop("deal_req_code", None)
    if not code:
        await update.message.reply_text(
            "Готово. Вернитесь по ссылке сделки, чтобы продолжить.",
            reply_markup=back_kb("payments"), parse_mode="HTML"
        )
        return
    uid = update.effective_user.id
    deal = attach_seller_to_deal(code, uid)
    if not deal:
        await update.message.reply_text("❌ Сделка не найдена.")
        return
    await update.message.reply_text(
        deal_offer_text(deal),
        reply_markup=deal_offer_kb(code),
        parse_mode="HTML"
    )

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=back_kb("payments"))
    return ConversationHandler.END

# ============================================================
# Глобальный обработчик ошибок
# ============================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке апдейта:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Произошла ошибка. Попробуйте ещё раз или напишите в поддержку."
            )
        except Exception:
            pass

# ============================================================
# Запуск
# ============================================================
def main():
    if not PANEL_IMAGE.exists():
        logger.error("panel.png не найден рядом с bot.py")
        return
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    deal_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_deal_start, pattern="^create_deal$")],
        states={
            CAT: [CallbackQueryHandler(cat_chosen, pattern="^cat_")],
            ITEM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, item_name_received)],
            ITEM_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, item_desc_received)],
            CURRENCY: [CallbackQueryHandler(currency_chosen, pattern="^cur_")],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)],
            CONFIRM: [
                CallbackQueryHandler(confirm_deal, pattern="^confirm_deal$"),
                CallbackQueryHandler(edit_deal, pattern="^edit_deal$"),
                CallbackQueryHandler(edit_field_name, pattern="^edit_field_name$"),
                CallbackQueryHandler(edit_field_desc, pattern="^edit_field_desc$"),
                CallbackQueryHandler(edit_field_amount, pattern="^edit_field_amount$"),
                CallbackQueryHandler(back_to_confirm, pattern="^back_to_confirm$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_message=False,
    )

    card_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(pay_card_start, pattern="^pay_card$"),
            CallbackQueryHandler(deal_pay_card_start, pattern="^deal_pay_card_"),
        ],
        states={
            CARD_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, card_num_received)],
            CARD_BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, card_bank_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_message=False,
    )

    sbp_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(pay_sbp_start, pattern="^pay_sbp$"),
            CallbackQueryHandler(deal_pay_sbp_start, pattern="^deal_pay_sbp_"),
        ],
        states={
            SBP_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sbp_phone_received)],
            SBP_BANK: [MessageHandler(filters.TEXT & ~filters.COMMAND, sbp_bank_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("terms", cmd_terms))
    app.add_handler(CommandHandler("penis", secret_confirm))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(accept_terms, pattern="^accept_terms$"))
    app.add_handler(deal_conv)
    app.add_handler(card_conv)
    app.add_handler(sbp_conv)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_input))
    app.add_handler(CallbackQueryHandler(deal_add_req, pattern="^deal_add_req_"))
    app.add_handler(CallbackQueryHandler(deal_pay_crypto, pattern="^deal_pay_crypto_"))
    app.add_handler(CallbackQueryHandler(seller_info_handler, pattern="^seller_info_"))
    app.add_handler(CallbackQueryHandler(seller_confirm, pattern="^seller_confirm_"))
    app.add_handler(CallbackQueryHandler(check_deal_handler, pattern="^check_deal_"))
    app.add_handler(CallbackQueryHandler(admin_set_deals, pattern="^admin_set_deals$"))
    app.add_handler(CallbackQueryHandler(admin_set_trust, pattern="^admin_set_trust$"))
    app.add_handler(CallbackQueryHandler(menu_router))

    app.add_error_handler(error_handler)

    logger.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
