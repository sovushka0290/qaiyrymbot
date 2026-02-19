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
    sheet, sheet_name = get_sheets_client()
    if not sheet: return False
    try:
        worksheet = sheet.worksheet(sheet_name)
        row = [user_id, name, age, skill, lang, username, datetime.now().isoformat()]
        worksheet.append_row(row, value_input_option="RAW")
        logger.info(f"[SHEETS] Волонтёр {name} добавлен")
        return True
    except Exception as e:
        logger.error(f"[SHEETS ERROR] {e}")
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
                logger.info(f"[DB] Загружено {len(USERS_DATA)} пользователей")
        else:
            USERS_DATA = {}
    except Exception as e:
        logger.error(f"[DB ERROR] {e}")
        USERS_DATA = {}

def save_users_db():
    try:
        with open(USER_DB_FILE, "w", encoding="utf-8") as f:
            json.dump(USERS_DATA, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[DB ERROR] {e}")

def get_user_role(user_id: str) -> str:
    return USERS_DATA.get(user_id, {}).get("role", "GUEST")

def save_user_registration(user_id: str, name: str, age: int, skill: str, lang: str, username: str = "") -> bool:
    try:
        USERS_DATA[user_id] = {
            "user_id": user_id, "name": name, "age": age, "skill": skill,
            "lang": lang, "role": "MEMBER", "registered_at": datetime.now().isoformat()
        }
        save_users_db()
        logger.info(f"[DB] Пользователь {user_id} зарегистрирован: {name}")
        append_volunteer_to_sheets(user_id, name, age, skill, lang, username)
        return True
    except Exception as e:
        logger.error(f"[DB ERROR] {e}")
        return False

def set_user_language(user_id: str, lang: str):
    if user_id not in USERS_DATA:
        USERS_DATA[user_id] = {"role": "GUEST"}
    USERS_DATA[user_id]["lang"] = lang
    save_users_db()

def get_all_member_ids() -> List[str]:
    return [uid for uid, data in USERS_DATA.items() if data.get("role") == "MEMBER"]

# ==================== KNOWLEDGE.txt ====================
def load_manifest() -> str:
    paths = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge.txt"),
             os.path.join(os.getcwd(), "knowledge.txt")]
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                logger.info(f"[MANIFEST] Загружен knowledge.txt ({len(content)} символов)")
                return content
        except:
            continue
    logger.warning("[MANIFEST] knowledge.txt не найден")
    return ""

KNOWLEDGE_MANIFEST = load_manifest()

# ==================== GEMINI ====================
GEMINI_MODEL_NAME = "gemini-2.5-flash"
GEMINI_FALLBACK_MODEL = "gemini-2.0-flash"
_client = None

def get_gemini_client():
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("[GEMINI] Клиент инициализирован")
    return _client

def get_chat_system_instruction(user_lang: str, role: str = "GUEST", chat_history_len: int = 0) -> str:
    lang = user_lang if user_lang in ("ru", "kz") else "ru"
    lang_name = "русском" if lang == "ru" else "казахском"
    
    base = (
        "Ты — Компас, ИИ-координатор проекта QAIYRYM.\n\n"
        "🎯 ГЛАВНАЯ ЗАДАЧА — ЗАДАВАЙ ВОПРОСЫ! Ты ведёшь интервью.\n"
        "После каждого ответа обязательно задай 1-2 вопроса.\n\n"
        "СТРАТЕГИЯ: порциями, живо, дружелюбно.\n"
        f"• Язык: {lang_name} ({lang})\n"
        "• Форматируй ответ ТОЛЬКО Markdown: *жирный*, _курсив_, `код`, [текст](ссылка)\n"
        "• НЕ используй HTML-теги.\n"
    )
    
    if chat_history_len <= 2:
        base += "\n⭐ ПЕРВОЕ СООБЩЕНИЕ: Можно поздороваться и задать первый вопрос.\n"
    else:
        base += "\n⭐ Продолжай диалог без повторного приветствия.\n"
    
    if role == "MEMBER":
        base += "\n👤 РЕЖИМ УЧАСТНИКА: глубокие темы, детали помощи."
    else:
        base += "\n👤 РЕЖИМ ГОСТЯ: дружелюбно, поощряй присоединиться."
    
    return base

async def ask_gemini(prompt: str, system_prompt: str | None = None, user_lang: str = DEFAULT_LANG, skip_lang_instruction: bool = False) -> str:
    # ... (оставил как было, без изменений) ...
    base = system_prompt or ""
    if not skip_lang_instruction:
        lang = user_lang if user_lang in ("ru", "kz") else DEFAULT_LANG
        lang_name = "русском" if lang == "ru" else "казахском"
        system_instruction = f"{base}\n\nОтвечай на {lang_name} ({lang})."
    else:
        system_instruction = base
    
    def _generate_sync(model_name: str) -> str:
        client = get_gemini_client()
        config = types.GenerateContentConfig(max_output_tokens=512, system_instruction=system_instruction)
        response = client.models.generate_content(model=model_name, contents=prompt, config=config)
        return response.text.strip() if response.text else "Извините, не могу ответить."
    
    try:
        return await asyncio.wait_for(asyncio.to_thread(_generate_sync, GEMINI_MODEL_NAME), timeout=45.0)
    except Exception as e:
        logger.error(f"[GEMINI ERROR] {e}")
        return "К сожалению, не могу ответить."

# ==================== ТЕКСТЫ (увеличены) ====================
def t(key: str, lang: str) -> str:
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
    
    "mission": {"ru": "🎯 <b>Миссия</b>\n\nСоздавать сообщество взаимопомощи, где каждый может помочь.\nМы хотим, чтобы помощь была быстрой, прозрачной и честной.\nВместе мы делаем Актобе добрее.", "kz": "🎯 <b>Миссия</b>\n\nӨзара көмектің қауымдастығын құру.\nКөмек жылдам, ашық және адал болуы керек.\nБіз бірге Ақтөбені мейірімді етеміз."},
    
    "creator": {"ru": "👤 <b>Создатель</b>\n\nПроект создан IT-HUB Актобе для помощи семьям.\nИдея родилась из реальной проблемы — волонтёры не знали, куда идти и как помогать.\nЯ решил это исправить.", "kz": "👤 <b>Жасушы</b>\n\nЖоба IT-HUB Ақтөбе командасымен құрылды.\nИдея нақты мәселеден туындады — еріктілер қайда барарын білмеді.\nМен оны түзетуді шештім."},
    
    "partners": {"ru": "🤝 <b>Партнёры</b>\n\nШколы, НПО, волонтёры, спонсоры.\nМы открыты к сотрудничеству с каждым, кто хочет делать добро.\nВместе мы можем гораздо больше.", "kz": "🤝 <b>Серіктестер</b>\n\nМектептер, ҮЕҰ, еріктілер, демеушілер.\nБіз әрбір жақсылық жасағысы келетін адаммен ынтымақтасуға ашықпыз."},
    
    "details": {"ru": "📋 <b>Подробности</b>\n\nПолная информация о проекте, команда, планы и как присоединиться.\nВсё собрано на удобном лендинге.", "kz": "📋 <b>Толық мәлімет</b>\n\nЖоба туралы толық ақпарат, команда, жоспарлар және қалай қосылу керек."},
    
    # ... остальные тексты без изменений ...
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
    "menu_landing": {"ru": "🌐 Подробнее о проекте", "kz": "🌐 Толығырақ жоба туралы"},
    "back": {"ru": "🔙 Назад", "kz": "🔙 Артқа"},
    "use_menu_buttons": {"ru": "👇 Используйте кнопки меню.", "kz": "👇 Мәзір түймелерін пайдаланыңыз."},
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
        # КНОПКА НА САЙТ
        [InlineKeyboardButton(text=t("menu_landing", lang), 
                              web_app=WebAppInfo(url=f"{WEBAPP_URL.rsplit('/', 1)[0]}/landing.html"))],
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
    role = get_user_role(user_id)
    logger.info(f"[LANG] User {user_id} выбрал {lang}, роль: {role}")
    
    if role == "MEMBER":
        await state.set_state(OnboardingState.member_menu)
        await callback.message.answer(t("intro_member", lang), reply_markup=member_menu_keyboard(lang))
    else:
        await state.set_state(OnboardingState.guest_menu)
        await callback.message.answer(t("intro_guest", lang), reply_markup=guest_menu_keyboard(lang))
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

# (остальные обработчики registration, chat_mode и т.д. оставил как в твоей последней версии, без изменений)

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
        logger.error(f"[BOT FATAL] {type(e).__name__}: {e}", exc_info=True)

async def main():
    asyncio.create_task(run_bot())
    logger.info("[MAIN] Запуск uvicorn на 0.0.0.0:8000 ...")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Выход...")
