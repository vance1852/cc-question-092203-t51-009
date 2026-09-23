# 换电站运营管理平台（纯后端）

新能源物流车换电站后台管理的纯后端 API 服务，提供站点、车辆和换电记录的统一管理能力。

## 技术栈

- FastAPI + Uvicorn
- SQLAlchemy + SQLite（本地文件，开箱即用）
- PyJWT（JWT 鉴权）
- 密码哈希用标准库 `hashlib.pbkdf2_hmac`，无额外依赖

所有数据本地、离线可运行，不依赖任何外部服务。

## 运行

```bash
pip install -r requirements.txt
python run.py
```

服务启动在 `http://127.0.0.1:7634`，首次启动自动建表并灌入种子数据。
交互式文档：`http://127.0.0.1:7634/docs`。

## 内置账号

首次启动自动创建唯一管理员（本平台只有 admin 一个角色）：

- 用户名：`admin`
- 密码：`admin123`

## 已实现的基础功能

- 登录签发 JWT、获取当前用户（`/api/auth/login`、`/api/auth/me`）
- 换电站增删改查（`/api/stations`，含站点时区 `timezone`）
- 车辆增删改查（`/api/vehicles`）
- 换电记录查询与登记（`/api/swaps`，会联动更新车辆电量与站点可用电池）
- 仪表盘统计（`/api/dashboard/stats`）
- 站点维护窗口（见下）
- 健康检查（`/api/health`）

除 `login` 与 `health` 外，所有接口均需携带 `Authorization: Bearer <token>`。

## 站点维护窗口

设备经理可提前安排维护计划，既支持整站停运，也支持只冻结部分仓位，
无需再把整座站点手工切成维护中。

- 窗口接口（均在站点下）：
  - `POST /api/stations/{id}/maintenance-windows` 创建窗口
  - `GET /api/stations/{id}/maintenance-windows` 列表（可按 `?status=` 过滤
    `scheduled/active/finished/cancelled`）
  - `GET /api/stations/{id}/maintenance-windows/{wid}` 详情
  - `PUT /api/stations/{id}/maintenance-windows/{wid}` 调整窗口
  - `POST /api/stations/{id}/maintenance-windows/{wid}/cancel` 取消窗口
  - `GET /api/stations/{id}/maintenance-windows/{wid}/events` 变更痕迹
  - `GET /api/stations/{id}/capacity-calendar?start_date=&end_date=` 容量日历
  - `GET /api/stations/{id}/min-capacity?start=&end=` 任意未来区间最低可服务容量
- `scope=station` 表示整站停运（冻结全部仓位）；`scope=slots` + `frozen_slots`
  表示冻结指定数量仓位。
- 时间按**站点时区**处理：请求时间为 naive 时按站点 `timezone` 解释，
  带偏移时先换算到 UTC；库内统一存 UTC，响应同时给出 `start_at/end_at`（UTC）
  和 `start_local/end_local`（站点时区），跨午夜区间自然支持。
- 窗口为半开区间 `[start_at, end_at)`：恰好等于开始时刻视为生效，
  等于结束时刻视为失效，站点详情、容量日历、换电准入、最低容量查询共用同一判断。
- 创建/调整窗口时，若与既有未取消窗口叠加后**任一时刻冻结容量超过总仓位**则拒绝；
  缩减站点总仓位时也会校验既有计划。
- 已开始（active）的窗口不可再改开始时刻，但可以延期/提前结束；
  每次创建、调整、取消都写入不可变的变更事件（含前后快照与操作人）；
  已结束/已取消的窗口只读。
- 取消、延期、服务重启后的边界切换都不依赖内存定时器——窗口状态由存储的
  起止字段在每次查询时按当前时刻推导；取消是持久化状态，落库即释放容量。
- 状态优先级（站点详情以 `effective_status` / `status_source` 暴露）：
  1. **手工状态最高**：站点被手工置为 `maintenance`/`offline` 时，无论窗口如何都不可服务；
  2. **整站停运窗口**次之：生效期间有效状态为 `maintenance`、可服务仓位为 0；
  3. **仓位冻结窗口**：不改变站点有效状态，只按 `frozen_slots` 扣减 `available_slots`；
  4. 均不命中时为 `running`。
  换电准入（`POST /api/swaps`）按同一优先级拦截。


## 测试

```bash
pip install -r requirements.txt
pytest -q
```

## 编码说明

源码与数据均为 UTF-8；FastAPI 响应为 UTF-8 JSON，中文不转义、不乱码。
Windows 控制台若为 GBK，仅影响终端打印观感，不影响接口返回。
