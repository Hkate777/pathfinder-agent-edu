# 交接文档 · Pathfinder AI 本地运行环境

> 生成日期：2026-11-19
> 仓库：https://github.com/Hkate777/pathfinder-agent-edu
> 本地路径：`C:\Users\25345\.zcode\workspace\draft-89dbc161-e494-41a3-96a6-388d540e5b22\pathfinder-agent-edu`

---

## 0A. 阶段 1 首批落地记录（2026-11-19，「方案执行落地」）

依据 `ADOPTION_NOTES.md` 采纳决策 + 方案 v1.1，Provider 路线**决策为云 API 优先**（跨境 DPA 义务见 `.env.example`；Ollama 为合规升级路径）。本批交付（62/62 测试通过）：

| # | 交付 | 文件 | 验证 |
|---|---|---|---|
| 1 | **SymPy 步骤级判分**（verdict: pass/fail/**unknown 不猜测**；同解验证/恒等检查/Unicode 归一/散文词剔除/无约束场景诚实化） | `verifier.py` + CLI | 教师标注对照：4 个代数样本断点**全中**；che/phy 按声明范围 unknown |
| 2 | **评测数据分层脚手架**（dev/challenge/held-out 契约、冻结指纹、标注指南 κ 模板） | `evaluation/README.md`、`held_out.template.json`、`annotation_guide.md` | — |
| 3 | **数据隔离门禁**（dev∩held_out=∅ + KB 泄漏检查 + 冻结保护，可作 CI 门禁） | `scripts/check_data_isolation.py` | 5 项测试含篡改检测 |
| 4 | **数据集 ingest**（GSM8K→项目格式；许可强制归档；默认 dry-run；`knowledge_point=null` 待标注语义） | `scripts/ingest_datasets.py` | 冒烟：2 可用/1 跳过 |
| 5 | **Config fail-fast 修复**：在线模式 `configured` 必须含 `embedding_model`（审评 P0-10，防已计费后 grounding 崩） | `llm.py` | 现有测试兼容 |
| 6 | 依赖与配置记录：`requirements.txt` + sympy；`.env.example` 记录云 API 决策与 DPA 义务 | 配置 | — |

**未做（待资源/授权）**：held_out 实例填充与教师 κ 研究、bge-m3/PaddleOCR 接入、云 API 真实模型评测、RBAC/同意门。gate 在 `held_out.json` 创建前为 inactive（exit 0），创建后自动生效。

---

## 0. 阶段 0 执行记录（2026-11-19 下午）

依据 `ASSESSMENT_AND_ROADMAP.md` 完成阶段 0 止血，代码改动集中在 4 个 UI P0 + 1 个评测缺口：

| # | 修复 | 改动文件 | 回归 |
|---|---|---|---|
| 8 | transcript 按 `(student_id, digest)` 隔离 + 失败可重拍；切学生清空临时状态 | `app.py` | `test_app_p0.py::test_c` |
| 9 | 提交按钮常驻 + 「诊断下一题」；`submissions` 表按内容 hash 幂等 | `app.py`、`diagnose.py`、`store.py` | `test_app_p0.py::test_a/test_b`、`test_workflows.py` |
| 10 | `teacher_kp_override` + 复核指定 KP；`effective_kp` 贯通画像/热力图/排程 | `app.py`、`diagnose.py`、`store.py` | `test_app_p0.py::test_d`、`test_workflows.py` |
| 11 | 补 12 例独立挑战集 `--set challenge`；三套评测全可跑 | `evaluation/classroom_challenge.json` | `eval.py --set challenge` |

**验证**：44/44 测试通过；三套评测全跑通（challenge 暴露泛化差距：错误类型 6/8=75%、知识点 7/8=88%、路由 10/12=83%）。文档数字统一为 44 测试 / 7 trace / 6+ 评测口径。改动全部提交在 `dev/local-run`，未推送远端。

---

## 1. 项目概述

**Pathfinder AI** 是面向 Cyberport 香港 AI Agent Challenge（教育文创赛道）的教育类 AI Agent。
学生拍照提交错题后，系统诊断出错的推理步骤（而非只判答案）、分类错误类型（概念性/程序性/粗心/图像不可读）、
grounding 到课纲知识点、推送分级练习并生成 7 天间隔复习计划；教师端提供班级知识缺口热力图。

核心设计原则为 **"诚实失败"**：不可读照片要求重拍、知识点匹配不上不猜测、低置信度结果转教师复核。

## 2. 仓库与分支状态

| 项目 | 状态 |
|---|---|
| 远程仓库 | `origin` → https://github.com/Hkate777/pathfinder-agent-edu.git |
| 上游最新提交 | `01f8de8` Align docs with Cyberport AI Agent Challenge (education track) and add proposal PPT |
| 上游提交数 | 2（`4e84590` 初始提交 → `01f8de8` 文档对齐） |
| **当前工作分支** | **`dev/local-run`**（基于 `main` @ `01f8de8` 创建） |
| 工作区 | **有大量未提交改动**（见下方说明） |
| 未跟踪产物 | `ui.py`、`ui.css`、`.streamlit/`、`tests/test_app.py`、`tests/test_workflows.py`、`HANDOVER.md`、2 份 trace（详见 `git status`） |

> ⚠️ **并行编辑说明（2026-11-19 12:45–13:09+，可能仍在进行）**：本会话克隆仓库并启动服务后，另一会话/开发者在同一工作区持续大改（最新累计 **11 个文件 +1163/-396 行**）。主要改动：UI 重构（`ui.py`/`ui.css`/`.streamlit/`）、新增 15 个测试（`test_app.py`/`test_workflows.py`）、修复 `utcnow` 弃用（`store.utc_now()`）、`store.py` 增加教师复核等、**`eval.py` 重写为三套评测**（`--set samples`/`parsing`/`challenge`，输出更细的指标：路由、引用步骤、错误归因）。交接文档初始版本描述的是克隆时基线；§4 验证结果已按改动后代码复核更新。接手时请先 `git status` + `git diff` 确认最新状态。

## 3. 本地环境

| 项 | 值 |
|---|---|
| 操作系统 | Windows 10.0.26200 x64（Git Bash） |
| Python | 3.14.3（`C:\Users\25345\AppData\Local\Programs\Python\Python314\python.exe`） |
| 依赖安装方式 | `python -m pip install -r requirements.txt` |
| 依赖清单 | streamlit≥1.30、python-dotenv≥1.0、requests≥2.31、pillow≥10.0 |
| 配置文件 | `.env`（自 `.env.example` 复制，**已被 gitignore**） |
| 关键配置 | `PATHFINDER_OFFLINE=1` → 使用离线规则基线（StubProvider），**无需 API Key** |
| 数据库 | `data/pathfinder.db`（SQLite，运行时自动创建，gitignored） |

> ⚠️ 切换在线模式：编辑 `.env`，将 `PATHFINDER_OFFLINE` 改回 `0` 并填入 `LLM_API_KEY`（默认模型 gpt-4o-mini + text-embedding-3-small，可换任意 OpenAI 兼容端点）。

## 4. 验证结果（2026-11-19 本地实测）

| 检查项 | 命令 | 结果 |
|---|---|---|
| 单元测试（基线代码） | `python -m unittest discover -s tests` | ✅ 23/23 通过（12:35，克隆时状态） |
| 单元测试（**并行改动后，最新复核**） | 同上 | ✅ **38/38 通过**（13:15 复核，含新增 `test_app.py`/`test_workflows.py` 共 15 个测试，无弃用警告） |
| 诊断 CLI | `python diagnose.py --sample samples/alg_distributive.json --trace` | ✅ 断点 `2x + 3 = 4x - 6`，错误类型 conceptual，知识点 ALG-7.3（score 0.8621），置信度 0.9，复习计划 day1/3/7（12:35，基线状态） |
| 样本集评测（基线格式） | `python eval.py --set samples --save` | ✅ 7 样本：错误类型一致率 100%、知识点准确率 100%、断步定位 6/7（12:35，基线状态） |
| 样本集评测（**重写后新格式**） | `python eval.py --set samples` | ✅ 路由 7/7（6 诊断 + 1 重拍）、引用步骤错误类型 6/6、知识点匹配 6/6、断步定位 6/6、错误归因步骤 0/6（13:15 复核） |
| 模块导入 | `python -c "import app, eval, store, diagnose, ui"` | ✅ 全部导入正常（13:15） |
| Streamlit UI | `python -m streamlit run app.py --server.headless true --server.port 8501` | ✅ health/page 均 HTTP 200，服务端日志无报错（13:16 以最新代码重启后复核） |

评测报告存档：`traces/eval_20261119_043519.md`、`traces/20261119_043501_alg_distributive.md`（基线状态生成）

## 5. 运行手册（接手后快速上手）

```bash
cd pathfinder-agent-edu

# 1) 安装依赖（首次）
python -m pip install -r requirements.txt

# 2) 配置（首次；离线模式已就绪）
cp .env.example .env        # Windows Git Bash: cp 可用

# 3) 跑测试
python -m unittest discover -s tests

# 4) 单样本诊断（CLI）
python diagnose.py --sample samples/alg_distributive.json --trace

# 5) 样本集评测
python eval.py --set samples --save

# 6) 启动 Web UI（学生/教师双端）
python -m streamlit run app.py          # 默认 http://localhost:8501
```

常用样本（`samples/`）：`alg_distributive` / `alg_fraction_add` / `alg_sign_drop` / `alg_square_of_sum` / `che_unbalanced` / `phy_unit_conversion` / `unreadable_photo`

知识库：`knowledge_base/knowledge_points.json`（10 个课纲知识点，覆盖数/理/化/英）+ `matchers.json` + 30 道分级练习。

## 6. 已知问题与注意事项

1. ~~**`datetime.utcnow()` 弃用警告**~~ ✅ **已修复**（改用 `store.utc_now()` → `datetime.now(timezone.utc)`）。
2. ~~**`--set challenge` 缺数据文件**~~ ✅ **已补齐**（阶段 0 新增 `evaluation/classroom_challenge.json`，12 例独立挑战用例，三套评测全部可跑）。
3. **当前指标来自离线规则基线**，非真实 LLM。`samples` 套件 6/6 是规则同源自证；`challenge` 套件（错误类型 75%、知识点 88%、路由 83%）才是更诚实的泛化信号，且该套件无独立教师校验。接入真实 API Key 后须重新评测并替换全部数字。
4. **三份 eval 报告 md5 相同**：`traces/eval_20261031_145842.md`、`eval_20261031_151738.md`、`eval_20261119_043519.md` 内容一致（旧格式归档）。阶段 0 未删除任何历史 trace——删除已提交/归档的运行证据风险大于收益。如需清理，先确认哪份是权威版本。
5. **Streamlit 热重载瞬态报错**：并行编辑期间浏览器可能撞上编辑中途文件状态（13:00 曾出现 `ImportError: utc_now`，重启即消失）。编辑活跃期遇报错，先看文件 mtime。
6. **`.env` 含密钥时严禁提交**（已被 gitignore，保持现状即可）。
7. **阶段 1 未做**：身份鉴权/RBAC、同意门与 PII 遮蔽、删除权、教师队列批量化等 P0 仍待办，详见 `ASSESSMENT_AND_ROADMAP.md` §7。

## 7. 建议的后续工作

- [x] ~~修复 `utcnow` 弃用警告~~
- [x] ~~补齐 `evaluation/classroom_challenge.json` 并跑通三套评测~~（阶段 0 完成）
- [x] ~~阶段 0 提交对齐（全部改动入库 `dev/local-run`）~~
- [ ] **阶段 1**：`correct` 状态、真实作答与判分、最小 RBAC、同意门/PII 遮蔽/删除权、教师队列批量化、诊断输出 schema 强校验（详见审评文档 §7）
- [ ] 接入真实 API 端点后重跑评测，替换 README/PPT 全部基线数字
- [ ] 决定重复 eval trace 是否清理历史归档
- [ ] 接入真实 API（OpenAI 或兼容端点）验证在线模式全链路
- [ ] 按 README 路线图推进：学校试点（200 人）→ 香港 DSE 本地化
- [ ] 参赛材料：`Pathfinder_AI_Idea_Pitch.pptx`、`proposal/`、`REVIEW.md`、`AGENTS.md`
- [ ] **全面审评与提升规划**：见 `ASSESSMENT_AND_ROADMAP.md`（2026-11-19，四维度只读审评，含 12 个 P0 缺陷、评测可信度专项、四阶段路线图与 10 个可立即开工的 PR）。落地前先按该文档 §7 前置条件冻结分支。

## 8. 关键文件速查

```
app.py           Streamlit 学生/教师 UI
diagnose.py      核心诊断管线（CLI 入口：--sample / --trace）
vision.py        图像转录（OCR/vision 模型层）
llm.py           模型层：可插拔 Provider + 离线规则 StubProvider
grounding.py     知识点 embedding 相似度检索（阈值 0.42）
schedule.py      7 天间隔复习计划
store.py         SQLite 存储（data/pathfinder.db）
eval.py          三套评测（--set samples / parsing / challenge）
tests/           44 个离线单元与回归测试（含 4 个 AppTest P0 回归）
samples/         7 个开发样本（规则据此编写）
evaluation/      12 个独立挑战用例（探测泛化差距）
knowledge_base/  10 知识点 + 30 分级练习
traces/          运行/评测存档（.md 报告）
.env.example     配置模板（含阈值：GROUNDING_THRESHOLD=0.42 / CONFIDENCE_REVIEW=0.60 / MAX_PRACTICE_PER_DAY=3）
```
