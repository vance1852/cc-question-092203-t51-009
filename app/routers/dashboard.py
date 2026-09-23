"""仪表盘统计路由（需登录）。"""
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..auth import get_current_user
from ..database import get_db
from .. import maintenance as mw
from ..models import Station, SwapRecord, Vehicle
from ..schemas import DashboardStats

router = APIRouter(prefix="/api/dashboard", tags=["仪表盘"], dependencies=[Depends(get_current_user)])


@router.get("/stats", response_model=DashboardStats)
def stats(db: Session = Depends(get_db)):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    stations = db.query(Station).all()
    # 运营中数量按维护窗口实时推导后的生效状态统计（整站停运窗口期间不计入）
    effective_running = sum(
        1 for s in stations if mw.effective_state(db, s)["effective_status"] == "running"
    )
    return DashboardStats(
        station_total=len(stations),
        station_running=effective_running,
        vehicle_total=db.query(Vehicle).count(),
        vehicle_fault=db.query(Vehicle).filter(Vehicle.status == "fault").count(),
        swap_today=db.query(SwapRecord).filter(SwapRecord.swapped_at >= today_start).count(),
        battery_ready_total=db.query(func.coalesce(func.sum(Station.battery_ready), 0)).scalar() or 0,
    )
