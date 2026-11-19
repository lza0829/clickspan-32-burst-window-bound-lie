# 点跨（Clickspan）

点击流装载 → 按访客空闲超时切会话 → 脏事件隔离 → 会话证明互证。

**不是** 运单轨迹时间线（途迹）、变更投影流（changecast）、CDC Sink、券核销钉扎、日结封账、双边对齐、改价价签、证据包放行。  
去皮后是：**事件装载 + 会话窗物化 + 闸/隔离 + 证明**。

## 启动

```powershell
# 后端
cd backend
pip install -r requirements.txt
$env:DATA_DIR = "$PWD\data"
$env:FIXTURES_DIR = "$PWD\..\fixtures"
uvicorn app.main:app --host 127.0.0.1 --port 10900

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5900 |
| 接口 | http://127.0.0.1:10900 |

## UI

**泳道织机**：顶栏品牌 + 顶栏菜单（织机 / 收流 / 搁置 / 核证 / 切窗）+ 横向会话串珠。  
不用底栏导航、不用三栏测绘板、不用左轨编号、不用流程 ribbon。

## 突发去重窗

短时间连点会让同一访客同一 `page_path + event_name` 在突发窗内重复进会话，
末击点被刷屏、切窗间隙失真。本功能在收流入口按窗去重：

- **键**：`visitor_id + page_path + event_name`。
- **窗配置形态**：**配置表** —— `settings` 表 `key='burst_window_sec'`（默认 60s）。
  另在 `hits.dedupe_window_sec` 落每条正式击点入正式时的窗秒快照，仅作核证审计，
  不改变配置来源。
- **失败闭形态**：**入口按窗去重分流** —— 逐击点在正式写入前判定，判定与写入同一
  `BEGIN IMMEDIATE` 事务；判定不可核证（时刻不可解析）时进搁置，绝不放行可疑重复。
- **判定**：取同键正式成员中时距最近的一条（含迟到击点），时距 `<= burst_window_sec`
  即窗内重复。首条仍入正式并更新末击点；越窗后入正式并更新末击点。
- **窗内重复**：写 `quarantine(reason='burst_window_dup')`，**不写 hits、不改
  `hit_count`、不改 `ended_at`（末击点时钟）**。重放已搁置击点幂等跳过。

接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api/settings/burst-window` | 写入 `burst_window_sec`（1..86400） |
| GET | `/api/burst-dedupe/config` | 只读读取窗配置与所选形态 |
| GET | `/api/burst-dedupe/proof` | **只读**核证：正式集 / 搁置重复集 / 末击点时钟 / `hit_count` 互证 |

核证只读，不写库；可传 `?visitor_id=` 过滤。任何同键正式成员落在窗内、搁置记录引用的
正式成员缺失或异键、`ended_at` 与正式最大 `event_ts` 不一致、`hit_count` 与正式点数
不一致，均报为异常。

本地夹具验收（SQLite）：

```bash
cd backend
pytest tests -q          # 13 项，含窗内连点只留首条、搁置可见其余
python -m uvicorn app.main:app --port 10900   # 前端「收流」→「核证」「搁置」
```

