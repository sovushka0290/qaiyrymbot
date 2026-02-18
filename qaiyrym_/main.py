import asyncio
import json
import logging
import os
import sys
from typing import Dict, Any, List
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv
from fastapi import FastAPI
import uvicorn
from fastapi.staticfiles import StaticFiles  # Добавлено для статических файлов

load_dotenv()
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)
# ═══════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
if not BOT_TOKEN or not GEMINI_API_KEY:
    logger.critical("❌ ОШИБКА: Укажите BOT_TOKEN и GEMINI_API_KEY в .env")
    sys.exit(1)
DEFAULT_LANG = "ru"
# ═══════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS
# ═══════════════════════════════════════════════════════════════════════════
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "qaiyrym-credentials.json")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Волонтёры")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
def get_sheets_client():
    """Инициализация клиента Google Sheets."""
    if not GOOGLE_SHEET_ID:
        logger.warning("[SHEETS] GOOGLE_SHEET_ID не установлен в .env")
        return None, None
   
    if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        logger.warning(f"[SHEETS] Файл {GOOGLE_CREDENTIALS_PATH} не найден")
        return None, None
   
    try:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(GOOGLE_SHEET_ID)
        logger.info(f"[SHEETS] Подключено к Google Sheets: {GOOGLE_SHEET_NAME}")
        return sheet, GOOGLE_SHEET_NAME
    except Exception as e:
        logger.error(f"[SHEETS ERROR] {e}")
        return None, None
def append_volunteer_to_sheets(user_id: str, name: str, age: int, skill: str, lang: str, username: str = "") -> bool:
    """Добавляет волонтёра в Google Sheets."""
    sheet, sheet_name = get_sheets_client()
    if not sheet:
        return False
   
    try:
        worksheet = sheet.worksheet(sheet_name)
        row = [user_id, name, age, skill, lang, username, datetime.now().isoformat()]
        worksheet.append_row(row, value_input_option="RAW")
        logger.info(f"[SHEETS] Волонтёр {name} добавлен")
        return True
    except Exception as e:
        logger.error(f"[SHEETS ERROR] {e}")
        return False
# ═══════════════════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════════════════
USER_DB_FILE = "users_db.json"
USERS_DATA: Dict[str, Dict[str, Any]] = {}
def load_users_db():
    """Загружает БД пользователей."""
    global USERS_DATA
    try:
        if os.path.exists(USER_DB_FILE):
            with open(USER_DB_FILE, "r", encoding="utf-8") as f:
                USERS_DATA = json.load(f)
                logger.info(f"[DB] Загружено {len(USERS_DATA)} пользователей")
        else:
            USERS_DATA = {}
    except Exception as e:
        logger.error(f"[DB ERROR] {e}")
        USERS_DATA = {}
def save_users_db():
    """Сохраняет БД пользователей."""
    try:
        with open(USER_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(USERS_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[DB ERROR] {e}")
def get_user_role(user_id: str) -> str:
    """Возвращает роль пользователя."""
    if user_id in USERS_DATA:
        return USERS_DATA[user_id].get("role", "GUEST")
    return "GUEST"
def save_user_registration(user_id: str, name: str, age: int, skill: str, lang: str, username: str = "") -> bool:
    """Сохраняет регистрацию пользователя."""
    try:
        USERS_DATA[user_id] = {
            "user_id": user_id,
            "name": name,
            "age": age,
            "skill": skill,
            "lang": lang,
            "role": "MEMBER",
            "registered_at": datetime.now().isoformat(),
        }
        save_users_db()
        logger.info(f"[DB] Пользователь {user_id} зарегистрирован: {name}")
        append_volunteer_to_sheets(user_id, name, age, skill, lang, username)
        return True
    except Exception as e:
        logger.error(f"[DB ERROR] {e}")
        return False
def set_user_language(user_id: str, lang: str):
    """Сохраняет язык пользователя."""
    if user_id not in USERS_DATA:
        USERS_DATA[user_id] = {"role": "GUEST"}
    USERS_DATA[user_id]["lang"] = lang
    save_users_db()
def get_all_member_ids() -> List[str]:
    """Возвращает список волонтеров."""
    return [user_id for user_id, data in USERS_DATA.items() if data.get("role") == "MEMBER"]
# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE.txt
# ═══════════════════════════════════════════════════════════════════════════
def load_manifest() -> str:
    """Загружает knowledge.txt."""
    paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.txt"),
        os.path.join(os.getcwd(), "knowledge.txt"),
    ]
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                logger.info(f"[MANIFEST] Загружен knowledge.txt ({len(content)} символов)")
                return content
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.error(f"[MANIFEST] ОШИБКА: {e}")
            continue
    logger.warning("[MANIFEST] knowledge.txt не найден")
    return ""
KNOWLEDGE_MANIFEST = load_manifest()
# ═══════════════════════════════════════════════════════════════════════════
# GEMINI
# ═══════════════════════════════════════════════════════════════════════════
GEMINI_MODEL_NAME = "gemini-2.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-2.0-flash"
_client = None
def get_gemini_client():
    """Инициализация Gemini клиента."""
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("[GEMINI] Клиент инициализирован")
    return _client
def get_chat_system_instruction(user_lang: str, role: str = "GUEST", chat_history_len: int = 0) -> str:
    """Формирует системный промпт для ИИ."""
    lang = user_lang if user_lang in ("ru", "kz") else "ru"
    lang_name = "русском" if lang == "ru" else "казахском"
   
    base = (
        "Ты — Компас, ИИ-координатор проекта QAIYRYM.\n\n"
       
        "🎯 ГЛАВНАЯ ЗАДАЧА — ЗАДАВАЙ ВОПРОСЫ!\n"
        "Твоя задача — ВЫТЯГИВАТЬ ИНФОРМАЦИЮ, а не просто отвечать.\n"
        "Ты ведешь ИНТЕРВЬЮ. После каждого ответа задай 1-2 вопроса!\n\n"
       
        "СТРАТЕГИЯ:\n"
        "1. ИНТЕРВЬЮ: Каждый ответ = вопрос в конце\n"
        "2. ПОРЦИИ: Не рассказывай всё сразу, давай 30% информации\n"
        "3. ГИБКОСТЬ: 3-4 предложения обычно, подробнее если просит\n"
        "4. ЛИЧНОСТЬ: Используй 'Кстати, а ты...', 'Интересно узнать...'\n"
        "5. ЭКСТРАВЕРТ: Предлагай помощь, интересуйся деталями\n\n"
       
        "ТЕХНИКА:\n"
        f"• Язык: {lang_name} ({lang})\n"
        "• Используй <code> для терминов\n"
        "• Оформляй важное <b>жирным</b>\n"
        "• Избегай скучных списков\n"
    )
   
    # Приветствие только при старте
    if chat_history_len <= 2:
        base += (
            "\n⭐ ПЕРВОЕ СООБЩЕНИЕ:\n"
            "Ты МОЖЕШЬ поздороваться и задать первый вопрос:\n"
            "'Привет! Я — Компас, координатор QAIYRYM. "
            "Как дела? Расскажи, что тебя привело в наш проект?'\n"
        )
    else:
        base += "\n⭐ ПОСЛЕДУЮЩИЕ: НЕ повторяй приветствие, продолжи диалог.\n"
   
    if role == "MEMBER":
        base += "\n👤 РЕЖИМ УЧАСТНИКА: Обсуждай глубокие темы, детали помощи."
    else:
        base += "\n👤 РЕЖИМ ГОСТЯ: Будь дружелюбен, поощряй присоединиться."
   
    return base
async def ask_gemini(prompt: str, system_prompt: str | None = None, user_lang: str = DEFAULT_LANG, skip_lang_instruction: bool = False) -> str:
    """Вызов Gemini с таймаутом."""
    base = system_prompt or ""
    if not skip_lang_instruction:
        lang = user_lang if user_lang in ("ru", "kz") else DEFAULT_LANG
        lang_name = "русском" if lang == "ru" else "казахском"
        lang_instruction = f"Отвечай на {lang_name} ({lang})."
        system_instruction = f"{base}\n\n{lang_instruction}" if base else lang_instruction
    else:
        system_instruction = base
    def _generate_sync(model_name: str) -> str:
        client = get_gemini_client()
        config_kw = {"max_output_tokens": 512}
        if system_instruction:
            config_kw["system_instruction"] = system_instruction
        config = types.GenerateContentConfig(**config_kw)
        response = client.models.generate_content(model=model_name, contents=prompt, config=config)
        return response.text.strip() if response.text else "Извините, не могу ответить."
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_generate_sync, GEMINI_MODEL_NAME),
            timeout=45.0
        )
    except asyncio.TimeoutError:
        logger.warning("[GEMINI] Таймаут")
        return "⏱️ Время ответа истекло. Попробуйте позже."
    except Exception as e:
        err_str = str(e)
        if "API_KEY_INVALID" in err_str or "API key not valid" in err_str:
            logger.error("[GEMINI] Неверный API ключ!")
            return "⚠️ Ошибка API. Проверьте настройки."
        if "404" in err_str or "NOT_FOUND" in err_str.upper():
            logger.warning(f"[GEMINI] Откат на {GEMINI_FALLBACK_MODEL}")
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(_generate_sync, GEMINI_FALLBACK_MODEL),
                    timeout=45.0
                )
            except Exception as e2:
                logger.error(f"[GEMINI ERROR] {e2}")
                return "К сожалению, не могу ответить."
        logger.error(f"[GEMINI ERROR] {e}")
        return "К сожалению, не могу ответить."
# ═══════════════════════════════════════════════════════════════════════════
# ТЕКСТЫ
# ═══════════════════════════════════════════════════════════════════════════
def t(key: str, lang: str) -> str:
    """Получить текст по ключу."""
    lang = lang if lang in ("ru", "kz") else DEFAULT_LANG
    val = TEXTS.get(key)
    if isinstance(val, dict):
        return val.get(lang, val.get(DEFAULT_LANG, ""))
    return str(val or "")
TEXTS = {
    "choose_lang": {"ru": "Выберите язык:", "kz": "Тілді таңдаңыз:"},
    "intro_guest": {"ru": "Я — Компас, твой координатор QAIYRYM. Выбери действие:", "kz": "Мен — Компас. Әрекетті таңдаңыз:"},
    "intro_member": {"ru": "Привет, участник! 🎉 Чем могу помочь?", "kz": "Сәлем, қатысушы! 🎉"},
    "about": {"ru": "💡 <b>О проекте QAIYRYM</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\nQAIYRYM — волонтёрский проект в Актобе, помогаем семьям.\n\nВыбери подменю ↓", "kz": "💡 <b>QAIYRYM жобасы туралы</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\nQAIYRYM — Ақтөбе еріктіліктің жобасы."},
    "mission": {"ru": "🎯 <b>Миссия</b>\n\nСоздавать сообщество взаимопомощи, где каждый может помочь.", "kz": "🎯 <b>Миссия</b>\n\nӨзара көмектің қауымдастығын құру."},
    "creator": {"ru": "👤 <b>Создатель</b>\n\nПроект создан IT-HUB Актобе для помощи семьям.", "kz": "👤 <b>Жасушы</b>\n\nIT-HUB командасымен құрылды."},
    "partners": {"ru": "🤝 <b>Партнёры</b>\n\nШколы, НПО, волонтёры, спонсоры.", "kz": "🤝 <b>Серіктестер</b>\n\nМектептер, ҮЕҰ, еріктілер."},
    "details": {"ru": "📋 <b>Подробности</b>\n\nПолная информация: https://example.com", "kz": "📋 <b>Толық мәлімет</b>\n\nhttps://example.com"},
    "join_intro": {"ru": "🤝 <b>Как вступить?</b>\n\nДавайте зарегистрируемся!", "kz": "🤝 <b>Қалай қосылуға болады?</b>\n\nТіркелейік!"},
    "ask_name": {"ru": "Введи своё имя:", "kz": "Өз атыңды енгіз:"},
    "ask_age": {"ru": "Укажи свой возраст (цифрой):", "kz": "Жасыңды енгіз (цифрмен):"},
    "ask_skill": {"ru": "Расскажи о своих навыках:", "kz": "Дағдыларың туралы айт:"},
    "invalid_age": {"ru": "Введи возраст цифрой (например: 25)", "kz": "Жасыңды цифрмен енгіз:"},
    "underage": {"ru": "⚠️ Регистрация доступна с 18 лет.", "kz": "⚠️ Тіркеу 18 жастан бастап."},
    "registered": {"ru": "✅ Регистрация завершена! Добро пожаловать! 🎉", "kz": "✅ Тіркеу аяқталды! 🎉"},
    "chat_mode_on": {"ru": "💬 <b>Режим общения</b>\n\nПиши мне — я отвечу! 👇", "kz": "💬 <b>Сөйлесу режимі</b>\n\nМаған жаз! 👇"},
    "instruction": {"ru": "📘 <b>Инструкция</b>\n\n📖 Гайды: https://example.com/guides\n\n1️⃣ Как начать\n2️⃣ Безопасность\n3️⃣ Вопросы", "kz": "📘 <b>Нұсқаулық</b>\n\nhttps://example.com"},
    "menu_chat": {"ru": "💬 Общение", "kz": "💬 Сөйлесу"},
    "menu_about": {"ru": "💡 О проекте", "kz": "💡 Жоба туралы"},
    "menu_join": {"ru": "🤝 Как вступить?", "kz": "🤝 Қалай қосылуға болады?"},
    "menu_instruction": {"ru": "📘 Инструкция", "kz": "📘 Нұсқаулық"},
    "menu_profile": {"ru": "🧭 Профиль", "kz": "🧭 Профиль"},
    "back": {"ru": "🔙 Назад", "kz": "🔙 Артқа"},
    "use_menu_buttons": {"ru": "👇 Используйте кнопки меню.", "kz": "👇 Мәзір түймелерін пайдаланыңыз."},
}
# ═══════════════════════════════════════════════════════════════════════════
# FSM
# ═══════════════════════════════════════════════════════════════════════════
class OnboardingState(StatesGroup):
    choose_language = State()
    guest_menu = State()
    member_menu = State()
    chat_mode = State()
    about_submenu = State()
    registration_name = State()
    registration_age = State()
    registration_skill = State()
# ═══════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════════
def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Қазақша 🇰🇿", callback_data="lang:kz"),
         InlineKeyboardButton(text="Русский 🇷🇺", callback_data="lang:ru")]
    ])
def guest_menu_keyboard(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    lang = lang if lang in ("ru", "kz") else DEFAULT_LANG
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("menu_chat", lang), callback_data="menu:chat")],
        [InlineKeyboardButton(text=t("menu_about", lang), callback_data="menu:about")],
        [InlineKeyboardButton(text=t("menu_join", lang), callback_data="menu:join")],
    ])
def member_menu_keyboard(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    lang = lang if lang in ("ru", "kz") else DEFAULT_LANG
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t("menu_chat", lang), callback_data="menu:chat")],
        [InlineKeyboardButton(text=t("menu_about", lang), callback_data="menu:about")],
        [InlineKeyboardButton(text=t("menu_instruction", lang), callback_data="menu:instruction")],
        [InlineKeyboardButton(text=t("menu_profile", lang), callback_data="menu:profile")],
    ])
def about_submenu_keyboard(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    lang = lang if lang in ("ru", "kz") else DEFAULT_LANG
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Миссия", callback_data="about:mission")],
        [InlineKeyboardButton(text="👤 Создатель", callback_data="about:creator")],
        [InlineKeyboardButton(text="🤝 Партнёры", callback_data="about:partners")],
        [InlineKeyboardButton(text="📋 Подробности", callback_data="about:details")],
        [InlineKeyboardButton(text=t("back", lang), callback_data="menu:back_to_main")],
    ])
# ═══════════════════════════════════════════════════════════════════════════
# HANDLERS
# ═══════════════════════════════════════════════════════════════════════════
router = Router()
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user_id = str(message.from_user.id)
    logger.info(f"[START] User {user_id}")
    await state.clear()
    await state.set_state(OnboardingState.choose_language)
    await message.answer(t("choose_lang", DEFAULT_LANG), reply_markup=lang_keyboard())
@router.callback_query(F.data.startswith("lang:"))
async def process_lang(callback: CallbackQuery, state: FSMContext) -> None:
    lang = callback.data.split(":")[1]
    user_id = str(callback.from_user.id)
    set_user_language(user_id, lang)
    await state.update_data(lang=lang)
    role = get_user_role(user_id)
    logger.info(f"[LANG] User {user_id} выбрал {lang}, роль: {role}")
   
    if role == "MEMBER":
        await state.set_state(OnboardingState.member_menu)
        await callback.message.edit_text(t("intro_member", lang), reply_markup=member_menu_keyboard(lang))
    else:
        await state.set_state(OnboardingState.guest_menu)
        await callback.message.edit_text(t("intro_guest", lang), reply_markup=guest_menu_keyboard(lang))
    await callback.answer()
@router.callback_query(F.data == "menu:chat")
async def menu_chat(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    logger.info(f"[MENU] User {callback.from_user.id} -> Общение")
    await state.set_state(OnboardingState.chat_mode)
    await callback.message.answer(t("chat_mode_on", lang))
    await callback.answer()
@router.callback_query(F.data == "menu:about")
async def menu_about(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    logger.info(f"[MENU] User {callback.from_user.id} -> О проекте")
    await state.set_state(OnboardingState.about_submenu)
    await callback.message.edit_text(t("about", lang), reply_markup=about_submenu_keyboard(lang))
    await callback.answer()
@router.callback_query(F.data.startswith("about:"))
async def about_submenu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
   
    text_map = {
        "mission": t("mission", lang),
        "creator": t("creator", lang),
        "partners": t("partners", lang),
        "details": t("details", lang),
    }
    text = text_map.get(action, t("about", lang))
    await callback.message.edit_text(text, reply_markup=about_submenu_keyboard(lang))
    await callback.answer()
@router.callback_query(F.data == "menu:back_to_main")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    user_id = str(callback.from_user.id)
    role = get_user_role(user_id)
   
    if role == "MEMBER":
        await state.set_state(OnboardingState.member_menu)
        await callback.message.edit_text(t("intro_member", lang), reply_markup=member_menu_keyboard(lang))
    else:
        await state.set_state(OnboardingState.guest_menu)
        await callback.message.edit_text(t("intro_guest", lang), reply_markup=guest_menu_keyboard(lang))
    await callback.answer()
@router.callback_query(F.data == "menu:join")
async def menu_join(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    logger.info(f"[MENU] User {callback.from_user.id} -> Регистрация")
    await state.set_state(OnboardingState.registration_name)
    await callback.message.answer(t("join_intro", lang) + "\n\n" + t("ask_name", lang))
    await callback.answer()
@router.message(OnboardingState.registration_name, F.text)
async def reg_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    await state.update_data(name=name)
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    await state.set_state(OnboardingState.registration_age)
    await message.answer(t("ask_age", lang))
@router.message(OnboardingState.registration_age, F.text)
async def reg_age(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
   
    if not text.isdigit():
        await message.answer(t("invalid_age", lang))
        return
   
    age = int(text)
    if age < 18:
        await message.answer(t("underage", lang))
        await state.clear()
        return
   
    await state.update_data(age=age)
    await state.set_state(OnboardingState.registration_skill)
    await message.answer(t("ask_skill", lang))
@router.message(OnboardingState.registration_skill, F.text)
async def reg_skill(message: Message, state: FSMContext) -> None:
    skill = message.text.strip()
    user_id = str(message.from_user.id)
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    name = data.get("name", "")
    age = data.get("age", 0)
    username = message.from_user.username or ""
   
    success = save_user_registration(user_id, name, age, skill, lang, username)
   
    if success:
        await message.answer(t("registered", lang))
        logger.info(f"[REGISTRATION] Пользователь {user_id} зарегистрирован")
        await state.clear()
        await state.set_state(OnboardingState.member_menu)
        await message.answer(t("intro_member", lang), reply_markup=member_menu_keyboard(lang))
    else:
        await message.answer("❌ Ошибка при сохранении. Попробуйте позже.")
        await state.clear()
@router.callback_query(F.data == "menu:instruction")
async def menu_instruction(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    logger.info(f"[MENU] User {callback.from_user.id} -> Инструкция")
    await callback.message.answer(t("instruction", lang), reply_markup=member_menu_keyboard(lang))
    await callback.answer()
@router.callback_query(F.data == "menu:profile")
async def menu_profile(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    if not WEBAPP_URL:
        await callback.message.answer("❌ Мини-приложение пока не настроено.")
        await callback.answer()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await callback.message.answer("🧭 Откройте мини-приложение", reply_markup=keyboard)
    await callback.answer()
@router.message(OnboardingState.chat_mode, F.text)
async def chat_mode_message(message: Message, state: FSMContext) -> None:
    """Обработчик чата с историей и фильтрацией."""
    user_text = (message.text or "").strip()
    if not user_text:
        return
   
    # ФИЛЬТР: пропускаем пустые слова
    skip_words = ["ок", "да", "нет", "привет", "привет!", "ха", "оке", "хорошо", "спасибо", "пока"]
    if user_text.lower() in skip_words:
        logger.info(f"[CHAT] Пустое сообщение скипнуто: {user_text}")
        return
   
    user_id = str(message.from_user.id)
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    role = get_user_role(user_id)
   
    # ИНИЦИАЛИЗИРУЕМ или берем историю
    if "chat_history" not in data:
        data["chat_history"] = []
   
    chat_history = data["chat_history"]
    logger.info(f"[CHAT] User {user_id} ({role}) -> {user_text[:50]}... (история: {len(chat_history)} сообщений)")
   
    # ДОБАВЛЯЕМ сообщение в историю
    chat_history.append({"role": "user", "content": user_text})
   
    # Эффект печатания
    await message.bot.send_chat_action(chat_id=message.chat.id, action="typing")
   
    # ФОРМИРУЕМ системный промпт с длиной истории
    system_instruction = get_chat_system_instruction(lang, role=role, chat_history_len=len(chat_history))
   
    # ДОБАВЛЯЕМ контекст в конец
    if KNOWLEDGE_MANIFEST:
        system_instruction += f"\n\n[CONTEXT_DATA]\n{KNOWLEDGE_MANIFEST}\n[END_CONTEXT_DATA]"
   
    # ФОРМАТИРУЕМ историю для Gemini
    formatted_messages = []
    for msg in chat_history:
        prefix = "🧑 ПОЛЬЗОВАТЕЛЬ:" if msg["role"] == "user" else "🤖 КОМПАС:"
        formatted_messages.append(f"{prefix} {msg['content']}")
   
    full_prompt = "\n\n".join(formatted_messages)
   
    try:
        # ВЫЗЫВАЕМ Gemini
        reply = await ask_gemini(full_prompt, system_instruction, user_lang=lang, skip_lang_instruction=True)
       
        # СОХРАНЯЕМ ответ в историю
        chat_history.append({"role": "model", "content": reply})
       
        # ОГРАНИЧИВАЕМ память (max 20 сообщений)
        if len(chat_history) > 20:
            chat_history = chat_history[-20:]
       
        await state.update_data(chat_history=chat_history)
       
        # ОТПРАВЛЯЕМ ответ
        safe_reply = reply.replace("<", "&lt;").replace(">", "&gt;")
        await message.answer(safe_reply, parse_mode=ParseMode.HTML)
       
    except Exception as e:
        logger.error(f"[CHAT ERROR] {e}")
        await message.answer("Я немного завис, попробуй еще раз!")
@router.message(OnboardingState.guest_menu, F.text)
async def guest_menu_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    await message.answer(t("use_menu_buttons", lang), reply_markup=guest_menu_keyboard(lang))
@router.message(OnboardingState.member_menu, F.text)
async def member_menu_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    await message.answer(t("use_menu_buttons", lang), reply_markup=member_menu_keyboard(lang))
@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, bot: Bot) -> None:
    user_id = str(message.from_user.id)
    if not ADMIN_ID or user_id != ADMIN_ID:
        await message.answer("❌ Нет доступа.")
        return
   
    broadcast_text = message.text.replace("/broadcast", "").strip()
    if not broadcast_text:
        await message.answer("❌ Укажите текст: /broadcast <текст>")
        return
   
    logger.info(f"[BROADCAST] Администратор {user_id} отправляет рассылку")
    member_ids = get_all_member_ids()
   
    if not member_ids:
        await message.answer("❌ Нет участников.")
        return
   
    success_count = 0
    error_count = 0
   
    for member_id in member_ids:
        try:
            await bot.send_message(chat_id=int(member_id), text=broadcast_text)
            success_count += 1
        except Exception as e:
            error_count += 1
            logger.error(f"[BROADCAST ERROR] {member_id}: {e}")
   
    result_text = f"📤 <b>Рассылка завершена!</b>\n\n✅ Успешно: {success_count}\n❌ Ошибок: {error_count}"
    await message.answer(result_text)
@router.message(F.text, StateFilter(None))
async def handle_unknown(message: Message, state: FSMContext) -> None:
    await state.set_state(OnboardingState.choose_language)
    await message.answer(t("choose_lang", DEFAULT_LANG), reply_markup=lang_keyboard())
# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════
app = FastAPI(title="QAIYRYM Compass Bot", version="1.4")

# Монтируем статические файлы (для мини-app HTML)
app.mount("/static", StaticFiles(directory="static"), name="static")  # Положи ai_studio_code.html в папку static

@app.get("/")
@app.get("/health")
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": "1.4", "bot": "running"}

# API для пользователя (для мини-app)
@app.get("/user/{user_id}")
async def get_user_data(user_id: str):
    load_users_db()  # Обновляем БД
    user_data = USERS_DATA.get(user_id, {"role": "GUEST"})
    return user_data

# API для заявок (замени на реальный код с gspread, если есть лист "Заявки")
@app.get("/requests")
async def get_requests():
    # Здесь подключи gspread к листу "Заявки"
    # Пока mock
    return [
        {"type": "Продукты", "title": "Семья Ивановых", "desc": "Нужен продуктовый набор (5 детей)", "dist": "1.2 км"},
        {"type": "Медикаменты", "title": "Пенсионерка Анна", "desc": "Помощь в покупке лекарств", "dist": "0.5 км"},
        {"type": "Ремонт", "title": "Дом #12", "desc": "Помощь с покраской забора", "dist": "3.0 км"}
    ]

async def run_bot():
    try:
        load_users_db()
        bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(router)

        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("[BOT] Polling запущен")
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    except Exception as e:
        logger.error(f"[BOT FATAL] {type(e).__name__}: {e}", exc_info=True)

async def main():
    # Запускаем бота в фоне
    asyncio.create_task(run_bot())

    # Запускаем веб-сервер (Koyeb требует, чтобы процесс слушал порт)
    logger.info("[MAIN] Запуск uvicorn на 0.0.0.0:8000 ...")
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
        timeout_keep_alive=120,
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Получен сигнал завершения, выход...")
    except Exception as e:
        logger.critical(f"Критическая ошибка запуска: {e}", exc_info=True)
        raise