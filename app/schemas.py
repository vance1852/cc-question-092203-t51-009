"""Pydantic 数据模型（请求体与响应体）。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- 认证 ----------
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str

    model_config = {"from_attributes": True}


# ---------- 换电站 ----------
class StationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    address: str = ""
    slot_total: int = Field(0, ge=0)
    battery_ready: int = Field(0, ge=0)
    status: str = Field("running", pattern="^(running|maintenance|offline)$")
    # IANA 时区，如 Asia/Shanghai；维护窗口的本地时间按它解释
    timezone: str = Field("Asia/Shanghai", min_length=1, max_length=64)


class StationCreate(StationBase):
    pass


class StationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    address: Optional[str] = None
    slot_total: Optional[int] = Field(None, ge=0)
    battery_ready: Optional[int] = Field(None, ge=0)
    status: Optional[str] = Field(None, pattern="^(running|maintenance|offline)$")
    timezone: Optional[str] = Field(None, min_length=1, max_length=64)


class ActiveWindowBrief(BaseModel):
    """站点详情中内嵌的当前生效窗口摘要。"""

    id: int
    title: str
    start_at: datetime
    end_at: datetime
    freeze_slots: int
    whole_station: bool


class StationOut(StationBase):
    id: int
    created_at: datetime
    # 以下为按当前时间实时推导的生效状态（非持久化字段）
    effective_status: Optional[str] = None
    serviceable: Optional[bool] = None
    frozen_slots: int = 0
    serviceable_slots: Optional[int] = None
    active_maintenance_windows: list[ActiveWindowBrief] = []

    model_config = {"from_attributes": True}


# ---------- 车辆 ----------
class VehicleBase(BaseModel):
    plate: str = Field(..., min_length=1, max_length=32)
    model: str = ""
    battery_capacity: float = Field(100.0, gt=0)
    current_soc: float = Field(100.0, ge=0, le=100)
    status: str = Field("idle", pattern="^(idle|running|charging|fault)$")


class VehicleCreate(VehicleBase):
    pass


class VehicleUpdate(BaseModel):
    plate: Optional[str] = Field(None, min_length=1, max_length=32)
    model: Optional[str] = None
    battery_capacity: Optional[float] = Field(None, gt=0)
    current_soc: Optional[float] = Field(None, ge=0, le=100)
    status: Optional[str] = Field(None, pattern="^(idle|running|charging|fault)$")


class VehicleOut(VehicleBase):
    id: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------- 换电记录 ----------
class SwapCreate(BaseModel):
    vehicle_id: int
    station_id: int
    soc_before: float = Field(..., ge=0, le=100)
    soc_after: float = Field(100.0, ge=0, le=100)


class SwapOut(BaseModel):
    id: int
    vehicle_id: int
    station_id: int
    soc_before: float
    soc_after: float
    swapped_at: datetime
    vehicle_plate: Optional[str] = None
    station_name: Optional[str] = None

    model_config = {"from_attributes": True}


# ---------- 仪表盘 ----------
class DashboardStats(BaseModel):
    station_total: int
    station_running: int
    vehicle_total: int
    vehicle_fault: int
    swap_today: int
    battery_ready_total: int


# ---------- 维护窗口 ----------
class MaintenanceWindowCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=128)
    reason: str = ""
    # 本地墙上时间（按站点 timezone 解释）；带不带偏移均可，naive 一律按站点时区
    start_at: datetime
    end_at: datetime
    # 整站停运或冻结指定仓位数，二选一语义：whole_station=True 时忽略 freeze_slots
    whole_station: bool = False
    freeze_slots: Optional[int] = Field(None, ge=0)


class MaintenanceWindowUpdate(BaseModel):
    """调整计划。已开始的窗口只允许延期/缩短结束时间（start_at 不可改）。"""

    title: Optional[str] = Field(None, min_length=1, max_length=128)
    reason: Optional[str] = Field(None, max_length=512)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    whole_station: Optional[bool] = None
    freeze_slots: Optional[int] = Field(None, ge=0)


class MaintenanceWindowCancel(BaseModel):
    reason: str = ""


class MaintenanceWindowReopen(BaseModel):
    """对已结束但未取消的窗口做延期重启（如检修实际超时）。"""

    new_end_at: datetime
    reason: str = ""


class MaintenanceWindowEventOut(BaseModel):
    id: int
    action: str
    changed_by: str
    changed_at: datetime
    note: str
    snapshot: dict

    model_config = {"from_attributes": True}


class MaintenanceWindowOut(BaseModel):
    id: int
    station_id: int
    title: str
    reason: str
    start_at: datetime
    end_at: datetime
    freeze_slots: int
    whole_station: bool
    status: str
    # 实时推导阶段：scheduled/active/completed/cancelled
    phase: str
    timezone: str
    # 按站点时区展示的本地时间
    start_at_local: datetime
    end_at_local: datetime
    created_by: str
    created_at: datetime
    updated_at: datetime
    cancelled_at: Optional[datetime] = None
    cancel_reason: Optional[str] = None

    model_config = {"from_attributes": True}


class MaintenanceWindowDetailOut(MaintenanceWindowOut):
    events: list[MaintenanceWindowEventOut] = []


class CapacityDay(BaseModel):
    date: str
    slot_total: int
    min_serviceable_slots: int
    max_frozen_slots: int
    whole_station: bool
    manual_status: str
    window_ids: list[int]


class MinimumCapacityOut(BaseModel):
    station_id: int
    range_start: datetime
    range_end: datetime
    timezone: str
    minimum_serviceable_slots: int
    block_reason: Optional[str] = None
    worst_at: Optional[datetime] = None
    window_ids: list[int]
