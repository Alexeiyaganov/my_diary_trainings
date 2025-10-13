#!/usr/bin/env python3
import os
import asyncio
import logging
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEB_APP_URL = os.getenv("WEB_APP_URL", "")  # URL веб-приложения
POLAR_CLIENT_ID = os.getenv("POLAR_CLIENT_ID", "")
POLAR_CLIENT_SECRET = os.getenv("POLAR_CLIENT_SECRET", "")
POLAR_REDIRECT_URI = os.getenv("POLAR_REDIRECT_URI", "")

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
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    from aiohttp import web
    import asyncpg
    import requests
    from aiohttp_jinja2 import setup as setup_jinja2
    import jinja2
except ImportError as e:
    logger.error(f"Missing dependencies: {e}")
    raise


# Состояния для FSM
class AddTraining(StatesGroup):
    waiting_for_sport = State()
    waiting_for_duration = State()
    waiting_for_distance = State()


class CoachAssignment(StatesGroup):
    waiting_for_athlete = State()
    waiting_for_description = State()
    waiting_for_date = State()
    waiting_for_targets = State()


class TrainingBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())
        self.db_pool = None
        self.web_app = None

    async def init_db(self):
        """Инициализация базы данных"""
        self.db_pool = await asyncpg.create_pool(DATABASE_URL)

        # Создаем таблицы
        async with self.db_pool.acquire() as conn:
            # Основные таблицы
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
                    avg_heart_rate INTEGER,
                    calories INTEGER,
                    notes TEXT,
                    source VARCHAR(20) DEFAULT 'manual',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Новые таблицы для расширенного функционала
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS training_assignments (
                    id SERIAL PRIMARY KEY,
                    coach_id BIGINT NOT NULL,
                    athlete_id BIGINT NOT NULL,
                    description TEXT NOT NULL,
                    date_assigned DATE NOT NULL,
                    target_duration INTEGER,
                    target_distance FLOAT,
                    is_completed BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS polar_connections (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT UNIQUE NOT NULL,
                    access_token TEXT NOT NULL,
                    polar_user_id TEXT,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            await conn.execute('''
                CREATE TABLE IF NOT EXISTS coach_athlete_relations (
                    id SERIAL PRIMARY KEY,
                    coach_id BIGINT NOT NULL,
                    athlete_id BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(coach_id, athlete_id)
                )
            ''')

            # Индексы
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_training_athlete_id ON training_sessions(athlete_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_training_start_time ON training_sessions(start_time)')
            await conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_assignments_athlete_date ON training_assignments(athlete_id, date_assigned)')

        logger.info("Database initialized successfully")

    def register_handlers(self):
        """Регистрация всех обработчиков команд"""

        # Команда /start с Web App кнопкой
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

                # Создаем клавиатуру с Web App кнопкой
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="📊 Открыть веб-панель",
                        web_app=WebAppInfo(url=f"{WEB_APP_URL}/dashboard/{message.from_user.id}")
                    )],
                    [InlineKeyboardButton(text="➕ Добавить тренировку", callback_data="add_training"),
                     InlineKeyboardButton(text="📈 Статистика", callback_data="stats")]
                ])

                await message.answer(
                    "🏃‍♂️ <b>Добро пожаловать в тренировочный дневник!</b>\n\n"
                    "📋 <b>Доступные команды:</b>\n"
                    "/add_training - Добавить тренировку\n"
                    "/my_stats - Моя статистика\n"
                    "/report_week - Отчет за неделю\n"
                    "/web - Открыть веб-панель\n"
                    "/coach - Режим тренера\n"
                    "/connect_polar - Подключить Polar Flow",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        # Команда для открытия веб-панели
        @self.dp.message(Command("web"))
        async def cmd_web(message: types.Message):
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="📊 Открыть веб-панель",
                    web_app=WebAppInfo(url=f"{WEB_APP_URL}/dashboard/{message.from_user.id}")
                )]
            ])
            await message.answer("Нажмите кнопку чтобы открыть веб-панель:", reply_markup=keyboard)

        # Режим тренера
        @self.dp.message(Command("coach"))
        async def cmd_coach(message: types.Message):
            async with self.db_pool.acquire() as conn:
                # Устанавливаем роль тренера
                await conn.execute(
                    'UPDATE users SET role = $1 WHERE telegram_id = $2',
                    'coach', message.from_user.id
                )

                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👥 Мои спортсмены", callback_data="my_athletes")],
                    [InlineKeyboardButton(text="📋 Создать задание", callback_data="create_assignment")],
                    [InlineKeyboardButton(text="📊 Панель тренера",
                                          web_app=WebAppInfo(url=f"{WEB_APP_URL}/coach/{message.from_user.id}"))]
                ])

                await message.answer(
                    "👨‍🏫 <b>Режим тренера активирован!</b>\n\n"
                    "Теперь вы можете:\n"
                    "• Добавлять спортсменов\n"
                    "• Создавать тренировочные планы\n"
                    "• Отслеживать прогресс\n"
                    "• Анализировать статистику",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        # Подключение Polar Flow
        @self.dp.message(Command("connect_polar"))
        async def cmd_connect_polar(message: types.Message):
            if not POLAR_CLIENT_ID:
                await message.answer("❌ Интеграция с Polar Flow не настроена")
                return

            # Создаем URL для авторизации Polar
            auth_params = {
                'response_type': 'code',
                'client_id': POLAR_CLIENT_ID,
                'redirect_uri': POLAR_REDIRECT_URI,
                'scope': 'trainingdata',
                'state': str(message.from_user.id)
            }

            auth_url = f"https://flow.polar.com/oauth2/authorization?{urlencode(auth_params)}"

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Подключить Polar Flow", url=auth_url)]
            ])

            await message.answer(
                "🔗 <b>Подключение Polar Flow</b>\n\n"
                "Нажмите кнопку ниже чтобы авторизоваться в Polar Flow и автоматически "
                "импортировать ваши тренировки:",
                parse_mode="HTML",
                reply_markup=keyboard
            )

        # Обработчики callback запросов
        @self.dp.callback_query(F.data == "my_athletes")
        async def show_athletes(callback: types.CallbackQuery):
            async with self.db_pool.acquire() as conn:
                athletes = await conn.fetch(
                    '''SELECT u.telegram_id, u.username, u.full_name,
                       COUNT(ts.id) as training_count
                       FROM coach_athlete_relations car
                       JOIN users u ON u.telegram_id = car.athlete_id
                       LEFT JOIN training_sessions ts ON ts.athlete_id = u.telegram_id
                       WHERE car.coach_id = $1
                       GROUP BY u.telegram_id, u.username, u.full_name''',
                    callback.from_user.id
                )

                if not athletes:
                    await callback.message.answer("❌ У вас пока нет спортсменов")
                    return

                response = "👥 <b>Ваши спортсмены:</b>\n\n"
                for athlete in athletes:
                    response += f"• {athlete['full_name']} (@{athlete['username']})\n"
                    response += f"  Тренировок: {athlete['training_count']}\n\n"

                await callback.message.answer(response, parse_mode="HTML")

        # Добавьте остальные обработчики из предыдущей версии...
        # (команды add_training, my_stats, report_week, report_month и т.д.)

    async def run_polling(self):
        """Запуск бота в режиме polling"""
        logger.info("Starting Telegram bot in polling mode...")
        await self.dp.start_polling(self.bot)


# Веб-приложение
async def create_web_app():
    """Создание веб-приложения"""
    app = web.Application()

    # Настройка Jinja2
    setup_jinja2(app, loader=jinja2.FileSystemLoader('templates'))

    # Статические файлы
    app.router.add_static('/static', 'static')

    # Маршруты
    app.router.add_get('/', handle_dashboard)
    app.router.add_get('/dashboard/{user_id}', handle_dashboard)
    app.router.add_get('/coach/{coach_id}', handle_coach_dashboard)
    app.router.add_get('/training/{user_id}', handle_training_list)
    app.router.add_get('/training/{user_id}/edit/{training_id}', handle_edit_training)
    app.router.add_post('/training/{user_id}/update/{training_id}', handle_update_training)
    app.router.add_post('/training/{user_id}/delete/{training_id}', handle_delete_training)
    app.router.add_get('/assignments/{coach_id}', handle_assignments)
    app.router.add_post('/assignments/{coach_id}/create', handle_create_assignment)
    app.router.add_get('/polar/callback', handle_polar_callback)

    return app


# Обработчики веб-маршрутов
@aiohttp_jinja2.template('dashboard.html')
async def handle_dashboard(request):
    user_id = int(request.match_info['user_id'])

    async with request.app['db_pool'].acquire() as conn:
        # Получаем статистику пользователя
        stats = await conn.fetchrow('''
            SELECT 
                COUNT(*) as total_trainings,
                COALESCE(SUM(duration_seconds), 0) as total_seconds,
                COALESCE(SUM(distance_meters), 0) as total_meters,
                COALESCE(AVG(avg_heart_rate), 0) as avg_hr
            FROM training_sessions 
            WHERE athlete_id = $1
        ''', user_id)

        # Последние тренировки
        recent_trainings = await conn.fetch('''
            SELECT * FROM training_sessions 
            WHERE athlete_id = $1 
            ORDER BY start_time DESC 
            LIMIT 10
        ''', user_id)

        # Активные задания
        active_assignments = await conn.fetch('''
            SELECT ta.*, u.full_name as coach_name
            FROM training_assignments ta
            JOIN users u ON u.telegram_id = ta.coach_id
            WHERE ta.athlete_id = $1 AND ta.date_assigned >= CURRENT_DATE
            ORDER BY ta.date_assigned
        ''', user_id)

    return {
        'user_id': user_id,
        'stats': stats,
        'recent_trainings': recent_trainings,
        'assignments': active_assignments
    }


@aiohttp_jinja2.template('coach_dashboard.html')
async def handle_coach_dashboard(request):
    coach_id = int(request.match_info['coach_id'])

    async with request.app['db_pool'].acquire() as conn:
        # Статистика по спортсменам
        athletes_stats = await conn.fetch('''
            SELECT u.telegram_id, u.full_name, u.username,
                   COUNT(ts.id) as training_count,
                   COALESCE(SUM(ts.duration_seconds), 0) as total_seconds,
                   COUNT(ta.id) as assignment_count,
                   COUNT(CASE WHEN ta.is_completed THEN 1 END) as completed_assignments
            FROM coach_athlete_relations car
            JOIN users u ON u.telegram_id = car.athlete_id
            LEFT JOIN training_sessions ts ON ts.athlete_id = u.telegram_id
            LEFT JOIN training_assignments ta ON ta.athlete_id = u.telegram_id
            WHERE car.coach_id = $1
            GROUP BY u.telegram_id, u.full_name, u.username
        ''', coach_id)

        # Предстоящие задания
        upcoming_assignments = await conn.fetch('''
            SELECT ta.*, u.full_name as athlete_name
            FROM training_assignments ta
            JOIN users u ON u.telegram_id = ta.athlete_id
            WHERE ta.coach_id = $1 AND ta.date_assigned >= CURRENT_DATE
            ORDER BY ta.date_assigned
        ''', coach_id)

    return {
        'coach_id': coach_id,
        'athletes': athletes_stats,
        'upcoming_assignments': upcoming_assignments
    }


async def handle_polar_callback(request):
    """Обработчик callback от Polar OAuth"""
    code = request.query.get('code')
    state = request.query.get('state')  # user_id

    if not code or not state:
        return web.Response(text="Ошибка авторизации")

    try:
        # Получаем access token от Polar
        token_response = requests.post('https://polarremote.com/v2/oauth2/token', data={
            'grant_type': 'authorization_code',
            'code': code,
            'client_id': POLAR_CLIENT_ID,
            'client_secret': POLAR_CLIENT_SECRET
        })

        if token_response.status_code == 200:
            token_data = token_response.json()

            # Сохраняем токен в базу
            async with request.app['db_pool'].acquire() as conn:
                await conn.execute('''
                    INSERT INTO polar_connections (user_id, access_token, expires_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO UPDATE SET
                    access_token = $2, expires_at = $3
                ''', int(state), token_data['access_token'],
                                   datetime.now() + timedelta(seconds=token_data['expires_in']))

            # Импортируем тренировки
            await import_polar_trainings(int(state), token_data['access_token'])

            return web.Response(
                text="✅ Polar Flow успешно подключен! Тренировки импортированы."
            )

    except Exception as e:
        logger.error(f"Polar connection error: {e}")
        return web.Response(text="❌ Ошибка при подключении Polar Flow")


async def import_polar_trainings(user_id, access_token):
    """Импорт тренировок из Polar Flow"""
    try:
        headers = {'Authorization': f'Bearer {access_token}'}

        # Получаем список тренировок
        exercises_response = requests.get(
            'https://www.polaraccesslink.com/v3/users/exercises',
            headers=headers
        )

        if exercises_response.status_code == 200:
            exercises = exercises_response.json()

            async with app['db_pool'].acquire() as conn:
                for exercise in exercises:
                    # Проверяем, есть ли уже такая тренировка
                    existing = await conn.fetchrow(
                        'SELECT id FROM exercise_data WHERE polar_exercise_id = $1',
                        exercise['id']
                    )

                    if not existing:
                        # Сохраняем детали тренировки
                        await conn.execute('''
                            INSERT INTO exercise_data 
                            (user_id, polar_exercise_id, start_time, duration_seconds,
                             sport_type, distance_meters, avg_heart_rate, max_heart_rate,
                             calories, raw_data)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        ''', user_id, exercise['id'], exercise['start_time'],
                                           exercise['duration'], exercise['sport'],
                                           exercise.get('distance', 0), exercise.get('heart_rate', {}).get('average'),
                                           exercise.get('heart_rate', {}).get('maximum'), exercise.get('calories'),
                                           json.dumps(exercise))

                        # Также добавляем в основную таблицу тренировок
                        await conn.execute('''
                            INSERT INTO training_sessions 
                            (athlete_id, start_time, duration_seconds, sport_type,
                             distance_meters, avg_heart_rate, calories, source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, 'polar')
                        ''', user_id, exercise['start_time'], exercise['duration'],
                                           exercise['sport'], exercise.get('distance', 0),
                                           exercise.get('heart_rate', {}).get('average'),
                                           exercise.get('calories'))

        logger.info(f"Imported Polar trainings for user {user_id}")

    except Exception as e:
        logger.error(f"Polar import error: {e}")


async def main():
    """Основная функция запуска"""
    try:
        # Инициализация бота
        bot = TrainingBot()
        await bot.init_db()
        bot.register_handlers()

        # Создание веб-приложения
        web_app = await create_web_app()
        web_app['db_pool'] = bot.db_pool

        # Настройка runner для веб-приложения
        runner = web.AppRunner(web_app)
        await runner.setup()

        port = int(os.getenv("PORT", "8000"))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()

        logger.info(f"Web application started on port {port}")

        # Запускаем бота в фоне
        asyncio.create_task(bot.run_polling())
        logger.info("Telegram bot started in polling mode")
        logger.info("Application is ready and running!")

        # Держим приложение запущенным
        await asyncio.Future()

    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())