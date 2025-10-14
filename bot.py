#!/usr/bin/env python3
import os
import asyncio
import logging
from datetime import datetime, timedelta

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")

# Импорты
try:
    from aiogram import Bot, Dispatcher, types, F
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiohttp import web
    import asyncpg
except ImportError as e:
    logger.error(f"Missing dependencies: {e}")
    raise


# Состояния для добавления тренировки
class AddTraining(StatesGroup):
    waiting_for_sport = State()
    waiting_for_duration = State()
    waiting_for_distance = State()


class TrainingBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.db_pool = None

    async def init_db(self):
        """Инициализация базы данных"""
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)

        # Создаем таблицы
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(100),
                    full_name VARCHAR(200),
                    role VARCHAR(20) DEFAULT 'athlete',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS training_sessions (
                    id SERIAL PRIMARY KEY,
                    athlete_id BIGINT NOT NULL,
                    start_time TIMESTAMP NOT NULL,
                    duration_seconds INTEGER,
                    sport_type VARCHAR(100),
                    distance_meters FLOAT,
                    source VARCHAR(20) DEFAULT 'manual',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

        logger.info("Database initialized")

    def register_handlers(self):
        """Регистрация обработчиков команд"""

        # Команда /start
        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            async with self.db_pool.acquire() as conn:
                user = await conn.fetchrow(
                    'SELECT * FROM users WHERE telegram_id = $1',
                    message.from_user.id
                )

                if not user:
                    await conn.execute(
                        'INSERT INTO users (telegram_id, username, full_name) VALUES ($1, $2, $3)',
                        message.from_user.id, message.from_user.username, message.from_user.full_name
                    )

                    await message.answer(
                        "🏃‍♂️ <b>Добро пожаловать в тренировочный дневник!</b>\n\n"
                        "📋 <b>Доступные команды:</b>\n"
                        "/add_training - Добавить тренировку\n"
                        "/my_stats - Моя статистика\n"
                        "/report_week - Отчет за неделю\n"
                        "/report_month - Отчет за месяц\n\n"
                        "💡 <b>Совет:</b> Начните с добавления первой тренировки!",
                        parse_mode="HTML"
                    )
                else:
                    await message.answer(
                        "🔄 <b>С возвращением!</b>\n\n"
                        "Чем могу помочь?\n\n"
                        "📋 Команды:\n"
                        "/add_training - Добавить тренировку\n"
                        "/my_stats - Моя статистика\n"
                        "/report_week - Отчет за неделю",
                        parse_mode="HTML"
                    )

        # Команда /add_training
        @self.dp.message(Command("add_training"))
        async def cmd_add_training(message: types.Message, state: FSMContext):
            await message.answer(
                "🏃 <b>Добавление тренировки</b>\n\n"
                "Какой вид спорта?\n"
                "• бег\n• велосипед\n• плавание\n• силовая\n• йога\n• другие",
                parse_mode="HTML"
            )
            await state.set_state(AddTraining.waiting_for_sport)

        # Обработчик вида спорта
        @self.dp.message(AddTraining.waiting_for_sport, F.text)
        async def process_sport(message: types.Message, state: FSMContext):
            await state.update_data(sport_type=message.text)
            await message.answer("⏱️ Сколько минут длилась тренировка?")
            await state.set_state(AddTraining.waiting_for_duration)

        # Обработчик длительности
        @self.dp.message(AddTraining.waiting_for_duration, F.text)
        async def process_duration(message: types.Message, state: FSMContext):
            try:
                duration_minutes = int(message.text)
                if duration_minutes <= 0:
                    await message.answer("❌ Длительность должна быть положительным числом. Попробуйте снова:")
                    return

                await state.update_data(duration_minutes=duration_minutes)
                await message.answer("📏 Какая дистанция в километрах? (0 - если не применимо)")
                await state.set_state(AddTraining.waiting_for_distance)

            except ValueError:
                await message.answer("❌ Пожалуйста, введите число (минуты):")

        # Обработчик дистанции и сохранение тренировки
        @self.dp.message(AddTraining.waiting_for_distance, F.text)
        async def process_distance(message: types.Message, state: FSMContext):
            try:
                distance_km = float(message.text)
                data = await state.get_data()

                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        '''INSERT INTO training_sessions 
                        (athlete_id, start_time, duration_seconds, sport_type, distance_meters, source) 
                        VALUES ($1, $2, $3, $4, $5, $6)''',
                        message.from_user.id, datetime.now(),
                        data['duration_minutes'] * 60, data['sport_type'],
                        distance_km * 1000, 'manual'
                    )

                await message.answer(
                    f"✅ <b>Тренировка добавлена!</b>\n\n"
                    f"🏃 Вид: {data['sport_type']}\n"
                    f"⏱️ Длительность: {data['duration_minutes']} мин\n"
                    f"📏 Дистанция: {distance_km if distance_km > 0 else 'N/A'} км",
                    parse_mode="HTML"
                )
                await state.clear()

            except ValueError:
                await message.answer("❌ Пожалуйста, введите число (километры):")

        # Команда /my_stats
        @self.dp.message(Command("my_stats"))
        async def cmd_my_stats(message: types.Message):
            async with self.db_pool.acquire() as conn:
                stats = await conn.fetchrow('''
                    SELECT 
                        COUNT(*) as total_trainings,
                        COALESCE(SUM(duration_seconds), 0) as total_seconds,
                        COALESCE(SUM(distance_meters), 0) as total_meters
                    FROM training_sessions 
                    WHERE athlete_id = $1
                ''', message.from_user.id)

                total_hours = stats['total_seconds'] // 3600
                total_minutes = (stats['total_seconds'] % 3600) // 60
                total_km = stats['total_meters'] / 1000

                await message.answer(
                    f"📊 <b>Ваша статистика</b>\n\n"
                    f"🏃 Тренировок: {stats['total_trainings']}\n"
                    f"⏱️ Время: {total_hours}ч {total_minutes}м\n"
                    f"📏 Дистанция: {total_km:.1f} км",
                    parse_mode="HTML"
                )

        # Команда /report_week
        @self.dp.message(Command("report_week"))
        async def cmd_report_week(message: types.Message):
            week_ago = datetime.now() - timedelta(days=7)

            async with self.db_pool.acquire() as conn:
                stats = await conn.fetchrow('''
                    SELECT 
                        COUNT(*) as trainings_count,
                        COALESCE(SUM(duration_seconds), 0) as total_seconds,
                        COALESCE(SUM(distance_meters), 0) as total_meters
                    FROM training_sessions 
                    WHERE athlete_id = $1 AND start_time >= $2
                ''', message.from_user.id, week_ago)

                hours = stats['total_seconds'] // 3600
                minutes = (stats['total_seconds'] % 3600) // 60
                km = stats['total_meters'] / 1000

                await message.answer(
                    f"📈 <b>Отчет за неделю</b>\n\n"
                    f"🏃 Тренировок: {stats['trainings_count']}\n"
                    f"⏱️ Время: {hours}ч {minutes}м\n"
                    f"📏 Дистанция: {km:.1f} км",
                    parse_mode="HTML"
                )

        # Команда /report_month
        @self.dp.message(Command("report_month"))
        async def cmd_report_month(message: types.Message):
            month_ago = datetime.now() - timedelta(days=30)

            async with self.db_pool.acquire() as conn:
                stats = await conn.fetchrow('''
                    SELECT 
                        COUNT(*) as trainings_count,
                        COALESCE(SUM(duration_seconds), 0) as total_seconds,
                        COALESCE(SUM(distance_meters), 0) as total_meters
                    FROM training_sessions 
                    WHERE athlete_id = $1 AND start_time >= $2
                ''', message.from_user.id, month_ago)

                hours = stats['total_seconds'] // 3600
                minutes = (stats['total_seconds'] % 3600) // 60
                km = stats['total_meters'] / 1000

                await message.answer(
                    f"📈 <b>Отчет за месяц</b>\n\n"
                    f"🏃 Тренировок: {stats['trainings_count']}\n"
                    f"⏱️ Время: {hours}ч {minutes}м\n"
                    f"📏 Дистанция: {km:.1f} км",
                    parse_mode="HTML"
                )

        # Обработчик любых сообщений
        @self.dp.message()
        async def handle_other_messages(message: types.Message):
            await message.answer(
                "🤖 <b>Тренировочный дневник</b>\n\n"
                "Используйте команды:\n"
                "/start - Начать работу\n"
                "/add_training - Добавить тренировку\n"
                "/my_stats - Моя статистика\n"
                "/report_week - Отчет за неделю\n"
                "/report_month - Отчет за месяц",
                parse_mode="HTML"
            )

    async def run_polling(self):
        """Запуск бота в режиме polling"""
        logger.info("Starting Telegram bot in polling mode...")
        await self.dp.start_polling(self.bot)


async def handle_http_request(request):
    """Обработчик HTTP запросов для Render"""
    return web.Response(text="🚀 Training Diary Bot is running!\n\nVisit your bot in Telegram to use it.")


async def main():
    """Основная функция запуска"""
    # Инициализация бота
    bot = TrainingBot()

    # Инициализация базы данных
    await bot.init_db()

    # Регистрация обработчиков
    bot.register_handlers()

    logger.info("Bot initialized successfully")

    # Создаем HTTP сервер для Render
    app = web.Application()
    app.router.add_get('/', handle_http_request)
    app.router.add_get('/health', handle_http_request)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    logger.info(f"HTTP server started on port {port}")

    # Запускаем бота в фоне
    bot_task = asyncio.create_task(bot.run_polling())

    logger.info("🚀 Application started successfully!")
    logger.info("📱 Telegram bot is running in polling mode")
    logger.info("🌐 HTTP server is ready for health checks")

    try:
        # Держим приложение запущенным
        await asyncio.Future()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        bot_task.cancel()
        await bot.db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())