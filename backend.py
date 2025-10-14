from fastapi import FastAPI, HTTPException, Depends
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

# Конфигурация
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./test.db")
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


# API endpoints
@app.get("/")
async def read_index():
    return FileResponse('index.html')


@app.post("/api/users/")
async def create_user(user: UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.telegram_id == user.telegram_id).first()
    if db_user:
        return db_user

    db_user = User(**user.dict())
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)