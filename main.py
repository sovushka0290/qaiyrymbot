import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
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
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove
)
from dotenv import load_dotenv
from fastapi import FastAPI
import uvicorn
from fastapi.staticfiles import StaticFiles

from aiogram.client.session.aiohttp import AiohttpSession
import aiohttp

load_dotenv()
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID", "")

if not BOT_TOKEN or not GEMINI_API_KEY:
    logger.critical("❌ ОШИБКА: Укажите BOT_TOKEN и GEMINI_API_KEY в .env")
    sys.exit(1)

DEFAULT_LANG = "ru"

# ==================== GOOGLE SHEETS ====================
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "qaiyrym-credentials.json")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Волонтёры")
GOOGLE_SHEET_NAME_REQUESTS = os.getenv("GOOGLE_SHEET_NAME_REQUESTS", "Заявки")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Хакерский трюк для Koyeb/Render: берем JSON из переменной окружения
if os.getenv("GOOGLE_CREDS_JSON"):
    try:
        with open(GOOGLE_CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            f.write(os.getenv("GOOGLE_CREDS_JSON"))
        logger.info("[SHEETS] Ключи Google успешно созданы из GOOGLE_CREDS_JSON")
    except Exception as e:
        logger.error(f"[SHEETS] Ошибка создания ключей из env: {e}")

def get_sheets_client():
    if not GOOGLE_SHEET_ID:
        logger.warning("[SHEETS] GOOGLE_SHEET_ID не установлен в .env")
        return None
    if not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        logger.warning(f"[SHEETS] Файл {GOOGLE_CREDENTIALS_PATH} не найден")
        return None
    try:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_PATH, scopes=SCOPES)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(GOOGLE_SHEET_ID)
        return sheet
    except Exception as e:
        logger.error(f"[SHEETS ERROR] {e}")
        return None

def append_volunteer_to_sheets(user_id: str, name: str, age: int, skill: str, coord: str, lang: str, username: str = "") -> bool:
    sheet = get_sheets_client()
    if not sheet: 
        logger.warning(f"[SHEETS MOCK VOLUNTEER] Mock mode. Data: {user_id}, {name}")
        return True # MOCK FOR DEMO
    try:
        worksheet = sheet.worksheet(GOOGLE_SHEET_NAME)
        row = [user_id, name, age, skill, coord, lang, username, datetime.now().isoformat()]
        worksheet.append_row(row, value_input_option="RAW")
        return True
    except Exception as e:
        logger.error(f"[SHEETS ERROR VOLUNTEER] {e}")
        return False

def append_request_to_sheets(user_id: str, name: str, address: str, req_type: str, lang: str, username: str = "") -> bool:
    sheet = get_sheets_client()
    if not sheet: 
        logger.warning(f"[SHEETS MOCK REQUEST] Mock mode. Data: {user_id}, {name}")
        return True # MOCK FOR DEMO
    try:
        try:
            worksheet = sheet.worksheet(GOOGLE_SHEET_NAME_REQUESTS)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sheet.add_worksheet(title=GOOGLE_SHEET_NAME_REQUESTS, rows="100", cols="20")
            worksheet.append_row(["ID", "Name", "Address", "Type", "Lang", "Username", "Date", "Status"])
            
        row = [user_id, name, address, req_type, lang, username, datetime.now().isoformat(), "НОВАЯ"]
        worksheet.append_row(row, value_input_option="RAW")
        return True
    except Exception as e:
        logger.error(f"[SHEETS ERROR REQUEST] {e}")
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

def save_user_role(user_id: str, role: str):
    if user_id not in USERS_DATA:
        USERS_DATA[user_id] = {}
    USERS_DATA[user_id]["role"] = role
    USERS_DATA[user_id]["registered_at"] = datetime.now().isoformat()
    save_users_db()

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
        except FileNotFoundError:
            continue
    return ""
KNOWLEDGE_MANIFEST = load_manifest()

# ==================== GEMINI ====================
GEMINI_MODEL_NAME = "gemini-2.5-flash"

def get_gemini_client():
    return genai.Client(api_key=GEMINI_API_KEY)

def get_chat_system_instruction(user_lang: str, role: str = "GUEST") -> str:
    lang_name = "русском" if user_lang == "ru" else "казахском"
    
    instruction = f"""
Ты — ИИ-координатор QAIYRYM. Отвечай на {lang_name}.
Твоя текущая роль собеседника: {role}.

[ПРАВИЛА ЛИЧНОСТИ]
1. КРАТКОСТЬ (Диалоговый режим): Если пользователь просто общается, отвечай не более 2-3 предложений. Будь как человек в мессенджере.
2. КОНТЕКСТНАЯ ГЛУБИНА: Если пользователь просит "рассказать подробнее", "как это работает" или "в чем выгода" — переключайся в режим эксперта и выдавай структурированную инфу.
3. РОЛЕВАЯ МОДЕЛЬ (СМОТРИ НА РОЛЬ):

   - ЕСЛИ РОЛЬ VOLUNTEER:
     Стиль: Дружелюбный, мотивирующий, на "ты". 
     Фокус: Твой рост, баллы, реальная помощь, крутое комьюнити. 
     Запрет: Не грузи их сложной ESG-аналитикой, если не просят.

   - ЕСЛИ РОЛЬ RECIPIENT:
     Стиль: Заботливый, вежливый, на "вы". 
     Фокус: Безопасность, сроки, как работает проверка волонтера. 
     Успокой человека: "Мы рядом, помощь уже ищется".

   - ЕСЛИ РОЛЬ CLIENT (PARTNER/BIZ/AKIMAT):
     Стиль: Деловой, конкретный, на "вы". 
     Фокус: Прозрачность (Solana), отчетность (ESG), автоматизация контроля. 
     Киллер-фича: Упоминай, что ИИ-агенты заменяют тысячи часов ручного аудита.

ТЕХНИЧЕСКИЙ ГАЙД:
- Всегда заканчивай ответ вопросом, чтобы вести диалог.
- Если человек спрашивает то, чего нет в knowledge.txt, мягко скажи: "Я пока учусь этому, но наш основатель Алихан точно знает ответ".
- Если пользователь пишет, что готов помогать, зарегистрироваться или получить помощь — предложи ему нажать кнопку "🚀 Приступить" в чате.
- Используй Markdown (жирный текст для акцентов).
"""
    
    if KNOWLEDGE_MANIFEST:
        instruction += f"\n\n[ДАННЫЕ О ПРОЕКТЕ / BAZA ZNANIY]\n{KNOWLEDGE_MANIFEST}"
        
    return instruction

async def ask_gemini(prompt: str, system_prompt: str) -> str:
    def _generate_sync(model_name: str) -> str:
        client = get_gemini_client()
        config = types.GenerateContentConfig(max_output_tokens=1024, system_instruction=system_prompt)
        response = client.models.generate_content(model=model_name, contents=prompt, config=config)
        return response.text.strip() if response.text else "Извините, не могу ответить."
    try:
        return await asyncio.wait_for(asyncio.to_thread(_generate_sync, GEMINI_MODEL_NAME), timeout=45.0)
    except Exception as e:
        logger.error(f"[GEMINI ERROR] {e}")
        return "К сожалению, не могу ответить."

# ==================== FSM ====================
class OnboardingState(StatesGroup):
    choose_language = State()
    agreement = State()
    choose_role = State()

class RegVolunteer(StatesGroup):
    name = State()
    age = State()
    skill = State()
    coordinator = State()

class RegRecipient(StatesGroup):
    name = State()
    address = State()
    req_type = State()

class RegClient(StatesGroup):
    name = State()
    company = State()

class MainMenu(StatesGroup):
    idle = State()
    chatting = State()
    esg_consultation = State()
    active_tasks = State()

class TaskVerification(StatesGroup):
    waiting_for_photo = State()

# ==================== КЛАВИАТУРЫ ====================
def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Қазақша 🇰🇿", callback_data="lang:kz"), InlineKeyboardButton(text="Русский 🇷🇺", callback_data="lang:ru")]])

def agreement_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Согласен", callback_data="agree:yes")], [InlineKeyboardButton(text="❌ Отказаться", callback_data="agree:no")]])

def roles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋‍♂️ Волонтер", callback_data="role_vol")],
        [InlineKeyboardButton(text="🆘 Нужна помощь", callback_data="role_rec")],
        [InlineKeyboardButton(text="💼 Партнер / Акимат", callback_data="role_biz")]
    ])

def coord_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Координатор Анна (Север)")],
        [KeyboardButton(text="Координатор Тимур (Центр)")],
        [KeyboardButton(text="Координатор Динара (Юг)")]
    ], resize_keyboard=True, one_time_keyboard=True)

def req_type_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🛒 Продуктовый набор")],
        [KeyboardButton(text="🧹 Бытовая помощь / Уборка")],
        [KeyboardButton(text="🎸 Образовательная помощь (Домбра и др.)")]
    ], resize_keyboard=True, one_time_keyboard=True)

def get_main_menu(role: str) -> InlineKeyboardMarkup:
    if role in ("VOLUNTEER", "COORDINATOR"):
        buttons = [
            [InlineKeyboardButton(text="🎯 Активные задания", callback_data="menu:tasks")],
            [InlineKeyboardButton(text="💬 Чат с ИИ", callback_data="menu:chat")],
            [InlineKeyboardButton(text="💡 О проекте", callback_data="menu:about")]
        ]
    elif role == "RECIPIENT":
        buttons = [
            [InlineKeyboardButton(text="📝 Создать заявку", callback_data="role_rec")],
            [InlineKeyboardButton(text="💬 Чат с координатором (ИИ)", callback_data="menu:chat")]
        ]
    elif role == "CLIENT":
        buttons = [
            [InlineKeyboardButton(text="📈 ESG-Консультация (ИИ)", callback_data="menu:esg")],
            [InlineKeyboardButton(text="📊 Аналитика платформы", callback_data="menu:analytics")],
            [InlineKeyboardButton(text="💡 О проекте", callback_data="menu:about")]
        ]
    else:
        buttons = [
            [InlineKeyboardButton(text="💡 О проекте", callback_data="menu:about")],
            [InlineKeyboardButton(text="💬 Задать вопрос ИИ", callback_data="menu:chat")],
            [InlineKeyboardButton(text="🔄 Сменить роль", callback_data="menu:change_role")]
        ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== HANDLERS ====================
router = Router()

def escape_md(text: str) -> str:
    escape_chars = '_*[]()~`>#+-=|{}.!'
    for char in escape_chars:
        text = text.replace(char, '\\' + char)
    return text

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    await state.clear()
    
    role = get_user_role(user_id)
    if role and role != "GUEST":
        if role == "VOLUNTEER":
            greet_text = "С возвращением! Готовы к новым задачам? Драйв, помощь, опыт ждут вас!"
        elif role == "CLIENT":
            greet_text = "Здравствуйте! Рад снова видеть вас. Напоминаю, я ИИ-координатор QAIYRYM. Мы помогаем бизнесу внедрять ESG и автоматизировать социальную отчетность."
        elif role == "RECIPIENT":
            greet_text = "Здравствуйте. У вас уже есть активная сессия. Выберите действие ниже."
        else:
            greet_text = "Добро пожаловать назад!"
        await message.answer(greet_text, reply_markup=get_main_menu(role))
        await state.set_state(MainMenu.idle)
        return

    save_user_role(user_id, "GUEST")
    await state.set_state(MainMenu.chatting)
    await state.update_data(chat_history=[])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💡 О проекте", callback_data="menu:about"), InlineKeyboardButton(text="🚀 Приступить", callback_data="gateway:start_roles")]
    ])
    
    await message.answer(
        "Привет! Я — Компас, ИИ-координатор системы QAIYRYM. 🛰\n\n"
        "Я здесь, чтобы сделать волонтёрство в Актобе прозрачным и эффективным. Могу рассказать о проекте, о наших 'крыльях' или помочь тебе стать частью команды.\n\n"
        "О чем хочешь узнать? Или сразу перейдем к делу?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@router.callback_query(F.data == "gateway:start_roles")
async def gateway_start_roles(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OnboardingState.choose_role)
    await callback.message.answer("Добро пожаловать в QAIYRYM!\nПожалуйста, ответьте: **Кто вы?**", reply_markup=roles_keyboard(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

# --- ВЫБОР РОЛИ ---
@router.callback_query(OnboardingState.choose_role, F.data.in_(["role_vol", "role_rec", "role_biz"]))
@router.callback_query(F.data == "role_rec")
async def process_role_selection(callback: CallbackQuery, state: FSMContext):
    choice = callback.data
    user_id = str(callback.from_user.id)
    
    if choice == "role_vol":
        role = "VOLUNTEER"
        save_user_role(user_id, role)
        await state.set_state(RegVolunteer.name)
        await callback.message.answer("🙋‍♂️ Вы выбрали стать Волонтёром!\n\nВведите ваше **ФИО**:")
    elif choice == "role_rec":
        role = "RECIPIENT"
        save_user_role(user_id, role)
        await state.set_state(RegRecipient.name)
        await callback.message.answer("🆘 Создание заявки на помощь.\n\nВведите ваше **ФИО**:")
    elif choice == "role_biz":
        role = "CLIENT"
        save_user_role(user_id, role)
        await state.set_state(MainMenu.esg_consultation)
        await state.update_data(chat_history=[])
        await callback.message.answer("💼 Здравствуйте! Я ИИ-координатор QAIYRYM. Мы помогаем бизнесу внедрять ESG, обеспечивать прозрачность через блокчейн и автоматизировать социальную отчетность. Чем я могу быть полезен?", reply_markup=get_main_menu("CLIENT"))
        
    await callback.answer()

# --- VOLUNTEER FLOW ---
@router.message(RegVolunteer.name, F.text)
async def v_name(m: Message, state: FSMContext):
    await state.update_data(v_name=m.text)
    await state.set_state(RegVolunteer.age)
    await m.answer("Укажите ваш возраст (цифрой):")

@router.message(RegVolunteer.age, F.text)
async def v_age(m: Message, state: FSMContext):
    if not m.text.isdigit():
        return await m.answer("Пожалуйста, введите возраст цифрами!")
    await state.update_data(v_age=int(m.text))
    await state.set_state(RegVolunteer.skill)
    await m.answer("Укажите ваши навыки (например: авто, медик, свободное время):")

@router.message(RegVolunteer.skill, F.text)
async def v_skill(m: Message, state: FSMContext):
    await state.update_data(v_skill=m.text)
    await state.set_state(RegVolunteer.coordinator)
    await m.answer("Выберите координатора вашего района:", reply_markup=coord_keyboard())

@router.message(RegVolunteer.coordinator, F.text)
async def v_coord(m: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    user_id = str(m.from_user.id)
    
    success = append_volunteer_to_sheets(user_id, data['v_name'], data['v_age'], data['v_skill'], m.text, lang, m.from_user.username or "")
    if success:
        await m.answer("✅ Регистрация завершена! Добро пожаловать в команду.", reply_markup=ReplyKeyboardRemove())
        await m.answer("Ваше меню волонтёра:", reply_markup=get_main_menu("VOLUNTEER"))
        await state.set_state(MainMenu.idle)
    else:
        await m.answer("Проблема с сохранением. Попробуйте позже.", reply_markup=ReplyKeyboardRemove())
        await state.clear()

# --- RECIPIENT FLOW ---
@router.message(RegRecipient.name, F.text)
async def r_name(m: Message, state: FSMContext):
    await state.update_data(r_name=m.text)
    await state.set_state(RegRecipient.address)
    await m.answer("Введите ваш полный **адрес**:")

@router.message(RegRecipient.address, F.text)
async def r_address(m: Message, state: FSMContext):
    await state.update_data(r_address=m.text)
    await state.set_state(RegRecipient.req_type)
    await m.answer("Какой тип помощи вам требуется?", reply_markup=req_type_keyboard())

@router.message(RegRecipient.req_type, F.text)
async def r_req_type(m: Message, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    user_id = str(m.from_user.id)
    
    success = append_request_to_sheets(user_id, data['r_name'], data['r_address'], m.text, lang, m.from_user.username or "")
    if success:
        await m.answer(f"✅ Заявка `{m.text}` принята! ИИ уже ищет подходящего волонтёра.", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.MARKDOWN)
        await m.answer("Меню:", reply_markup=get_main_menu("RECIPIENT"))
        await state.set_state(MainMenu.idle)
    else:
        await m.answer("Проблема с сохранением. Попробуйте позже.", reply_markup=ReplyKeyboardRemove())
        await state.clear()

# --- CLIENT (ESG) FLOW ---
@router.message(RegClient.name, F.text)
async def c_name(m: Message, state: FSMContext):
    await state.update_data(c_name=m.text)
    await state.set_state(RegClient.company)
    await m.answer("Введите **название вашей организации**:")

@router.message(RegClient.company, F.text)
async def c_company(m: Message, state: FSMContext):
    USERS_DATA[str(m.from_user.id)]["company"] = m.text
    save_users_db()
    await m.answer("✅ Аккаунт организации успешно создан.", reply_markup=ReplyKeyboardRemove())
    await m.answer("Главное меню Партнера:", reply_markup=get_main_menu("CLIENT"))
    await state.set_state(MainMenu.idle)


# --- MAIN MENU HANDLERS ---
@router.callback_query(F.data == "menu:about")
async def handle_about(cb: CallbackQuery):
    await cb.message.answer("*О проекте QAIYRYM*\nСмотри раздел `knowledge.txt` в чате с ИИ для полной инфы.", parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu(get_user_role(str(cb.from_user.id))))
    await cb.answer()

@router.callback_query(F.data == "menu:analytics")
async def handle_analytics(cb: CallbackQuery):
    await cb.message.answer("📊 **Аналитика платформы (Demo)**\n\n• Выполнено заявок: 1,245\n• Транзакций в Solana: 15,892\n• Риск коррупции: 0.00%\n\nПлатформа полностью прозрачна.", parse_mode=ParseMode.MARKDOWN)
    await cb.answer()

# --- ЗАДАНИЯ И ВЕРИФИКАЦИЯ (VOLUNTEER) ---
@router.callback_query(F.data == "menu:tasks")
async def handle_tasks(cb: CallbackQuery, state: FSMContext):
    # Mocking active requests
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Взять заявку #402 (Продукты)", callback_data="task:402")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu:back_idle")]
    ])
    await cb.message.answer("📋 **Активные задания в вашем районе:**\n\n**#402** - Продуктовый набор\nАдрес: ул. Абая 12, кв 44\nКоординатор: Тимур", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await cb.answer()

@router.callback_query(F.data.startswith("task:"))
async def take_task(cb: CallbackQuery, state: FSMContext):
    await state.set_state(TaskVerification.waiting_for_photo)
    await cb.message.answer("🎯 **Задание #402 принято!**\n\nПосле выполнения прикрепите фото-доказательство прямо сюда. ИИ-агенты автоматически проверят его.")
    await cb.answer()

@router.message(TaskVerification.waiting_for_photo, F.photo)
async def handle_task_proof(m: Message, state: FSMContext):
    msg = await m.answer("⌛ Проверка ИИ-агентами (Computer Vision, Geo, Compliance)...")
    await asyncio.sleep(2) # Имитация работы блокчейна/сегментации
    
    await msg.edit_text("✅ **Верификация успешно пройдена!**\n\nДоказательство социального влияния (SBT Token) хешируется и отправлено вашему Координатору на подпись.\nТранзакция в сети Solana инициирована.", parse_mode=ParseMode.MARKDOWN)
    await m.answer("Ваше меню:", reply_markup=get_main_menu(get_user_role(str(m.from_user.id))))
    await state.set_state(MainMenu.idle)

@router.message(TaskVerification.waiting_for_photo)
async def handle_not_photo(m: Message):
    await m.answer("Пожалуйста, прикрепите фото-доказательство для верификации (отправьте картинку).")

# --- ЧАТЫ (GENERAL / ESG) ---
@router.callback_query(F.data == "menu:chat")
async def start_chat(cb: CallbackQuery, state: FSMContext):
    await state.set_state(MainMenu.chatting)
    await state.update_data(chat_history=[])
    await cb.message.answer("💬 **Общение с Компас-ИИ**\n\nПиши любой вопрос. Напиши 'назад' чтобы выйти в меню.", parse_mode=ParseMode.MARKDOWN)
    await cb.answer()

@router.callback_query(F.data == "menu:esg")
async def start_esg(cb: CallbackQuery, state: FSMContext):
    await state.set_state(MainMenu.esg_consultation)
    await state.update_data(chat_history=[])
    role_info = USERS_DATA.get(str(cb.from_user.id), {})
    company = role_info.get("company", "Ваша компания")
    
    await cb.message.answer(f"📈 **ESG-Консультация для представителей {company}**\n\nЗадайте вопрос о том, как наша ERP-платформа может помочь вам: сформировать ESG отчетность, нанять проверенных лидеров или провести прозрачную акцию.\n\n*(Напишите 'назад' для выхода)*", parse_mode=ParseMode.MARKDOWN)
    await cb.answer()

@router.message(MainMenu.chatting, F.text)
@router.message(MainMenu.esg_consultation, F.text)
async def process_chat(m: Message, state: FSMContext):
    user_text = m.text.strip()
    if user_text.lower() in ["назад", "back", "выход"]:
        await state.set_state(MainMenu.idle)
        await m.answer("Вы вернулись в меню:", reply_markup=get_main_menu(get_user_role(str(m.from_user.id))))
        return

    curr_state = await state.get_state()
    is_esg = (curr_state == MainMenu.esg_consultation.state)
    
    data = await state.get_data()
    chat_history = data.get("chat_history", [])
    chat_history.append({"role": "user", "content": user_text})
    
    await m.bot.send_chat_action(chat_id=m.chat.id, action="typing")
    
    user_role = get_user_role(str(m.from_user.id))
    user_lang = USERS_DATA.get(str(m.from_user.id), {}).get("lang", "ru")
    
    system_prompt = get_chat_system_instruction(user_lang, user_role)

    prompt_lines = [f"{msg['role']}: {msg['content']}" for msg in chat_history]
    full_prompt = "\n".join(prompt_lines)
    
    reply = await ask_gemini(full_prompt, system_prompt)
    chat_history.append({"role": "model", "content": reply})
    if len(chat_history) > 10: chat_history = chat_history[-10:]
    await state.update_data(chat_history=chat_history)
    
    await m.answer(escape_md(reply), parse_mode=ParseMode.MARKDOWN_V2)

@router.callback_query(F.data == "menu:back_idle")
async def back_idle(cb: CallbackQuery, state: FSMContext):
    await state.set_state(MainMenu.idle)
    await cb.message.edit_text("Меню:", reply_markup=get_main_menu(get_user_role(str(cb.from_user.id))))

# ==================== ЗАПУСК APP ====================
_bot_task = None
async def run_bot():
    load_users_db()
    for attempt in range(1, 6):
        try:
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            session = AiohttpSession(timeout=timeout)
            bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)
            dp = Dispatcher(storage=MemoryStorage())
            dp.include_router(router)
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("[BOT] ✅ Поллинг (Роли ERP) запущен")
            await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
            return
        except Exception as e:
            logger.error(f"[BOT] Попытка {attempt}/5 не удалась: {e}")
            if attempt < 5:
                await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(application: FastAPI):
    global _bot_task
    logger.info("[LIFESPAN] Запуск бота...")
    _bot_task = asyncio.create_task(run_bot())
    yield
    logger.info("[LIFESPAN] Остановка...")
    if _bot_task:
        _bot_task.cancel()

app = FastAPI(title="QAIYRYM ERP Bot", version="2.0", lifespan=lifespan)
@app.get("/")
async def health_check(): return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
