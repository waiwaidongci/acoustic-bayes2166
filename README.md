# 桐弦声检故障判读册

GaussianNB与分层交叉验证、混淆矩阵和后验解释。为桐弦研究协作组的12位分析与业务人员提供从样本核对、独立worker计算到批准快照交付的完整工作流。

## 真实业务场景

合成声学特征→故障类型判读→复检批注；真实训练/验证分离、训练内标准化、类先验展示。团队原先将 `acoustic-bayes-202611-01.csv` 复制到多份人工表格，复核时再逐项对照。三个痛点是口径不可追溯、重复计算易错、业务说明与结果脱节。上游提供合成业务观察，系统承担研究和复核，下游业务人员读取批准快照。全部演示数据是固定随机种子生成的模拟数据。

## 业务模块

1. 业务总览：批次、样本、任务与批准交付账册。
2. 声检批次：创建/修改来源说明、查看样本、用真实业务字段追加记录；列表分页。
3. 分析配置：冻结来源、参数和全部样本，进入Redis队列。
4. 完整结果：真实算法摘要、专属诊断、全部CSV导出和可恢复失败态。
5. 备件反演：读取已完成研究的折外混淆矩阵，冻结判读件数与允许偏差，按整件求指定故障最少/最多占比及可复原判读计数的构成；两端齐备才发布，更正件数另开新版。
6. 复检批注：分析师草案→负责人批准→阅读者下载批准快照。
7. 审计：登录、来源修改、任务worker完成、备件版本创建/发布/取代、业务说明批准等事件。

## 快速启动

```bash
cp .env.example .env
docker compose config --quiet
docker compose up -d --build
# 可重复执行，不覆盖人工修改
docker compose exec -T backend python -m scripts.seed_demo
```

访问 [前端](http://localhost:18245)；[API文档](http://localhost:19245/docs)。数据库仅演示用途映射 `17745`。首次准备依赖和初始化数据库需要等待健康状态：`docker compose ps`，五个服务均需healthy。

| 组件 | 技术与职责 |
|---|---|
| frontend | 原生 HTML / CSS / JS ES Modules，Nginx代理 `/api/` |
| backend | Python3.11 / FastAPI / SQLAlchemy / JWT |
| worker | 独立Python进程，Redis BLPOP队列，持久化结果/CSV/心跳 |
| db | MySQL8，命名卷持久化 |
| redis | Redis7，AOF命名卷 |

恰好五服务，均配置healthcheck；后端/worker依赖数据库与Redis健康，前端依赖后端健康；worker与后端共享 `artifacts` 命名卷。重置演示环境：`docker compose down -v --remove-orphans`，会删除本项目卷。

## 账号与权限

所有演示账号密码：`Research2026!`。

| 邮箱 | 角色 | 权限 |
|---|---|---|
| admin@acoustic-bayes.demo | admin | 全部录入/执行、批准业务说明 |
| analyst@acoustic-bayes.demo | analyst | 录入来源/样本、执行、提交说明，无批准权 |
| viewer@acoustic-bayes.demo | viewer | 只读、导出结果及已批准快照、查看已发布备件版本 |

时间在数据库/API中以UTC存储，总览与审计页面明确转换为Asia/Shanghai北京时间。

JWT有效期8小时；PBKDF2盐化哈希保存密码；角色每请求从数据库读取。访客写操作403，匿名访问业务数据401，重复样本409，非法业务字段422，重复批准409。每客户端/路径写请求90次/分钟，超限429。生产使用需修改JWT及演示数据库凭据。

## 实体与字段

AcousticBatch / AcousticSample / ClassifierRun / InspectionNote 是核心实体；另有User和Audit。数据库实际表：`acoustic_batches` → `acoustic_samples` → `classifier_runs` → `inspection_notes`。样本以固定字段JSON保存，后端按以下字段严格验证，不接受未知字段替代必填值：均方根振幅（`rms`）、频谱质心 Hz（`centroid`）、峭度（`kurtosis`）、复核类型（`label`）。

备件反演另有 `SparesStudy` / `SparesVersion` / `SparesEnd`（表 `spares_studies` → `spares_versions` → `spares_ends`）。一份已完成研究对应一个反演研究；每个版本冻结折外混淆矩阵、故障类型顺序、各类型判读件数、允许偏差（百分点）与目标故障，并保存输入校验和。每个版本拆成 `min`/`max` 两个端，各自记录状态、尝试次数与结果。

实体贯穿 `backend/src/models`、`schemas`、`services`、`controllers`、`routes`，以及 `frontend/src/api/research.js`、`stores/research.js` 和 `pages`。`Source.key` 与 `(Record.source_id, reference)` 为自然键；来源改名不会修改已冻结任务输入。业务说明在创建时保存结果快照，批准后从该快照导出。

## 算法与口径

GaussianNB与分层交叉验证、混淆矩阵和后验解释，全部计算位于 `backend/src/services/algorithm.py`，前端不拟合模型、不生成模拟结果。输入是冻结业务观察及参数，输出summary、专属诊断、逐行计算明细、seed与输入SHA256。CSV包含全部结果行；页面仅预览前30行。参数：

- `folds`：分层交叉验证折数，默认 `3`。
- `smoothing`：方差平滑系数，默认 `1e-09`。

分层交叉验证，每折只在训练行拟合均值与方差；后验概率是模型分布，不是检修结论。

## 备件整件反演

在「完整结果」页点击“按判读件数反演备件区间”，或在导航「备件反演」选择已完成研究。冻结一份已完成研究的折外混淆矩阵 C：对真类 i，其判读去向 j 的基准比例是 `C[i][j] / Σ_j C[i][j]`；允许偏差 t（百分点）给出浮动带 `[max(0, p-t), min(1, p+t)]`，并且同一真类各去向比例之和仍为 100%。

给定新批各判读去向的件数 `counts[j]`，求整件构成：真类件数 `n[i]` 与整数矩阵 `A[i][j]` 满足

- `Σ_j A[i][j] = n[i]`（行合计等于该真类件数），`Σ_i A[i][j] = counts[j]`（列合计恰为给定判读件数）；
- `n[i] > 0` 时 `lower[i][j] ≤ A[i][j]/n[i] ≤ upper[i][j]`；`n[i] = 0` 时该行全为 0。

分别最小化与最大化目标故障的 `n[target]`。求解位于 `backend/src/services/spares_math.py`，使用整数规划，随后用精确有理数逐项复核行合计、列合计与比例带；复核不过不发布。两端都给出：可行时输出最少/最多整件数与完整构成表（含各格比例），不可行时说明原因——例如某去向件数为 0 但每个真类在该去向的比例下限都大于 0、某个真类的可行件数区间内没有整数、或比例带内连分数件数都无解。

工作流约定：

- **版本**：每次冻结（包括仅更正判读件数）都新建一版，版本号只增。创建版本即冻结该版资料，此后来源、研究结果或更高版本都不影响它。
- **双端与发布**：每个版本有 `min`/`max` 两端，由独立 worker 分别计算；两端都得出终局（有构成或明确无解）且该版仍是研究最新版时，版本才标为 `published` 并在页面显示。页面只显示当前版本；历史版本折叠在「版本记录」中，不显示构成。
- **晚到旧计算**：旧版本晚到的结果只把该版标为 `superseded`，不会覆盖研究页面的最新版。
- **中断恢复**：worker 领取端点是条件更新，完成后条件迁移状态并各写一次审计；同一端重跑直接跳过，不会重复发布。worker 启动时与每 15 秒做恢复扫描，对运行中超时（`SPARES_STALE_SECONDS`，默认 180 秒）或从未推送的端用**该版冻结资料**重推；失败端保留原资料，可在页面用同一版资料重跑。
- **权限**：分析师/负责人冻结与重跑，阅读者只读已发布版本。

失败态：业务字段缺失/非有限数值、参数越界、样本不足、退化特征或结构错误均明确拒绝或将任务标failed，错误消息保存在任务中。可回到来源或配置修正后重新执行，历史失败记录保留。

已知边界：独立同分布、特征条件独立和高斯类条件是假设；后验未校准。演示数据刻意有分离度，准确率不代表真实设备效果。使用分层折外验证，不提供外部最终测试集。

## 演示数据与一天剧本

固定seed=42，3个来源 ` TX-202611-01/02/03`，每来源240条，共720条业务记录；三个账号和首轮种子任务。自然键检查后仅插入缺失数据，连跑seed不重复、不覆盖用户修改。`SEED_DEMO=true` 时启动自动执行；失败记录日志但不阻塞服务。首轮任务同样由独立worker真实计算。

1. 09:00 负责人查看三份来源和720条样本。
2. 09:10 分析师选择 `TX-202611-01 秋季常规组`，核对 `acoustic-bayes-202611-01.csv`。
3. 09:15 在“声检批次”核对均方根振幅（`rms`）、频谱质心 Hz（`centroid`）、峭度（`kurtosis`）、复核类型（`label`），追加编号 `TX-NEW-001` 的样本。
4. 09:20 新建“十一月复核研究”，按默认参数冻结241条输入。
5. 09:21 独立worker消费Redis队列，返回实算结果与输入校验和。
6. 09:25 分析师查看专属诊断及逐行结果，并下载CSV复算。
7. 09:30 提交“十一月业务复核意见”，负责人核对并批准。
8. 09:40 viewer下载批准快照，查看审计中的worker完成与批准事件。
9. 09:45 在「备件反演」选择首轮种子研究，以各判读去向件数冻结一版；worker 分别求目标故障的最少/最多整件构成。首轮种子研究的演示版本在任务完成后自动冻结一版（判读件数取折外混淆矩阵的预测列合计）。

## 目录与本地开发

```text
frontend/src/api, stores, types, components/common, hooks, pages, router, utils, constants
backend/src/routes, controllers, services, models, schemas, middlewares, utils, constants, config
backend/scripts/seed_demo.py
backend/migrations/001_initial.sql
```

本地后端：Python3.11虚拟环境安装 `backend/requirements.txt`，设置 `DATABASE_URL`、`REDIS_URL`、`ARTIFACT_DIR`，在backend目录运行 `uvicorn src.main:app --reload`；另开终端运行 `python -m src.worker`。原生前端无需编译，推荐以Compose Nginx访问，保证 `/api/` 代理。前端为Google Fonts双字体、oklch色板，离线字体回退serif；1280/768/390响应布局。

| 环境变量 | 默认与说明 |
|---|---|
| SPARES_STALE_SECONDS / SPARES_MAX_ATTEMPTS | `180 / 3`，备件端运行中判定超时的秒数与求解重试上限 |
| COMPOSE_PROJECT_NAME | `acoustic-bayes`，隔离容器/网络/命名卷 |
| FRONTEND_PORT / BACKEND_PORT / DB_PORT | `18245 / 19245 / 17745` |
| SEED_DEMO | `true` |
| JWT_SECRET | `.env.example`演示值，部署前修改 |
| DATABASE_URL / REDIS_URL | Compose内部连接，定义在compose的env锚点 |
| ARTIFACT_DIR | `/artifacts`，后端worker共享 |

共享枚举：`backend/src/constants/enums.py` 与 `frontend/src/constants/enums.js` 都定义 admin/analyst/viewer、queued/running/completed/failed、draft/approved。后端service检查对应字符串；README为全量值说明。共享前端组件table/status/chart/business跨总览、结果、配置与复核复用；actions hook和format utils负责交互/格式。后端errors与rate_limit是两个中间件。

测试及截图在批根 `test-artifacts/gb-0041/`，不混入源码。独立算法oracle、HTTP完整流程和真实Playwright浏览器流程见该目录 `TESTING.md`；备件反演的数学与版本流测试见同目录 `test_spares_math.py`、`test_spares_flow.py`。License：MIT。
