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

try:
    from aiogram import Bot, Dispatcher, types, F
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    import asyncpg
    import aiohttp
except ImportError as e:
    logger.error(f"Missing dependencies: {e}")
    raise

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "8000"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required")


# Упрощенная работа с базой данных
class Database:
    def __init__(self):
        self.pool = None

    async def init(self):
        """Инициализация connection pool"""
        self.pool = await asyncpg.create_pool(DATABASE_URL)
        await self.create_tables()
        logger.info("Database initialized")

    async def create_tables(self):
        """Создание таблиц"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей
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

            # Таблица тренировок
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS training_sessions (
                    id SERIAL PRIMARY KEY,
                    athlete_id BIGINT NOT NULL,
                    start_time TIMESTAMP NOT NULL,
                    duration_seconds INTEGER,
                    sport_type VARCHAR(100),
                    distance_meters FLOAT,
                    avg_heart_rate INTEGER,
                    calories INTEGER,
                    notes TEXT,
                    source VARCHAR(20) DEFAULT 'manual',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Индексы для производительности
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_athlete_id ON training_sessions(athlete_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_start_time ON training_sessions(start_time)')


# Состояния бота
class AddTraining(StatesGroup):
    waiting_for_sport = State()
    waiting_for_duration = State()
    waiting_for_distance = State()


class TrainingBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.db = Database()

    async def init(self):
        """Инициализация бота"""
        await self.db.init()
        self.register_handlers()
        logger.info("Bot initialized successfully")

    def register_handlers(self):
        """Регистрация обработчиков"""

        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            async with self.db.pool.acquire() as conn:
                user = await conn.fetchrow(
                    'SELECT * FROM users WHERE telegram_id = $1',
                    message.from_user.id
                )

                if not user:
                    await conn.execute(
                        'INSERT INTO users (telegram_id, username, full_name, role) VALUES ($1, $2, $3, $4)',
                        message.from_user.id, message.from_user.username,
                        message.from_user.full_name, 'athlete'
                    )

                    await message.answer(
                        "🏃‍♂️ <b>Добро пожаловать в тренировочный дневник!</b>\n\n"
                        "📋 <b>Команды:</b>\n"
                        "/add_training - Добавить тренировку\n"
                        "/my_stats - Моя статистика\n"
                        "/report_week - Отчет за неделю\n"
                        "/report_month - Отчет за месяц",
                        parse_mode="HTML"
                    )
                else:
                    await message.answer("С возвращением! Используйте /add_training для добавления тренировки.")

        @self.dp.message(Command("add_training"))
        async def cmd_add_training(message: types.Message, state: FSMContext):
            await message.answer("🏃 Какой вид спорта? (бег, велосипед, плавание, силовая)")
            await state.set_state(AddTraining.waiting_for_sport)

        @self.dp.message(AddTraining.waiting_for_sport, F.text)
        async def process_sport(message: types.Message, state: FSMContext):
            await state.update_data(sport_type=message.text)
            await message.answer("⏱️ Сколько минут длилась тренировка?")
            await state.set_state(AddTraining.waiting_for_duration)

        @self.dp.message(AddTraining.waiting_for_duration, F.text)
        async def process_duration(message: types.Message, state: FSMContext):
            try:
                duration_minutes = int(message.text)
                if duration_minutes > 0:
                    await state.update_data(duration_minutes=duration_minutes)
                    await message.answer("📏 Дистанция в км? (0 если не применимо)")
                    await state.set_state(AddTraining.waiting_for_distance)
                else:
                    await message.answer("❌ Введите положительное число:")
            except ValueError:
                await message.answer("❌ Введите число:")

        @self.dp.message(AddTraining.waiting_for_distance, F.text)
        async def process_distance(message: types.Message, state: FSMContext):
            try:
                distance_km = float(message.text)
                data = await state.get_data()

                async with self.db.pool.acquire() as conn:
                    await conn.execute(
                        '''INSERT INTO training_sessions 
                        (athlete_id, start_time, duration_seconds, sport_type, distance_meters, source) 
                        VALUES ($1, $2, $3, $4, $5, $6)''',
                        message.from_user.id, datetime.now(),
                        data['duration_minutes'] * 60, data['sport_type'],
                        distance_km * 1000, 'manual'
                    )

                await message.answer(
                    f"✅ <b>Тренировка добавлена!</b>\n"
                    f"🏃 {data['sport_type']}\n"
                    f"⏱️ {data['duration_minutes']} мин\n"
                    f"📏 {distance_km if distance_km > 0 else 'N/A'} км",
                    parse_mode="HTML"
                )
                await state.clear()

            except ValueError:
                await message.answer("❌ Введите число:")

        @self.dp.message(Command("my_stats"))
        async def cmd_my_stats(message: types.Message):
            async with self.db.pool.acquire() as conn:
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

        @self.dp.message(Command("report_week"))
        async def cmd_report_week(message: types.Message):
            week_ago = datetime.now() - timedelta(days=7)

            async with self.db.pool.acquire() as conn:
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

        @self.dp.message(Command("report_month"))
        async def cmd_report_month(message: types.Message):
            month_ago = datetime.now() - timedelta(days=30)

            async with self.db.pool.acquire() as conn:
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

        @self.dp.message()
        async def handle_other(message: types.Message):
            await message.answer(
                "🤖 Используйте команды:\n"
                "/start - Начать\n"
                "/add_training - Добавить тренировку\n"
                "/my_stats - Статистика\n"
                "/report_week - Отчет за неделю"
            )

    async def run_polling(self):
        """Запуск в режиме polling"""
        logger.info("Starting bot in polling mode...")
        await self.dp.start_polling(self.bot)


async def main():
    bot = TrainingBot()
    await bot.init()
    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())