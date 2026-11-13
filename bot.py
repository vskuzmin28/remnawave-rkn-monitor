import asyncio
import logging
import os
import json
import sys
import socket
import aiohttp
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- ЗАГРУЗКА КОНФИГУРАЦИИ ИЗ .ENV ---
load_dotenv()

# Читаем переменные. Если BOT_TOKEN нет - бот не запустится (защита от дурака)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env файле! Создайте файл .env и добавьте туда токен.")

# Парсим ID админов из строки "123,456" в список чисел [123, 456]
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().lstrip('-').isdigit()]

# Парсим остальные настройки
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "60"))

# ID темы (может быть пустым или None)
_target_thread = os.getenv("TARGET_THREAD_ID")
TARGET_THREAD_ID = int(_target_thread) if _target_thread and _target_thread.isdigit() else None

PANEL_URL = os.getenv("PANEL_URL", "")
API_TOKEN = os.getenv("API_TOKEN", "")

SITES_FILE = "sites.txt"
STATE_FILE = "state.json"

# --- СПРАВОЧНИК НОД (Запасной вариант) ---
NODE_METADATA = {
    "status.vitosh.ru": {"node": "Panel [DE] Remnawave", "ip": "138.124.228.231", "provider": "nuxt.cloud"},
    "cdn001.vitosh.ru": {"node": "CDN", "ip": "yandex.cloud", "provider": "yandex.cloud"},
    "details.vitosh.ru": {"node": "Exit [FI] node #2", "ip": "62.60.226.45", "provider": "nuxt.cloud"},
    "cdn031.vitosh.ru": {"node": "Exit [FI] node #3", "ip": "45.14.165.203", "provider": "hip.hosting"},
    "os.vitosh.ru":     {"node": "Exit [DE] node #5", "ip": "152.234.209.185", "provider": "nuxt.cloud"},
    "fabric.vitosh.ru": {"node": "Entry [RU] node #1", "ip": "77.83.244.190", "provider": "nuxt.cloud"},
    "depends.vitosh.ru":{"node": "Exit [DE] node #4", "ip": "48.102.185.144", "provider": "nuxt.cloud"},
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- MIDDLEWARE ДЛЯ ПРОВЕРКИ ДОСТУПА ---
class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: callable,
        event: types.TelegramObject,
        data: dict
    ) -> any:
        # Получаем ID пользователя, который написал или нажал кнопку
        user_id = event.from_user.id if event.from_user else None
        
        # Проверяем, есть ли ID пользователя в списке разрешенных (игнорируем отрицательные ID групп)
        allowed_users = [uid for uid in ADMIN_IDS if uid > 0]
        
        if user_id not in allowed_users:
            # Если это личные сообщения, вежливо отказываем. В группах просто игнорируем.
            if isinstance(event, types.Message) and event.chat.type == "private":
                await event.answer("⛔ У вас нет прав на использование этого бота.")
            elif isinstance(event, types.CallbackQuery):
                await event.answer("⛔ У вас нет прав.", show_alert=True)
            return  # Блокируем выполнение дальнейших команд
        
        # Если пользователь разрешен — передаем управление дальше
        return await handler(event, data)


# --- РАБОТА С ФАЙЛАМИ ---
def get_tracked_sites() -> list[str]:
    if not os.path.exists(SITES_FILE):
        return []
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        sites = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
    return list(dict.fromkeys(sites))

def save_tracked_sites(sites: list[str]):
    with open(SITES_FILE, "w", encoding="utf-8") as f:
        for site in sites:
            f.write(f"{site}\n")

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=4)


# --- ФОРМИРОВАНИЕ ОТЧЕТА ---
def format_sites_report(sites: list[str], states: dict) -> str:
    if not sites:
        return "Список отслеживаемых сайтов пуст."

    sites_data = []
    for s in sites:
        meta = NODE_METADATA.get(s, {})
        last_status = states.get(s, "unknown")
        
        if last_status == "blocked":
            status_text = "🚨 Заблокирован"
            priority = 0
        elif last_status == "ok":
            status_text = "✅ Доступен"
            priority = 1
        else:
            status_text = "❓ Неизвестно (нет проверок)"
            priority = 2
            
        sites_data.append({
            "site": s,
            "priority": priority,
            "status_text": status_text,
            "node_name": meta.get('node', 'Неизвестно'),
            "ip_addr": meta.get('ip', 'Неизвестно'),
            "provider": meta.get('provider', 'Неизвестно')
        })

    sites_data.sort(key=lambda x: x["priority"])

    sites_list = ""
    for item in sites_data:
        sites_list += (
            f"📌 **Последний статус:** {item['status_text']}\n"
            f"🖥 **Нода:** {item['node_name']}\n"
            f"🌐 **Айпи:** `{item['ip_addr']}`\n"
            f"🔗 **Домен:** `{item['site']}`\n"
            f"☁️ **Провайдер:** {item['provider']}\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
        )
    return sites_list


# --- ИНТЕГРАЦИЯ С REMNAWAVE ---
async def sync_with_remnawave():
    if not PANEL_URL or not API_TOKEN or "your-panel" in PANEL_URL:
        logging.warning("Интеграция с Remnawave не настроена (проверьте PANEL_URL и API_TOKEN).")
        return

    logging.info("--- Запуск синхронизации с Remnawave ---")
    base_url = PANEL_URL.rstrip("/")
    api_url = f"{base_url}/api/nodes"
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, headers=headers, timeout=15) as response:
                if response.status != 200:
                    logging.error(f"Ошибка API Remnawave: статус {response.status}, текст: {await response.text()}")
                    return
                
                data = await response.json()
                nodes = []
                if isinstance(data, dict):
                    if "response" in data and isinstance(data["response"], list):
                        nodes = data["response"]
                    elif "nodes" in data and isinstance(data["nodes"], list):
                        nodes = data["nodes"]
                    else:
                        nodes = list(data.values())
                elif isinstance(data, list):
                    nodes = data
                
                if not isinstance(nodes, list):
                    logging.error(f"Не удалось найти список нод в ответе API.")
                    return

                new_sites = []
                new_metadata = {}
                
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    site = node.get("address") or node.get("domain")
                    if not site:
                        continue
                    
                    real_ip = node.get("ip")
                    if not real_ip:
                        ips_list = node.get("ips")
                        if isinstance(ips_list, list) and len(ips_list) > 0 and isinstance(ips_list[0], dict):
                            real_ip = ips_list[0].get("ip")
                    if not real_ip:
                        try:
                            real_ip = await asyncio.to_thread(socket.gethostbyname, site)
                        except Exception:
                            real_ip = "Неизвестно"

                    provider_data = node.get("provider", {})
                    provider_name = provider_data.get("name", "Unknown") if isinstance(provider_data, dict) else str(provider_data)
                    
                    country_code = node.get("countryCode", "")
                    flag = "".join(chr(ord(c.upper()) + 127397) for c in country_code) + " " if country_code else ""
                    node_name = f"{flag}{node.get('name', 'Unknown')}"

                    new_sites.append(site)
                    new_metadata[site] = {
                        "node": node_name,
                        "ip": real_ip,
                        "provider": provider_name
                    }
                
                if new_sites:
                    save_tracked_sites(new_sites)
                    global NODE_METADATA
                    NODE_METADATA = new_metadata
                    logging.info(f"Синхронизация завершена. Загружено {len(new_sites)} нод.")
                else:
                    logging.warning("API Remnawave вернул пустой список нод.")
    except Exception as e:
        logging.error(f"Ошибка синхронизации с Remnawave: {e}")
    logging.info("--- Синхронизация завершена ---")


# --- ПЕРЕВОДЧИК РЕЗУЛЬТАТОВ RKN-CHECK ---
def interpret_verdict(result: dict) -> tuple[bool, str, str, str]:
    if not result:
        return False, "❓", "Ошибка проверки", "Не удалось получить ответ."
    verdict = result.get("verdict", "OK").upper()
    tcp_ok = result.get("tcp_ok", True)
    tls_ok = result.get("tls_ok", True)
    notes = result.get("notes", [])

    translated_notes = []
    for note in notes:
        if "timeout" in note.lower(): translated_notes.append("Таймаут")
        elif "reset" in note.lower(): translated_notes.append("Сброс")
        elif "dns" in note.lower(): translated_notes.append("DNS")
        else: translated_notes.append(note)
    details = f" ({'; '.join(translated_notes)})" if translated_notes else ""

    if verdict == "DOWN":
        return False, "⚠️", "Сайт недоступен (DOWN)", f"Сервер не отвечает{details}"
    if verdict in ["OK", "PASS", "WORKING", "SUCCESS"]:
        return False, "✅", "Доступен", "Ошибок не обнаружено."
    if verdict == "TLS_BLOCK" or (tcp_ok and not tls_ok):
        return True, "⚠️", "Режут TLS (DPI)", f"TCP проходит, TLS обрывается{details}"
    elif "DNS" in verdict:
        return True, "🚫", "Подмена DNS", f"Неверный IP{details}"
    elif verdict == "TCP_RESET" or (not tcp_ok and not tls_ok):
        return True, "⛔", "Забанен полностью", f"Нет соединения{details}"
    elif verdict == "HTTP_STUB":
        return True, "📄", "Заглушка провайдера", f"Страница-заглушка{details}"
    else:
        return True, "❓", f"Блокировка: {verdict}", f"Аномалия{details}"


# --- ФУНКЦИЯ ПРОВЕРКИ ---
async def check_site_via_rkn_checker(site: str) -> dict | None:
    rkn_check_path = os.path.join(os.path.dirname(sys.executable), "rkn-check")
    cmd = [rkn_check_path, "--json", "--no-self-info", "--url", site]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            return None
        if process.returncode != 0:
            return None
        data = json.loads(stdout.decode())
        target_result = None
        for list_name in ["ad_hoc", "whitelist", "blacklist", "results", "checks"]:
            for item in data.get(list_name, []):
                possible_urls = [item.get("url"), item.get("name"), item.get("target"), item.get("host")]
                clean_site = site.lower().replace("http://", "").replace("https://", "").rstrip("/")
                match_found = False
                for u in possible_urls:
                    if u:
                        clean_u = u.lower().replace("http://", "").replace("https://", "").rstrip("/")
                        if clean_u == clean_site:
                            match_found = True
                            break
                if match_found:
                    target_result = item
                    break
            if target_result:
                break
        return target_result
    except Exception as e:
        logging.error(f"[{site}] Ошибка: {e}")
        return None


# --- ПЕРИОДИЧЕСКАЯ ЗАДАЧА (С ПОДДЕРЖКОЙ РУЧНОГО ЗАПУСКА) ---
async def periodic_check(manual_request: bool = False, message: types.Message = None):
    logging.info(f"--- Запуск проверки ({'Ручная' if manual_request else 'Автоматическая'}) ---")
    sites = get_tracked_sites()
    states = load_state()
    
    if not sites:
        if manual_request and message:
            await message.answer("Список отслеживаемых сайтов пуст.")
        return

    for site in sites:
        try:
            raw_result = await check_site_via_rkn_checker(site)
            is_blocked, emoji, status_text, details = interpret_verdict(raw_result)
            current_status = "blocked" if is_blocked else "ok"
            previous_status = states.get(site, "unknown")

            meta = NODE_METADATA.get(site, {})
            node_str = f"🖥 **Нода:** {meta.get('node')}\n" if meta.get('node') else ""
            ip_str = f"🌐 **Адрес:** `{meta.get('ip')}`\n" if meta.get('ip') else ""
            provider_str = f"☁️ **Провайдер:** {meta.get('provider')}\n" if meta.get('provider') else ""

            if not manual_request:
                alert_message = None
                if current_status == "blocked" and previous_status != "blocked":
                    alert_message = (
                        f"{emoji} **Внимание! Сайт заблокирован!**\n\n"
                        f"🔗 **Сайт:** `{site}`\n"
                        f"📌 **Статус:** {status_text}\n"
                        f"📝 **Детали:** {details}\n"
                        f"{node_str}{ip_str}{provider_str}"
                        f"🕒 **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    logging.info(f"Сайт {site} заблокирован ({status_text}).")
                elif current_status == "ok" and previous_status == "blocked":
                    alert_message = (
                        f"✅ **Отличные новости! Сайт снова доступен!**\n\n"
                        f"🔗 **Сайт:** `{site}`\n"
                        f"{node_str}{ip_str}{provider_str}"
                        f"🕒 **Время:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                    logging.info(f"Сайт {site} восстановлен.")
                else:
                    logging.info(f"Сайт {site}: {status_text} (без изменений).")

                if alert_message:
                    for admin_id in ADMIN_IDS:
                        try:
                            thread_id = TARGET_THREAD_ID if admin_id < 0 and TARGET_THREAD_ID else None
                            await bot.send_message(admin_id, alert_message, parse_mode="Markdown", message_thread_id=thread_id)
                        except Exception as e:
                            logging.error(f"Ошибка отправки в {admin_id}: {e}")
            
            states[site] = current_status
        except Exception as e:
            logging.error(f"Ошибка проверки {site}: {e}")

        await asyncio.sleep(1.5)
    
    save_state(states)
    
    if manual_request and message:
        report_text = format_sites_report(sites, states)
        await message.answer(f"📋 **Результаты внеплановой проверки:**\n\n{report_text}", parse_mode="Markdown")
        
    logging.info("--- Проверка завершена ---")


# --- КЛАВИАТУРА ---
def get_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(text="📋 Список сайтов", callback_data="list")],
        [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="check")],
        [InlineKeyboardButton(text="🔄 Синхронизировать с Remnawave", callback_data="sync")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# --- КОМАНДЫ БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот для мониторинга блокировок.\n"
        "Используйте кнопки ниже или команды в меню.",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer(
        "Выберите действие:",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    sites = get_tracked_sites()
    if not sites:
        await message.answer("Список отслеживаемых сайтов пуст.")
        return
    states = load_state()
    report_text = format_sites_report(sites, states)
    await message.answer(f"**Отслеживаемые сайты:**\n\n{report_text}", parse_mode="Markdown")

@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    await message.answer("🔄 Запускаю проверку всех сайтов. Пожалуйста, подождите (это займет несколько секунд)...")
    await periodic_check(manual_request=True, message=message)

@dp.message(Command("sync"))
async def cmd_sync(message: types.Message):
    await message.answer("🔄 Синхронизирую с Remnawave...")
    await sync_with_remnawave()
    await message.answer("✅ Синхронизация завершена. Проверьте /list.")

# Обработчик inline-кнопок
@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    if callback.data == "list":
        await cmd_list(callback.message)
    elif callback.data == "check":
        await cmd_check(callback.message)
    elif callback.data == "sync":
        await cmd_sync(callback.message)
    await callback.answer()

# --- УСТАНОВКА КОМАНД В МЕНЮ TELEGRAM ---
async def set_bot_commands():
    try:
        commands = [
            BotCommand(command="menu", description="Показать кнопки управления"),
            BotCommand(command="list", description="Список сайтов и их статусы"),
            BotCommand(command="check", description="Проверить все сайты сейчас"),
            BotCommand(command="sync", description="Синхронизировать с Remnawave"),
        ]
        await bot.set_my_commands(commands)
        logging.info("Меню команд успешно установлено.")
    except Exception as e:
        logging.error(f"Не удалось установить меню команд: {e}. Бот продолжит работу без него.")

# --- ЗАПУСК ---
async def main():
    if not os.path.exists(SITES_FILE):
        with open(SITES_FILE, "w", encoding="utf-8") as f:
            f.write("# Список сайтов\n")

    # Подключаем Middleware для проверки прав пользователей
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # Устанавливаем меню команд в Telegram
    await set_bot_commands()
    
    await sync_with_remnawave()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(periodic_check, "interval", minutes=CHECK_INTERVAL_MINUTES)
    scheduler.add_job(sync_with_remnawave, "interval", minutes=SYNC_INTERVAL_MINUTES)
    scheduler.start()
    
    logging.info(f"Проверка: каждые {CHECK_INTERVAL_MINUTES} мин.")
    logging.info(f"Синхронизация: каждые {SYNC_INTERVAL_MINUTES} мин.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())