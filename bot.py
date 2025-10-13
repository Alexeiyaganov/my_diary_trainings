#!/usr/bin/env python3
import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Импорты
try:
    from aiogram import Bot, Dispatcher, types, F
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web
    import asyncpg
    from sqlalchemy import create_engine, Column, Integer, String, BigInteger, Boolean, Text, TIMESTAMP, Float
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.sql import func
except ImportError as e:
    logger.error(f"Missing dependencies: {e}")
    logger.info("Install with: pip install aiogram sqlalchemy asyncpg aiohttp")
    raise


# Конфигурация
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgresql://", "postgresql+asyncpg://")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
    PORT = int(os.getenv("PORT", "8000"))

    @classmethod
    def validate(cls):
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")
        if not cls.DATABASE_URL:
            raise ValueError("DATABASE_URL environment variable is required")


# База данных
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(100))
    full_name = Column(String(200))
    role = Column(String(20), nullable=False, default='athlete')
    created_at = Column(TIMESTAMP, server_default=func.now())


class TrainingSession(Base):
    __tablename__ = "training_sessions"

    id = Column(Integer, primary_key=True)
    athlete_id = Column(BigInteger, nullable=False)
    start_time = Column(TIMESTAMP, nullable=False)
    duration_seconds = Column(Integer)
    sport_type = Column(String(100))
    distance_meters = Column(Float)
    avg_heart_rate = Column(Integer)
    calories = Column(Integer)
    notes = Column(Text)
    source = Column(String(20), nullable=False, default='manual')
    created_at = Column(TIMESTAMP, server_default=func.now())


class Database:
    def __init__(self):
        self.engine = None
        self.SessionLocal = None

    async def init(self):
        """Инициализация базы данных"""
        try:
            # Создаем синхронный engine для создания таблиц
            sync_db_url = Config.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
            self.engine = create_engine(sync_db_url)

            # Создаем таблицы
            Base.metadata.create_all(bind=self.engine)
            logger.info("Database tables created successfully")

            # Создаем sessionmaker для синхронных операций
            self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    def get_session(self):
        """Получить сессию базы данных"""
        return self.SessionLocal()


# Состояния бота
class AddTraining(StatesGroup):
    waiting_for_sport = State()
    waiting_for_duration = State()
    waiting_for_distance = State()


class TrainingBot:
    def __init__(self):
        self.bot = None
        self.dp = None
        self.db = Database()

    async def init(self):
        """Инициализация бота"""
        Config.validate()

        # Инициализация базы данных
        await self.db.init()

        # Инициализация бота
        self.bot = Bot(token=Config.BOT_TOKEN)
        self.dp = Dispatcher(storage=MemoryStorage())

        # Регистрация обработчиков
        self.register_handlers()

        logger.info("Bot initialized successfully")

    def register_handlers(self):
        """Регистрация всех обработчиков команд"""

        # Команда /start
        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            session = self.db.get_session()
            try:
                # Проверяем существующего пользователя
                user = session.query(User).filter(User.telegram_id == message.from_user.id).first()

                if not user:
                    user = User(
                        telegram_id=message.from_user.id,
                        username=message.from_user.username,
                        full_name=message.from_user.full_name,
                        role="athlete"
                    )
                    session.add(user)
                    session.commit()

                    await message.answer(
                        "🏃‍♂️ <b>Добро пожаловать в ваш тренировочный дневник!</b>\n\n"
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
            except Exception as e:
                logger.error(f"Error in /start: {e}")
                await message.answer("❌ Произошла ошибка. Попробуйте позже.")
            finally:
                session.close()

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
            session = self.db.get_session()
            try:
                distance_km = float(message.text)
                data = await state.get_data()

                # Сохраняем тренировку
                training = TrainingSession(
                    athlete_id=message.from_user.id,
                    start_time=datetime.now(),
                    duration_seconds=data['duration_minutes'] * 60,
                    sport_type=data['sport_type'],
                    distance_meters=distance_km * 1000 if distance_km > 0 else 0,
                    source='manual'
                )

                session.add(training)
                session.commit()

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
            except Exception as e:
                logger.error(f"Error saving training: {e}")
                await message.answer("❌ Ошибка при сохранении тренировки. Попробуйте позже.")
            finally:
                session.close()

        # Команда /my_stats
        @self.dp.message(Command("my_stats"))
        async def cmd_my_stats(message: types.Message):
            session = self.db.get_session()
            try:
                # Общая статистика
                from sqlalchemy import func

                total_stats = session.query(
                    func.count(TrainingSession.id),
                    func.sum(TrainingSession.duration_seconds),
                    func.sum(TrainingSession.distance_meters)
                ).filter(TrainingSession.athlete_id == message.from_user.id).first()

                # Статистика за неделю
                week_ago = datetime.now() - timedelta(days=7)
                week_stats = session.query(
                    func.count(TrainingSession.id),
                    func.sum(TrainingSession.duration_seconds)
                ).filter(
                    TrainingSession.athlete_id == message.from_user.id,
                    TrainingSession.start_time >= week_ago
                ).first()

                total_trainings, total_seconds, total_meters = total_stats
                week_trainings, week_seconds = week_stats

                # Форматируем данные
                total_hours = total_seconds // 3600 if total_seconds else 0
                total_minutes = (total_seconds % 3600) // 60 if total_seconds else 0
                total_km = total_meters / 1000 if total_meters else 0

                week_hours = week_seconds // 3600 if week_seconds else 0
                week_minutes = (week_seconds % 3600) // 60 if week_seconds else 0

                await message.answer(
                    f"📊 <b>Ваша статистика</b>\n\n"
                    f"<b>Всего:</b>\n"
                    f"🏃 Тренировок: {total_trainings or 0}\n"
                    f"⏱️ Время: {total_hours}ч {total_minutes}м\n"
                    f"📏 Дистанция: {total_km:.1f} км\n\n"
                    f"<b>За последнюю неделю:</b>\n"
                    f"🏃 Тренировок: {week_trainings or 0}\n"
                    f"⏱️ Время: {week_hours}ч {week_minutes}м",
                    parse_mode="HTML"
                )

            except Exception as e:
                logger.error(f"Error getting stats: {e}")
                await message.answer("❌ Ошибка при получении статистики. Попробуйте позже.")
            finally:
                session.close()

        # Команда /report_week
        @self.dp.message(Command("report_week"))
        async def cmd_report_week(message: types.Message):
            session = self.db.get_session()
            try:
                week_ago = datetime.now() - timedelta(days=7)

                stats = session.query(
                    func.count(TrainingSession.id),
                    func.sum(TrainingSession.duration_seconds),
                    func.sum(TrainingSession.distance_meters)
                ).filter(
                    TrainingSession.athlete_id == message.from_user.id,
                    TrainingSession.start_time >= week_ago
                ).first()

                trainings_count, total_seconds, total_meters = stats

                hours = total_seconds // 3600 if total_seconds else 0
                minutes = (total_seconds % 3600) // 60 if total_seconds else 0
                km = total_meters / 1000 if total_meters else 0

                await message.answer(
                    f"📈 <b>Отчет за неделю</b>\n\n"
                    f"🏃 Тренировок: {trainings_count or 0}\n"
                    f"⏱️ Общее время: {hours}ч {minutes}м\n"
                    f"📏 Общая дистанция: {km:.1f} км\n\n"
                    f"📅 Период: {week_ago.strftime('%d.%m')} - {datetime.now().strftime('%d.%m')}",
                    parse_mode="HTML"
                )

            except Exception as e:
                logger.error(f"Error in report_week: {e}")
                await message.answer("❌ Ошибка при формировании отчета.")
            finally:
                session.close()

        # Команда /report_month
        @self.dp.message(Command("report_month"))
        async def cmd_report_month(message: types.Message):
            session = self.db.get_session()
            try:
                month_ago = datetime.now() - timedelta(days=30)

                stats = session.query(
                    func.count(TrainingSession.id),
                    func.sum(TrainingSession.duration_seconds),
                    func.sum(TrainingSession.distance_meters)
                ).filter(
                    TrainingSession.athlete_id == message.from_user.id,
                    TrainingSession.start_time >= month_ago
                ).first()

                trainings_count, total_seconds, total_meters = stats

                hours = total_seconds // 3600 if total_seconds else 0
                minutes = (total_seconds % 3600) // 60 if total_seconds else 0
                km = total_meters / 1000 if total_meters else 0

                await message.answer(
                    f"📈 <b>Отчет за месяц</b>\n\n"
                    f"🏃 Тренировок: {trainings_count or 0}\n"
                    f"⏱️ Общее время: {hours}ч {minutes}м\n"
                    f"📏 Общая дистанция: {km:.1f} км\n\n"
                    f"📅 Период: {month_ago.strftime('%d.%m')} - {datetime.now().strftime('%d.%m')}",
                    parse_mode="HTML"
                )

            except Exception as e:
                logger.error(f"Error in report_month: {e}")
                await message.answer("❌ Ошибка при формировании отчета.")
            finally:
                session.close()

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

    async def run_webhook(self):
        """Запуск бота в режиме webhook (для Render)"""
        app = web.Application()
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=self.dp,
            bot=self.bot,
        )

        # Регистрируем webhook
        webhook_requests_handler.register(app, path="/webhook")
        setup_application(app, self.dp, bot=self.bot)

        # Запускаем сервер
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', Config.PORT)
        await site.start()

        logger.info(f"Bot started with webhook on port {Config.PORT}")

        # Устанавливаем webhook
        await self.bot.set_webhook(f"{Config.WEBHOOK_URL}/webhook")
        logger.info(f"Webhook set to: {Config.WEBHOOK_URL}/webhook")

        # Бесконечное ожидание
        await asyncio.Future()

    async def run_polling(self):
        """Запуск бота в режиме polling (для разработки)"""
        logger.info("Starting bot in polling mode...")
        await self.dp.start_polling(self.bot)


async def main():
    """Основная функция запуска"""
    bot = TrainingBot()
    await bot.init()

    # Выбираем режим запуска
    if Config.WEBHOOK_URL:
        await bot.run_webhook()
    else:
        await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())