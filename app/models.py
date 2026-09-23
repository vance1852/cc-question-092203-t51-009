"""数据库模型。

业务主题：新能源物流车换电站运营管理。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    """后台用户（本平台只有 admin 一个管理员角色）。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(64), nullable=False, default="管理员")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Station(Base):
    """换电站。"""

    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    address = Column(String(256), nullable=False, default="")
    # 电池仓位总数与当前满电可换电池数
    slot_total = Column(Integer, nullable=False, default=0)
    battery_ready = Column(Integer, nullable=False, default=0)
    # 运营状态：running 运营中 / maintenance 维护中 / offline 离线（手工状态，优先级最高）
    status = Column(String(16), nullable=False, default="running")
    # 站点时区（IANA 名称），维护窗口的起止时间按此时区解释
    timezone = Column(String(64), nullable=False, default="Asia/Shanghai")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="station")
    maintenance_windows = relationship(
        "MaintenanceWindow", back_populates="station", cascade="all, delete-orphan"
    )


class Vehicle(Base):
    """新能源物流车。"""

    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String(32), unique=True, nullable=False, index=True)
    model = Column(String(64), nullable=False, default="")
    battery_capacity = Column(Float, nullable=False, default=100.0)  # kWh
    current_soc = Column(Float, nullable=False, default=100.0)  # 0-100 百分比
    # 状态：idle 空闲 / running 运营 / charging 换电中 / fault 故障
    status = Column(String(16), nullable=False, default="idle")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    swaps = relationship("SwapRecord", back_populates="vehicle")


class SwapRecord(Base):
    """换电记录。"""

    __tablename__ = "swap_records"

    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    soc_before = Column(Float, nullable=False, default=0.0)
    soc_after = Column(Float, nullable=False, default=100.0)
    swapped_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    vehicle = relationship("Vehicle", back_populates="swaps")
    station = relationship("Station", back_populates="swaps")


class MaintenanceWindow(Base):
    """站点维护窗口。

    - scope=station：整站停运；scope=slots：冻结指定数量仓位。
    - start_at / end_at 统一存 UTC（naive，与全库一致），按半开区间
      [start_at, end_at) 生效：恰好等于开始时刻视为生效，等于结束时刻视为失效。
    - 生效/失效、延期、取消全部由存储字段在查询时推导，不依赖任何内存定时器，
      服务重启后边界行为一致。
    """

    __tablename__ = "maintenance_windows"

    id = Column(Integer, primary_key=True, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # 范围：station 整站停运 / slots 冻结部分仓位
    scope = Column(String(16), nullable=False, default="slots")
    # 仅 scope=slots 时有意义：冻结的仓位数；整站停运时为空（贡献=站点总仓位）
    frozen_slots = Column(Integer, nullable=True)
    start_at = Column(DateTime, nullable=False, index=True)  # UTC
    end_at = Column(DateTime, nullable=False)  # UTC
    reason = Column(String(256), nullable=False, default="")
    # 取消时间；非空即视为已取消（持久化状态，非定时器）
    cancelled_at = Column(DateTime, nullable=True)
    created_by = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    station = relationship("Station", back_populates="maintenance_windows")
    events = relationship(
        "MaintenanceWindowEvent",
        back_populates="window",
        cascade="all, delete-orphan",
        order_by="MaintenanceWindowEvent.id",
    )


class MaintenanceWindowEvent(Base):
    """维护窗口变更痕迹（创建 / 调整 / 取消均留痕，含前后快照）。"""

    __tablename__ = "maintenance_window_events"

    id = Column(Integer, primary_key=True, index=True)
    window_id = Column(Integer, ForeignKey("maintenance_windows.id"), nullable=False, index=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False, index=True)
    # 动作：created 创建 / updated 调整 / cancelled 取消
    action = Column(String(16), nullable=False)
    # 变更前后快照：{"before": {...}|None, "after": {...}|None}
    detail = Column(JSON, nullable=False, default=dict)
    changed_by = Column(String(64), nullable=False, default="")
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    window = relationship("MaintenanceWindow", back_populates="events")
