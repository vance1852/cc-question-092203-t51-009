"""维护窗口功能测试。

覆盖：整站停运/仓位冻结、站点时区跨日区间、容量叠加拒绝、半开区间边界、
站点详情/容量日历/换电准入/最低容量、手工状态优先级、变更痕迹、
取消与延期的持久化边界切换（无内存定时器）。
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.main import app
from app.seed import init_db

init_db()
client = TestClient(app)

SH = ZoneInfo("Asia/Shanghai")
LA = ZoneInfo("America/Los_Angeles")
UTC = dt_timezone.utc


def _headers() -> dict:
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_station(headers, slot_total=10, battery_ready=5, tz="Asia/Shanghai", status="running"):
    resp = client.post(
        "/api/stations",
        json={
            "name": f"窗口测试站-{id(object()):x}",
            "slot_total": slot_total,
            "battery_ready": battery_ready,
            "timezone": tz,
            "status": status,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_vehicle(headers, soc=10.0):
    resp = client.post(
        "/api/vehicles",
        json={"plate": f"测窗口{datetime.now(UTC).strftime('%H%M%S%f')}", "current_soc": soc},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _window(headers, sid, **payload):
    return client.post(f"/api/stations/{sid}/maintenance-windows", json=payload, headers=headers)


# ---------- 创建与校验 ----------
def test_create_slots_window_scheduled():
    h = _headers()
    s = _create_station(h)
    resp = _window(
        h, s["id"], scope="slots", frozen_slots=4,
        start_time="2030-01-15T08:00:00", end_time="2030-01-15T12:00:00",
        reason="仓位检修",
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["scope"] == "slots"
    assert data["frozen_slots"] == 4
    assert data["status"] == "scheduled"
    assert data["reason"] == "仓位检修"
    assert data["start_local"].endswith("+08:00")


def test_create_validations():
    h = _headers()
    s = _create_station(h, slot_total=10)
    base = {"start_time": "2030-02-01T08:00:00", "end_time": "2030-02-01T10:00:00"}
    # slots 范围缺冻结数
    r = _window(h, s["id"], scope="slots", **base)
    assert r.status_code == 422
    # 冻结数超过总仓位
    r = _window(h, s["id"], scope="slots", frozen_slots=11, **base)
    assert r.status_code == 422
    # 整站停运又传冻结数
    r = _window(h, s["id"], scope="station", frozen_slots=3, **base)
    assert r.status_code == 422
    # 结束不晚于开始
    r = _window(h, s["id"], scope="station",
                start_time="2030-02-01T10:00:00", end_time="2030-02-01T10:00:00")
    assert r.status_code == 422
    # 非法站点时区
    r = client.post("/api/stations", json={"name": "坏时区站", "timezone": "Mars/Base"}, headers=h)
    assert r.status_code == 422


# ---------- 站点时区与跨日 ----------
def test_naive_times_interpreted_in_station_tz_cross_midnight():
    h = _headers()
    s = _create_station(h, tz="Asia/Shanghai")
    # 本地 22:00 到次日 02:00 的跨午夜计划
    r = _window(
        h, s["id"], scope="slots", frozen_slots=3,
        start_time="2030-01-15T22:00:00", end_time="2030-01-16T02:00:00",
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["start_at"] == "2030-01-15T14:00:00"
    assert data["end_at"] == "2030-01-15T18:00:00"
    assert data["start_local"] == "2030-01-15T22:00:00+08:00"
    assert data["end_local"] == "2030-01-16T02:00:00+08:00"


def test_other_timezone_and_offset_input():
    h = _headers()
    # 洛杉矶（1 月为 PST，UTC-8）
    s = _create_station(h, tz="America/Los_Angeles")
    r = _window(
        h, s["id"], scope="station",
        start_time="2030-01-15T20:00:00", end_time="2030-01-15T23:00:00",
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["start_at"] == "2030-01-16T04:00:00"
    assert data["start_local"] == "2030-01-15T20:00:00-08:00"

    # 显式带偏移的输入同样先换算到 UTC
    r2 = _window(
        h, s["id"], scope="slots", frozen_slots=1,
        start_time="2030-03-01T20:00:00+00:00", end_time="2030-03-01T21:00:00+00:00",
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["start_local"] == "2030-03-01T12:00:00-08:00"


# ---------- 容量叠加 ----------
def test_overlap_frozen_capacity_rejected_but_adjacent_allowed():
    h = _headers()
    s = _create_station(h, slot_total=10)
    r1 = _window(h, s["id"], scope="slots", frozen_slots=6,
                 start_time="2030-04-01T00:00:00", end_time="2030-04-03T00:00:00")
    assert r1.status_code == 201, r1.text
    # 重叠时段 6+5=11 > 10，拒绝
    r2 = _window(h, s["id"], scope="slots", frozen_slots=5,
                 start_time="2030-04-02T00:00:00", end_time="2030-04-04T00:00:00")
    assert r2.status_code == 422
    assert "超过站点总仓位" in r2.json()["detail"]
    # 首尾相接（半开区间，不重叠）允许
    r3 = _window(h, s["id"], scope="slots", frozen_slots=10,
                 start_time="2030-04-03T00:00:00", end_time="2030-04-04T00:00:00")
    assert r3.status_code == 201, r3.text


def test_station_scope_overlap_rejected():
    h = _headers()
    s = _create_station(h, slot_total=10)
    r1 = _window(h, s["id"], scope="station",
                 start_time="2030-05-01T00:00:00", end_time="2030-05-02T00:00:00")
    assert r1.status_code == 201
    # 整站停运与任何窗口重叠都等于总仓位+，拒绝
    r2 = _window(h, s["id"], scope="slots", frozen_slots=1,
                 start_time="2030-05-01T12:00:00", end_time="2030-05-02T12:00:00")
    assert r2.status_code == 422
    # 两个整站停运窗口首尾相接允许
    r3 = _window(h, s["id"], scope="station",
                 start_time="2030-05-02T00:00:00", end_time="2030-05-03T00:00:00")
    assert r3.status_code == 201, r3.text


def test_update_into_conflict_rejected_and_audited():
    h = _headers()
    s = _create_station(h, slot_total=10)
    a = _window(h, s["id"], scope="slots", frozen_slots=6,
                start_time="2030-06-01T00:00:00", end_time="2030-06-02T00:00:00").json()
    b = _window(h, s["id"], scope="slots", frozen_slots=5,
                start_time="2030-06-03T00:00:00", end_time="2030-06-04T00:00:00").json()
    r = client.put(
        f"/api/stations/{s['id']}/maintenance-windows/{b['id']}",
        json={"start_time": "2030-06-01T12:00:00"}, headers=h,
    )
    assert r.status_code == 422
    # 被拒绝后原计划不变（start_at 为 UTC，本地 06-03 00:00 +08:00 = UTC 06-02 16:00）
    got = client.get(f"/api/stations/{s['id']}/maintenance-windows/{b['id']}", headers=h).json()
    assert got["start_at"] == "2030-06-02T16:00:00"
    # 合法调整成功
    ok = client.put(
        f"/api/stations/{s['id']}/maintenance-windows/{a['id']}",
        json={"frozen_slots": 5}, headers=h,
    )
    assert ok.status_code == 200, ok.text
    events = client.get(
        f"/api/stations/{s['id']}/maintenance-windows/{a['id']}/events", headers=h
    ).json()
    actions = [e["action"] for e in events]
    assert actions == ["created", "updated"]
    assert events[1]["detail"]["before"]["frozen_slots"] == 6
    assert events[1]["detail"]["after"]["frozen_slots"] == 5


# ---------- 生效窗口对站点详情与准入的影响 ----------
def test_active_slots_window_station_detail():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    now = datetime.now(UTC)
    _window(h, s["id"], scope="slots", frozen_slots=4,
            start_time=(now - timedelta(minutes=2)).isoformat(),
            end_time=(now + timedelta(hours=1)).isoformat())
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["status"] == "running"  # 手工状态仍是运营
    assert detail["effective_status"] == "running"  # 部分冻结不改状态
    assert detail["frozen_slots"] == 4
    assert detail["available_slots"] == 6


def test_active_station_window_and_swap_admission():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    v = _create_vehicle(h)
    now = datetime.now(UTC)
    _window(h, s["id"], scope="station",
            start_time=(now - timedelta(minutes=2)).isoformat(),
            end_time=(now + timedelta(hours=1)).isoformat())
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["effective_status"] == "maintenance"
    assert detail["status_source"] == "window"
    assert detail["available_slots"] == 0
    swap = client.post("/api/swaps",
                       json={"vehicle_id": v["id"], "station_id": s["id"],
                             "soc_before": 10.0, "soc_after": 100.0}, headers=h)
    assert swap.status_code == 422
    assert "整站停运" in swap.json()["detail"]


def test_partial_freeze_swap_allowed_but_full_freeze_blocked():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    v = _create_vehicle(h)
    now = datetime.now(UTC)
    _window(h, s["id"], scope="slots", frozen_slots=6,
            start_time=(now - timedelta(minutes=2)).isoformat(),
            end_time=(now + timedelta(hours=1)).isoformat())
    ok = client.post("/api/swaps",
                     json={"vehicle_id": v["id"], "station_id": s["id"],
                           "soc_before": 10.0, "soc_after": 100.0}, headers=h)
    assert ok.status_code == 201, ok.text
    assert client.get(f"/api/stations/{s['id']}", headers=h).json()["battery_ready"] == 4

    s2 = _create_station(h, slot_total=10, battery_ready=5)
    v2 = _create_vehicle(h)
    _window(h, s2["id"], scope="slots", frozen_slots=10,
            start_time=(now - timedelta(minutes=2)).isoformat(),
            end_time=(now + timedelta(hours=1)).isoformat())
    blocked = client.post("/api/swaps",
                          json={"vehicle_id": v2["id"], "station_id": s2["id"],
                                "soc_before": 10.0, "soc_after": 100.0}, headers=h)
    assert blocked.status_code == 422
    assert "全部仓位" in blocked.json()["detail"]


def test_future_window_does_not_affect_now():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    _window(h, s["id"], scope="station",
            start_time="2030-07-01T00:00:00", end_time="2030-07-02T00:00:00")
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["effective_status"] == "running"
    assert detail["available_slots"] == 10


# ---------- 手工状态优先级 ----------
def test_manual_status_takes_priority():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    v = _create_vehicle(h)
    now = datetime.now(UTC)
    _window(h, s["id"], scope="slots", frozen_slots=3,
            start_time=(now - timedelta(minutes=2)).isoformat(),
            end_time=(now + timedelta(hours=1)).isoformat())
    # 手工切维护：优先级高于一切窗口
    client.put(f"/api/stations/{s['id']}", json={"status": "maintenance"}, headers=h)
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["effective_status"] == "maintenance"
    assert detail["status_source"] == "manual"
    assert detail["available_slots"] == 0
    swap = client.post("/api/swaps",
                       json={"vehicle_id": v["id"], "station_id": s["id"],
                             "soc_before": 10.0, "soc_after": 100.0}, headers=h)
    assert swap.status_code == 422 and "手工" in swap.json()["detail"]
    # 手工恢复运营后，窗口冻结重新生效
    client.put(f"/api/stations/{s['id']}", json={"status": "running"}, headers=h)
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["effective_status"] == "running"
    assert detail["frozen_slots"] == 3
    assert detail["available_slots"] == 7


# ---------- 半开区间边界一致性 ----------
def test_half_open_boundary_unit():
    from app import maintenance as m
    from app.models import MaintenanceWindow

    w = MaintenanceWindow(
        scope="slots",
        frozen_slots=4,
        start_at=datetime(2030, 8, 1, 0, 0),
        end_at=datetime(2030, 8, 2, 0, 0),
    )
    # 恰好开始时刻：生效；恰好结束时刻：失效
    assert m.is_active_at(w, w.start_at) is True
    assert m.is_active_at(w, w.end_at) is False
    assert m.window_status(w, w.start_at) == "active"
    assert m.window_status(w, w.end_at) == "finished"
    assert m.window_status(w, w.start_at - timedelta(seconds=1)) == "scheduled"


def test_min_capacity_boundary_exact_instants():
    h = _headers()
    s = _create_station(h, slot_total=20)
    _window(h, s["id"], scope="slots", frozen_slots=8,
            start_time="2030-09-01T00:00:00", end_time="2030-09-02T00:00:00")
    base = f"/api/stations/{s['id']}/min-capacity"
    # 查询区间恰好等于窗口区间：开始时刻纳入（冻结 8，最低 12）
    r = client.get(base, params={"start": "2030-09-01T00:00:00+08:00",
                                 "end": "2030-09-02T00:00:00+08:00"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["max_frozen_slots"] == 8
    assert r.json()["min_available_slots"] == 12
    # 区间结束恰好落在窗口开始时刻：窗口不纳入
    r2 = client.get(base, params={"start": "2030-08-31T00:00:00+08:00",
                                  "end": "2030-09-01T00:00:00+08:00"}, headers=h)
    assert r2.json()["max_frozen_slots"] == 0
    assert r2.json()["min_available_slots"] == 20


def test_min_capacity_overlapping_windows():
    h = _headers()
    s = _create_station(h, slot_total=20)
    _window(h, s["id"], scope="slots", frozen_slots=6,
            start_time="2030-10-01T00:00:00", end_time="2030-10-03T00:00:00")
    _window(h, s["id"], scope="slots", frozen_slots=5,
            start_time="2030-10-02T00:00:00", end_time="2030-10-04T00:00:00")
    r = client.get(f"/api/stations/{s['id']}/min-capacity",
                   params={"start": "2030-10-01T00:00:00+08:00",
                           "end": "2030-10-05T00:00:00+08:00"}, headers=h)
    data = r.json()
    assert data["max_frozen_slots"] == 11  # 10-02 ~ 10-03 叠加
    assert data["min_available_slots"] == 9


# ---------- 容量日历（跨日） ----------
def test_capacity_calendar_cross_midnight():
    h = _headers()
    s = _create_station(h, slot_total=10)
    _window(h, s["id"], scope="slots", frozen_slots=6,
            start_time="2030-11-15T22:00:00", end_time="2030-11-16T02:00:00")
    r = client.get(f"/api/stations/{s['id']}/capacity-calendar",
                   params={"start_date": "2030-11-15", "end_date": "2030-11-16"}, headers=h)
    assert r.status_code == 200, r.text
    days = r.json()
    assert [d["date"] for d in days] == ["2030-11-15", "2030-11-16"]
    assert days[0]["min_available_slots"] == 4
    assert days[1]["min_available_slots"] == 4
    assert len(days[0]["windows"]) == 1 and len(days[1]["windows"]) == 1
    # 日历返回的本地时刻保留跨日形态
    assert days[1]["windows"][0]["start_local"].endswith("22:00:00+08:00")


# ---------- 已开始窗口的变更限制与留痕、取消 ----------
def test_active_window_restrictions_cancel_and_immediate_switch():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    v = _create_vehicle(h)
    now = datetime.now(UTC)
    w = _window(h, s["id"], scope="slots", frozen_slots=10,
                start_time=(now - timedelta(minutes=5)).isoformat(),
                end_time=(now + timedelta(hours=2)).isoformat()).json()
    # 生效中：全部仓位冻结，换电被拒
    swap = client.post("/api/swaps",
                       json={"vehicle_id": v["id"], "station_id": s["id"],
                             "soc_before": 10.0, "soc_after": 100.0}, headers=h)
    assert swap.status_code == 422
    # 已开始窗口不可前移/后移开始时刻
    r = client.put(f"/api/stations/{s['id']}/maintenance-windows/{w['id']}",
                   json={"start_time": (now - timedelta(hours=1)).isoformat()}, headers=h)
    assert r.status_code == 422 and "开始时刻" in r.json()["detail"]
    # 延期结束时刻允许并留痕
    r2 = client.put(f"/api/stations/{s['id']}/maintenance-windows/{w['id']}",
                    json={"end_time": (now + timedelta(hours=5)).isoformat()}, headers=h)
    assert r2.status_code == 200, r2.text

    # 取消是持久化状态：落库后能力立即恢复，不经过任何内存定时器
    cancel = client.post(
        f"/api/stations/{s['id']}/maintenance-windows/{w['id']}/cancel", headers=h
    )
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "cancelled"
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["frozen_slots"] == 0 and detail["available_slots"] == 10
    swap2 = client.post("/api/swaps",
                        json={"vehicle_id": v["id"], "station_id": s["id"],
                              "soc_before": 10.0, "soc_after": 100.0}, headers=h)
    assert swap2.status_code == 201, swap2.text
    # 重复取消被拒
    again = client.post(
        f"/api/stations/{s['id']}/maintenance-windows/{w['id']}/cancel", headers=h
    )
    assert again.status_code == 422
    # 痕迹完整：创建、延期、取消
    events = client.get(
        f"/api/stations/{s['id']}/maintenance-windows/{w['id']}/events", headers=h
    ).json()
    assert [e["action"] for e in events] == ["created", "updated", "cancelled"]
    assert events[-1]["detail"]["before"]["cancelled_at"] is None
    assert events[-1]["detail"]["after"]["cancelled_at"] is not None
    # 已取消窗口不可再调整
    edit = client.put(f"/api/stations/{s['id']}/maintenance-windows/{w['id']}",
                      json={"reason": "x"}, headers=h)
    assert edit.status_code == 422


def test_end_active_window_early_switches_boundary_without_timer():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    now = datetime.now(UTC)
    w = _window(h, s["id"], scope="station",
                start_time=(now - timedelta(minutes=5)).isoformat(),
                end_time=(now + timedelta(hours=2)).isoformat()).json()
    assert client.get(f"/api/stations/{s['id']}", headers=h).json()["effective_status"] == "maintenance"
    # 现场提前修好：把结束时刻改到过去，下一次查询立即恢复运营（无需重启/定时器）
    r = client.put(
        f"/api/stations/{s['id']}/maintenance-windows/{w['id']}",
        json={"end_time": (now - timedelta(seconds=10)).isoformat()}, headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "finished"
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["effective_status"] == "running"
    assert detail["status_source"] == "none"


def test_finished_window_read_only():
    h = _headers()
    s = _create_station(h)
    now = datetime.now(UTC)
    w = _window(h, s["id"], scope="slots", frozen_slots=2,
                start_time=(now - timedelta(hours=2)).isoformat(),
                end_time=(now - timedelta(hours=1)).isoformat()).json()
    assert client.get(
        f"/api/stations/{s['id']}/maintenance-windows/{w['id']}", headers=h
    ).json()["status"] == "finished"
    assert client.put(
        f"/api/stations/{s['id']}/maintenance-windows/{w['id']}",
        json={"reason": "改"}, headers=h).status_code == 422
    assert client.post(
        f"/api/stations/{s['id']}/maintenance-windows/{w['id']}/cancel", headers=h
    ).status_code == 422
    # 已结束窗口不影响当前能力
    assert client.get(f"/api/stations/{s['id']}", headers=h).json()["available_slots"] == 10


# ---------- 仪表盘口径 ----------
def test_dashboard_excludes_stopped_by_window():
    h = _headers()
    before = client.get("/api/dashboard/stats", headers=h).json()["station_running"]
    s = _create_station(h, slot_total=10)
    now = datetime.now(UTC)
    _window(h, s["id"], scope="station",
            start_time=(now - timedelta(minutes=1)).isoformat(),
            end_time=(now + timedelta(hours=1)).isoformat())
    after = client.get("/api/dashboard/stats", headers=h).json()["station_running"]
    assert after == before  # 新站被整站停运窗口覆盖，不计入运营


# ---------- 取消释放容量 / 总仓位调整 / 读时推导 ----------
def test_cancelled_window_frees_capacity_for_new_plan():
    h = _headers()
    s = _create_station(h, slot_total=10)
    w = _window(h, s["id"], scope="slots", frozen_slots=10,
                start_time="2030-12-01T00:00:00", end_time="2030-12-02T00:00:00").json()
    # 未取消时，同段再冻结 1 仓即超载
    conflict = _window(h, s["id"], scope="slots", frozen_slots=1,
                       start_time="2030-12-01T12:00:00", end_time="2030-12-02T12:00:00")
    assert conflict.status_code == 422
    client.post(f"/api/stations/{s['id']}/maintenance-windows/{w['id']}/cancel", headers=h)
    # 取消后容量释放，原计划不再冲突
    ok = _window(h, s["id"], scope="slots", frozen_slots=1,
                 start_time="2030-12-01T12:00:00", end_time="2030-12-02T12:00:00")
    assert ok.status_code == 201, ok.text


def test_shrink_slot_total_validates_existing_plans():
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=0)
    _window(h, s["id"], scope="slots", frozen_slots=8,
            start_time="2031-01-01T00:00:00", end_time="2031-01-02T00:00:00")
    # 缩减到 6：既有窗口需冻结 8 仓，拒绝
    bad = client.put(f"/api/stations/{s['id']}", json={"slot_total": 6}, headers=h)
    assert bad.status_code == 422
    assert "维护窗口" in bad.json()["detail"]
    # 缩减到 8：恰好可行
    ok = client.put(f"/api/stations/{s['id']}", json={"slot_total": 8}, headers=h)
    assert ok.status_code == 200, ok.text


def test_no_timer_state_derived_at_read_time():
    """窗口边界切换由存储的起止时刻在查询时推导，无需任何内存定时器或重启动作。"""
    h = _headers()
    s = _create_station(h, slot_total=10, battery_ready=5)
    v = _create_vehicle(h)
    now = datetime.now(UTC)
    # 窗口在 2 秒前自然结束（期间服务不做任何事，等价于跨过边界的重启）
    _window(h, s["id"], scope="station",
            start_time=(now - timedelta(hours=1)).isoformat(),
            end_time=(now - timedelta(seconds=2)).isoformat())
    detail = client.get(f"/api/stations/{s['id']}", headers=h).json()
    assert detail["effective_status"] == "running"
    assert detail["available_slots"] == 10
    swap = client.post("/api/swaps",
                       json={"vehicle_id": v["id"], "station_id": s["id"],
                             "soc_before": 10.0, "soc_after": 100.0}, headers=h)
    assert swap.status_code == 201, swap.text
