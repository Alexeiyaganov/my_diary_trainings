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

# Простая реализация без сложных зависимостей
try:
    import asyncpg
except ImportError:
    logger.error("asyncpg not installed. Run: pip install asyncpg")
    raise


class TrainingBot:
    def __init__(self):
        self.pool = None

    async def init_db(self):
        """Инициализация базы данных"""
        self.pool = await asyncpg.create_pool(DATABASE_URL)

        # Создаем таблицы
        async with self.pool.acquire() as conn:
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

    async def handle_start(self, user_id, username, full_name):
        """Обработка команды start"""
        async with self.pool.acquire() as conn:
            user = await conn.fetchrow(
                'SELECT * FROM users WHERE telegram_id = $1',
                user_id
            )

            if not user:
                await conn.execute(
                    'INSERT INTO users (telegram_id, username, full_name) VALUES ($1, $2, $3)',
                    user_id, username, full_name
                )
                return "🏃‍♂️ Добро пожаловать в тренировочный дневник!\n\nКоманды:\n/add_training - Добавить тренировку\n/my_stats - Статистика\n/report_week - Отчет за неделю"
            else:
                return "С возвращением! Используйте /add_training для добавления тренировки."

    async def add_training(self, user_id, sport_type, duration_minutes, distance_km):
        """Добавление тренировки"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                '''INSERT INTO training_sessions 
                (athlete_id, start_time, duration_seconds, sport_type, distance_meters) 
                VALUES ($1, $2, $3, $4, $5)''',
                user_id, datetime.now(), duration_minutes * 60,
                sport_type, distance_km * 1000
            )

        return f"✅ Тренировка добавлена!\n🏃 {sport_type}\n⏱️ {duration_minutes} мин\n📏 {distance_km} км"

    async def get_stats(self, user_id):
        """Получение статистики"""
        async with self.pool.acquire() as conn:
            stats = await conn.fetchrow('''
                SELECT 
                    COUNT(*) as total_trainings,
                    COALESCE(SUM(duration_seconds), 0) as total_seconds,
                    COALESCE(SUM(distance_meters), 0) as total_meters
                FROM training_sessions 
                WHERE athlete_id = $1
            ''', user_id)

            total_hours = stats['total_seconds'] // 3600
            total_minutes = (stats['total_seconds'] % 3600) // 60
            total_km = stats['total_meters'] / 1000

            return (
                f"📊 Статистика:\n"
                f"🏃 Тренировок: {stats['total_trainings']}\n"
                f"⏱️ Время: {total_hours}ч {total_minutes}м\n"
                f"📏 Дистанция: {total_km:.1f} км"
            )

    async def get_week_report(self, user_id):
        """Отчет за неделю"""
        week_ago = datetime.now() - timedelta(days=7)

        async with self.pool.acquire() as conn:
            stats = await conn.fetchrow('''
                SELECT 
                    COUNT(*) as trainings_count,
                    COALESCE(SUM(duration_seconds), 0) as total_seconds,
                    COALESCE(SUM(distance_meters), 0) as total_meters
                FROM training_sessions 
                WHERE athlete_id = $1 AND start_time >= $2
            ''', user_id, week_ago)

            hours = stats['total_seconds'] // 3600
            minutes = (stats['total_seconds'] % 3600) // 60
            km = stats['total_meters'] / 1000

            return (
                f"📈 Отчет за неделю:\n"
                f"🏃 Тренировок: {stats['trainings_count']}\n"
                f"⏱️ Время: {hours}ч {minutes}м\n"
                f"📏 Дистанция: {km:.1f} км"
            )


# Простой HTTP сервер для обработки webhook
async def handle_http_request(request):
    """Обработчик HTTP запросов"""
    return web.Response(text="Bot is running!")


async def main():
    """Основная функция"""
    bot = TrainingBot()
    await bot.init_db()
    logger.info("Bot started successfully")

    # Простой HTTP сервер для Render
    from aiohttp import web

    app = web.Application()
    app.router.add_get('/', handle_http_request)
    app.router.add_post('/webhook', handle_http_request)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "8000"))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

    logger.info(f"Server started on port {port}")

    # Держим приложение запущенным
    await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())