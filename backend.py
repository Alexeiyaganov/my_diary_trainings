from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime, timedelta
import os
from pydantic import BaseModel
from typing import Optional, List
import json
import hashlib
import hmac
import time

# Конфигурация
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Модели БД
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True)
    username = Column(String(100))
    full_name = Column(String(200))
    role = Column(String(20), default='athlete')
    polar_access_token = Column(Text)
    polar_user_id = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)


class TrainingSession(Base):
    __tablename__ = "training_sessions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'))
    start_time = Column(DateTime, nullable=False)
    duration_seconds = Column(Integer)
    sport_type = Column(String(100))
    distance_meters = Column(Float)
    avg_heart_rate = Column(Integer)
    calories = Column(Integer)
    notes = Column(Text)
    source = Column(String(20), default='manual')
    polar_training_id = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)


class TrainingPlan(Base):
    __tablename__ = "training_plans"
    id = Column(Integer, primary_key=True)
    coach_id = Column(Integer, ForeignKey('users.id'))
    user_id = Column(Integer, ForeignKey('users.id'))
    title = Column(String(200))
    description = Column(Text)
    week_start = Column(DateTime)
    week_end = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)


class TrainingAssignment(Base):
    __tablename__ = "training_assignments"
    id = Column(Integer, primary_key=True)
    plan_id = Column(Integer, ForeignKey('training_plans.id'))
    day_of_week = Column(Integer)
    sport_type = Column(String(100))
    duration_minutes = Column(Integer)
    description = Column(Text)
    completed = Column(Boolean, default=False)


# Pydantic модели
class UserCreate(BaseModel):
    telegram_id: str
    username: Optional[str] = None
    full_name: str
    role: str = "athlete"


class TrainingSessionCreate(BaseModel):
    user_id: int
    start_time: datetime
    duration_seconds: int
    sport_type: str
    distance_meters: Optional[float] = None
    avg_heart_rate: Optional[int] = None
    notes: Optional[str] = None


class TrainingPlanCreate(BaseModel):
    coach_id: int
    user_id: int
    title: str
    description: Optional[str] = None
    week_start: datetime
    week_end: datetime
    assignments: List[dict]


class PolarAuthRequest(BaseModel):
    user_id: int
    access_token: str
    user_id_polar: str


# FastAPI приложение
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Статические файлы
app.mount("/static", StaticFiles(directory="."), name="static")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


# Валидация Telegram Web App данных
def validate_telegram_data(init_data: str) -> bool:
    if not TELEGRAM_BOT_TOKEN:
        return True  # В разработке пропускаем проверку

    try:
        parsed_data = {}
        for item in init_data.split('&'):
            key, value = item.split('=')
            parsed_data[key] = value

        hash_str = parsed_data.pop('hash')
        data_check_string = '\n'.join([f"{k}={v}" for k, v in sorted(parsed_data.items())])

        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        return calculated_hash == hash_str
    except:
        return False


# API endpoints
@app.get("/")
async def read_index():
    return FileResponse('index.html')


@app.post("/api/auth/telegram")
async def auth_telegram(request: Request, db: Session = Depends(get_db)):
    form_data = await request.form()
    init_data = form_data.get('initData')

    if not validate_telegram_data(init_data):
        raise HTTPException(status_code=401, detail="Invalid Telegram data")

    # Парсим данные пользователя
    user_data = {}
    for item in init_data.split('&'):
        if 'user=' in item:
            user_json = item.split('user=')[1]
            user_data = json.loads(user_json)
            break

    if not user_data:
        raise HTTPException(status_code=400, detail="User data not found")

    # Создаем/обновляем пользователя
    db_user = db.query(User).filter(User.telegram_id == str(user_data['id'])).first()
    if not db_user:
        db_user = User(
            telegram_id=str(user_data['id']),
            username=user_data.get('username'),
            full_name=f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip(),
            role='athlete'
        )
        db.add(db_user)
        db.commit()
        db.refresh(db_user)

    return db_user


@app.get("/api/users/telegram/{telegram_id}")
async def get_user_by_telegram(telegram_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@app.post("/api/training_sessions/")
async def create_training_session(session: TrainingSessionCreate, db: Session = Depends(get_db)):
    db_session = TrainingSession(**session.dict())
    db.add(db_session)
    db.commit()
    db.refresh(db_session)
    return db_session


@app.get("/api/training_sessions/user/{user_id}")
async def get_user_sessions(user_id: int, db: Session = Depends(get_db)):
    sessions = db.query(TrainingSession).filter(TrainingSession.user_id == user_id).order_by(
        TrainingSession.start_time.desc()).all()
    return sessions


@app.get("/api/dashboard/stats/{user_id}")
async def get_dashboard_stats(user_id: int, db: Session = Depends(get_db)):
    # Общая статистика
    total_stats = db.query(
        func.count(TrainingSession.id),
        func.coalesce(func.sum(TrainingSession.duration_seconds), 0),
        func.coalesce(func.sum(TrainingSession.distance_meters), 0)
    ).filter(TrainingSession.user_id == user_id).first()

    # Статистика по неделям
    weekly_stats = []
    for i in range(8):
        week_start = datetime.utcnow() - timedelta(weeks=i + 1)
        week_end = datetime.utcnow() - timedelta(weeks=i)

        week_data = db.query(
            func.count(TrainingSession.id),
            func.coalesce(func.sum(TrainingSession.duration_seconds), 0),
            func.coalesce(func.sum(TrainingSession.distance_meters), 0)
        ).filter(
            TrainingSession.user_id == user_id,
            TrainingSession.start_time >= week_start,
            TrainingSession.start_time < week_end
        ).first()

        weekly_stats.append({
            "week": week_start.strftime("%Y-%m-%d"),
            "trainings": week_data[0],
            "duration_hours": round(week_data[1] / 3600, 1),
            "distance_km": round(week_data[2] / 1000, 1) if week_data[2] else 0
        })

    # Распределение по видам спорта
    sport_distribution = db.query(
        TrainingSession.sport_type,
        func.count(TrainingSession.id)
    ).filter(TrainingSession.user_id == user_id).group_by(TrainingSession.sport_type).all()

    sport_data = [{"sport": sport, "count": count} for sport, count in sport_distribution]

    return {
        "total_trainings": total_stats[0],
        "total_duration_hours": round(total_stats[1] / 3600, 1),
        "total_distance_km": round(total_stats[2] / 1000, 1) if total_stats[2] else 0,
        "weekly_stats": weekly_stats,
        "sport_distribution": sport_data
    }


@app.post("/api/polar/auth")
async def polar_auth(auth: PolarAuthRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == auth.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.polar_access_token = auth.access_token
    user.polar_user_id = auth.user_id_polar
    db.commit()

    return {"status": "success", "message": "Polar account connected"}


@app.get("/api/polar/sync/{user_id}")
async def sync_polar_data(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.polar_access_token:
        raise HTTPException(status_code=400, detail="Polar not connected")

    # Здесь будет реальная синхронизация с Polar API
    # Пока возвращаем заглушку
    return {
        "status": "success",
        "message": "Polar sync completed",
        "synced_trainings": 0,
        "new_trainings": []
    }


@app.post("/api/training_plans/")
async def create_training_plan(plan: TrainingPlanCreate, db: Session = Depends(get_db)):
    db_plan = TrainingPlan(
        coach_id=plan.coach_id,
        user_id=plan.user_id,
        title=plan.title,
        description=plan.description,
        week_start=plan.week_start,
        week_end=plan.week_end
    )
    db.add(db_plan)
    db.commit()
    db.refresh(db_plan)

    for assignment in plan.assignments:
        db_assignment = TrainingAssignment(
            plan_id=db_plan.id,
            day_of_week=assignment['day_of_week'],
            sport_type=assignment['sport_type'],
            duration_minutes=assignment.get('duration_minutes'),
            description=assignment.get('description')
        )
        db.add(db_assignment)

    db.commit()
    return db_plan


@app.get("/api/training_plans/user/{user_id}")
async def get_user_plans(user_id: int, db: Session = Depends(get_db)):
    plans = db.query(TrainingPlan).filter(TrainingPlan.user_id == user_id).all()
    return plans


# Список доступных видов спорта
@app.get("/api/sports")
async def get_sports():
    sports = [
        {"id": "running", "name": "🏃 Бег", "default": True},
        {"id": "cycling", "name": "🚴 Велосипед", "default": False},
        {"id": "swimming", "name": "🏊 Плавание", "default": False},
        {"id": "strength", "name": "💪 Силовая", "default": False},
        {"id": "yoga", "name": "🧘 Йога", "default": False},
        {"id": "walking", "name": "🚶 Ходьба", "default": False},
        {"id": "skiing", "name": "⛷️ Лыжи", "default": False},
        {"id": "rowing", "name": "🚣 Гребля", "default": False}
    ]
    return sports


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)