# 时档（HourSpan）

**骨架：** 门店营业属性贴源入档 → 业务键变更检测 → Type2 历史行开闭 → 当前镜投影 → 历史与当前互证

- 领域：数据工程（去皮=维表 SCD / 历史开闭 / 当前镜 / 核证；不是门店后台 CRUD）
- 端口：前端 **6200**，接口 **11200**（避开 Chrome 封锁的 6000–6063）
- 公有仓前缀：`lza0829/hourspan-NN-*`
- 栈：Python 3.12 + FastAPI + SQLite；前端 **React + Vite**
- 避让：勿撞筑层分层汇总、价舱改价调度、信汇多源去重、变流 CDC 游标、格湾占用态

本地冒烟：

```powershell
cd claude-workspace/hourspan/_scaffold/backend
pip install -r requirements.txt
$env:DATA_DIR="$PWD/data"; $env:FIXTURES_DIR="$PWD/../fixtures"
python -m uvicorn app.main:app --host 127.0.0.1 --port 11200

cd ../frontend
npm install
npm run dev
```

或：`docker compose up -d --build`（6200 / 11200）

## 检测覆盖成员核证（detect_coverage_member_proof）

架构选型（全局唯一）：**乙 · 可落跑 + 失败闭**。一次检测对约定范围内每条入档快照
都落一条成员分类 `changed / unchanged / skipped`，成员 snapshot_id 集合与范围
snapshot_id 集合须全等证明。

- 范围冻结在 `detect_run_scopes`，成员落库在 `detect_run_members`；跑次头
  `detect_runs` 增加 `scope_count / member_count / missing_count / valid`（老库启动自动补列）。
- 缺员或范围外混入 → 跑次仍落头但 `valid=0`，核证暴露缺员列表；同成败一律禁止
  开 hist 段（`scd.apply_type2` 写闸只认 `valid=1`，且只开成员表 `changed` 行，
  `unchanged/skipped` 永不开段）。
- 跑次定稿不可翻案：事后补成员只进新跑次，禁止把已失败跑次改成 `valid=1`。
- 只读核证接口 `GET /api/detect-coverage?run_id=`（空=最近跑次）：范围数、成员数、
  缺员列表、范围外列表、run_id、valid、实时复算 valid、每条成员 kind。
- 检测在单条 `BEGIN IMMEDIATE` 事务内完成范围冻结→成员落库→跑次定稿，并发同范围
  检测串行化，互不串改。
- 甲口径（齐套才开工、缺员不占成功 run）不采用；两架构对照矩阵见
  `backend/tests/verify_coverage.py` 输出。测试用故障注入：环境变量
  `DETECT_COVERAGE_DROP=<snapshot_id>` 模拟人工少写一条成员。
- 验收：`backend/.venv/bin/python -I backend/tests/verify_coverage.py`
