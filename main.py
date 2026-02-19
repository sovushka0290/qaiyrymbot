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
from fastapi.staticfiles import StaticFiles

load_dotenv()
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

if not BOT_TOKEN or not GEMINI_API_KEY:
    logger.critical("❌ ОШИБКА: Укажите BOT_TOKEN и GEMINI_API_KEY в .env")
    sys.exit(1)

DEFAULT_LANG = "ru"

# ==================== GOOGLE SHEETS ====================
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "qaiyrym-credentials.json")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Волонтёры")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheets_client():
    if not GOOGLE_SHEET_ID: return None, None
    if not os.path.exists(GOOGLE_CREDENTIALS_PATH): return None, None
    try:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(GOOGLE_SHEET_ID)
        return sheet, GOOGLE_SHEET_NAME
    except:
        return None, None

def append_volunteer_to_sheets(user_id: str, name: str, age: int, skill: str, lang: str, username: str = "") -> bool:
    sheet, sheet_name = get_sheets_client()
    if not sheet: return False
    try:
        worksheet = sheet.worksheet(sheet_name)
        row = [user_id, name, age, skill, lang, username, datetime.now().isoformat()]
        worksheet.append_row(row, value_input_option="RAW")
        return True
    except:
        return False

# ==================== БАЗА ДАННЫХ ====================
USER_DB_FILE = "users_db.json"
USERS_DATA: Dict[str, Dict[str, Any]] = {}

def load_users_db():
    global USERS_DATA
    try:
        if os.path.exists(USER_DB_FILE):
            with open(USER_DB_FILE, "r", encoding="utf-8") as f:
                USERS_DATA = json.load(f)
    except:
        USERS_DATA = {}

def save_users_db():
    try:
        with open(USER_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(USERS_DATA, f, ensure_ascii=False, indent=2)
    except:
        pass

def get_user_role(user_id: str) -> str:
    return USERS_DATA.get(user_id, {}).get("role", "GUEST")

def save_user_registration(user_id: str, name: str, age: int, skill: str, lang: str, username: str = "") -> bool:
    try:
        USERS_DATA[user_id] = {
            "user_id": user_id, "name": name, "age": age, "skill": skill,
            "lang": lang, "role": "MEMBER", "registered_at": datetime.now().isoformat()
        }
        save_users_db()
        append_volunteer_to_sheets(user_id, name, age, skill, lang, username)
        return True
    except:
        return False

def set_user_language(user_id: str, lang: str):
    if user_id not in USERS_DATA:
        USERS_DATA[user_id] = {"role": "GUEST"}
    USERS_DATA[user_id]["lang"] = lang
    save_users_db()

# ==================== KNOWLEDGE & GEMINI ====================
def load_manifest() -> str:
    for path in [os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.txt"), "knowledge.txt"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except:
            continue
    return ""

KNOWLEDGE_MANIFEST = load_manifest()

GEMINI_MODEL_NAME = "gemini-2.5-flash"
_client = None

def get_gemini_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client

def get_chat_system_instruction(user_lang: str, role: str = "GUEST", chat_history_len: int = 0) -> str:
    lang_name = "русском" if user_lang == "ru" else "казахском"
    base = f"Ты — Компас, ИИ-координатор QAIYRYM. Отвечай на {lang_name}.\n\n"
    base += "После каждого ответа задавай 1-2 вопроса. Используй Markdown: *жирный*, _курсив_, `код`.\n"
    return base

async def ask_gemini(prompt: str, system_prompt: str | None = None) -> str:
    try:
        client = get_gemini_client()
        config = types.GenerateContentConfig(max_output_tokens=512, system_instruction=system_prompt)
        response = client.models.generate_content(model=GEMINI_MODEL_NAME, contents=prompt, config=config)
        return response.text.strip() or "Извините, не могу ответить."
    except Exception as e:
        logger.error(f"[GEMINI ERROR] {e}")
        return "К сожалению, не могу ответить."

# ==================== ТЕКСТЫ ====================
def t(key: str, lang: str) -> str:
    lang = lang if lang in ("ru", "kz") else DEFAULT_LANG
    val = TEXTS.get(key, {})
    return val.get(lang, val.get(DEFAULT_LANG, "Текст не найден"))

TEXTS = {
    "choose_lang": {"ru": "Выберите язык:", "kz": "Тілді таңдаңыз:"},
    "intro_guest": {"ru": "Я — Компас, твой координатор QAIYRYM. Выбери действие:", "kz": "Мен — Компас. Әрекетті таңдаңыз:"},
    "intro_member": {"ru": "Привет, участник! 🎉 Чем могу помочь?", "kz": "Сәлем, қатысушы! 🎉"},
    "about": {"ru": "💡 <b>О проекте QAIYRYM</b>\n━━━━━━━━━━━━━━━━━━━━━━\n\nQAIYRYM — волонтёрский проект в Актобе.", "kz": "💡 <b>QAIYRYM жобасы туралы</b>\n━━━━━━━━━━━━━━━━━━━━━━"},
    "menu_chat": {"ru": "💬 Общение с ИИ", "kz": "💬 ИИ-мен сөйлесу"},
    "menu_about": {"ru": "💡 О проекте", "kz": "💡 Жоба туралы"},
    "menu_join": {"ru": "🤝 Как вступить?", "kz": "🤝 Қалай қосылуға болады?"},
    "menu_instruction": {"ru": "📘 Инструкция", "kz": "📘 Нұсқаулық"},
    "menu_profile": {"ru": "🧭 Профиль", "kz": "🧭 Профиль"},
    "menu_landing": {"ru": "🌐 Подробнее о проекте", "kz": "🌐 Толығырақ жоба туралы"},
    "back": {"ru": "🔙 Назад", "kz": "🔙 Артқа"},
}

# ==================== FSM ====================
class OnboardingState(StatesGroup):
    choose_language = State()
    guest_menu = State()
    member_menu = State()
    chat_mode = State()
    about_submenu = State()
    registration_name = State()
    registration_age = State()
    registration_skill = State()

# ==================== КЛАВИАТУРЫ ====================
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
        [InlineKeyboardButton(text=t("menu_landing", lang), web_app=WebAppInfo(url=f"{WEBAPP_URL.rsplit('/', 1)[0]}/landing.html"))],
        [InlineKeyboardButton(text=t("back", lang), callback_data="menu:back_to_main")],
    ])

# ==================== HANDLERS ====================
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
    logger.info(f"[LANG] User {user_id} выбрал {lang}")
    await state.set_state(OnboardingState.guest_menu)
    await callback.message.answer(t("intro_guest", lang), reply_markup=guest_menu_keyboard(lang))
    await callback.answer()

@router.callback_query(F.data == "menu:about")
async def menu_about(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    await state.set_state(OnboardingState.about_submenu)
    await callback.message.edit_text(t("about", lang), reply_markup=about_submenu_keyboard(lang))
    await callback.answer()

@router.callback_query(F.data.startswith("about:"))
async def about_submenu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    text_map = {"mission": t("mission", lang), "creator": t("creator", lang),
                "partners": t("partners", lang), "details": t("details", lang)}
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
    await state.set_state(OnboardingState.registration_name)
    await callback.message.answer(t("join_intro", lang) + "\n\n" + t("ask_name", lang))
    await callback.answer()

@router.message(OnboardingState.registration_name, F.text)
async def reg_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(OnboardingState.registration_age)
    await message.answer(t("ask_age", DEFAULT_LANG))

@router.message(OnboardingState.registration_age, F.text)
async def reg_age(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit():
        await message.answer(t("invalid_age", DEFAULT_LANG))
        return
    age = int(message.text.strip())
    if age < 18:
        await message.answer(t("underage", DEFAULT_LANG))
        await state.clear()
        return
    await state.update_data(age=age)
    await state.set_state(OnboardingState.registration_skill)
    await message.answer(t("ask_skill", DEFAULT_LANG))

@router.message(OnboardingState.registration_skill, F.text)
async def reg_skill(message: Message, state: FSMContext) -> None:
    skill = message.text.strip()
    user_id = str(message.from_user.id)
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    name = data.get("name", "")
    age = data.get("age", 0)
    username = message.from_user.username or ""
    save_user_registration(user_id, name, age, skill, lang, username)
    await message.answer(t("registered", lang))
    await state.clear()
    await state.set_state(OnboardingState.member_menu)
    await message.answer(t("intro_member", lang), reply_markup=member_menu_keyboard(lang))

@router.callback_query(F.data == "menu:chat")
async def menu_chat(callback: CallbackQuery, state: FSMContext) -> None:
    lang = (await state.get_data()).get("lang") or DEFAULT_LANG
    await state.set_state(OnboardingState.chat_mode)
    await callback.message.answer(t("chat_mode_on", lang))
    await callback.answer()

@router.message(OnboardingState.chat_mode, F.text)
async def chat_mode_message(message: Message, state: FSMContext) -> None:
    user_text = message.text.strip()
    if not user_text: return
    user_id = str(message.from_user.id)
    data = await state.get_data()
    lang = data.get("lang") or DEFAULT_LANG
    role = get_user_role(user_id)
    if "chat_history" not in data:
        data["chat_history"] = []
    chat_history = data["chat_history"]
    chat_history.append({"role": "user", "content": user_text})
    await message.bot.send_chat_action(message.chat.id, "typing")
    system_instruction = get_chat_system_instruction(lang, role, len(chat_history))
    if KNOWLEDGE_MANIFEST:
        system_instruction += f"\n\n[CONTEXT_DATA]\n{KNOWLEDGE_MANIFEST}\n[END_CONTEXT_DATA]"
    formatted = [f"{'🧑 ПОЛЬЗОВАТЕЛЬ' if m['role']=='user' else '🤖 КОМПАС'}: {m['content']}" for m in chat_history]
    reply = await ask_gemini("\n\n".join(formatted), system_instruction)
    chat_history.append({"role": "model", "content": reply})
    if len(chat_history) > 20:
        chat_history = chat_history[-20:]
    await state.update_data(chat_history=chat_history)
    await message.answer(reply, parse_mode=ParseMode.MARKDOWN_V2)

# ==================== MAIN ====================
app = FastAPI(title="QAIYRYM Compass Bot", version="1.4")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def health_check():
    return {"status": "ok", "version": "1.4", "bot": "running"}

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
        logger.error(f"[BOT FATAL] {e}")

async def main():
    asyncio.create_task(run_bot())
    logger.info("[MAIN] Запуск uvicorn...")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Выход...")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
