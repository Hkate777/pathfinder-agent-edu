# Pathfinder AI · 團隊交接說明（繁體）

> 更新日期：2026-11-19 ｜ 分支：`dev/local-run` ｜ 基線 `main` @ `01f8de8`
> 本文件面向共同開發的隊友：說明目前做到哪裡、如何接手、協作規範、已知風險。
> 專案原始說明見 `README.md`；本地執行環境見 `HANDOVER.md`；全面審評與路線圖見 `ASSESSMENT_AND_ROADMAP.md`。

---

## 1. 專案與現況（一句話）

Pathfinder AI 是「**診斷器而非解題器**」：學生拍錯題 → 定位首個錯誤步驟 → 錯因分類 → 對齊課綱知識點 → 分級練習 + 7 天複習；教師端看班級缺口並複核低信心案例。

**目前的真實狀態**（請以本節為準，勿引用過時數字）：

| 項目 | 現況 |
|---|---|
| 測試 | **66/66 通過**（`python -m unittest discover -s tests`） |
| 離線基線 | `samples` 知識點 6/6、`challenge` 知識點 8/8、路由 10/12 |
| 線上混合（hy4-flash-free + 本地語義檢索） | `samples` 知識點 6/6、斷步 6/6、路由 7/7；`challenge` 知識點 8/8、斷步 8/8、路由 11/12 |
| 中文題檢索 | **0/6 → 6/6**（本地語義模型） |
| 錯因分類 | **仍是弱項**：3/6、4/8（約 50%），且出現 false blame 1/3 |
| 真實學生資料 | **尚未接入**（合規前置未完成，見 §7） |

> 任何對外數字都必須標注：provider、混合模式、樣本量、日期、是否經教師校驗。目前 `challenge` 為作者自編（無獨立教師校驗），**不構成產品結論**。

---

## 2. 分支策略

```
origin/main            01f8de8（賽前基線，未包含本批工作）
  └── dev/local-run    efd98ea（本批 6 個 commit，已推送）
        ├── feature/<你的模組>   ← 隊友從這裡開分支
        └── ...
```

**規則**：
1. 隊友**不要**直接在 `dev/local-run` 上開發，改為 `git switch -c feature/<主題> dev/local-run`。
2. 合併走 PR → 至少 1 人審查 → 合回 `dev/local-run`。
3. `dev/local-run` 驗收通過（66 測試 + 三套評測 + 隔離門禁）後，才由負責人合併 `main`。
4. **不要 force push、不要改寫已推送的歷史**。

---

## 3. 快速開始（新機器複現）

```bash
git clone https://github.com/Hkate777/pathfinder-agent-edu.git
cd pathfinder-agent-edu
git switch dev/local-run

python -m pip install -r requirements.txt     # 含 sympy；sentence-transformers 為可選（見下）

cp .env.example .env                          # 填入你自己的 key，.env 永不提交
```

三種運行模式（按需選一）：

| 模式 | `.env` 關鍵設定 | 適用 |
|---|---|---|
| 純離線（零成本） | `PATHFINDER_OFFLINE=1` | 開發、跑測試、CI |
| 雲端診斷 + 本地語義檢索 | `PATHFINDER_OFFLINE=0`、`TEXT_MODEL`、`EMBEDDING_MODE=local-st` | 端點無 `/embeddings`（如 OpenCode Go） |
| 全在線 | `EMBEDDING_MODE=api`、`EMBEDDING_MODEL=...` | 端點同時提供 chat 與 embeddings |

驗證命令（提交前必跑）：

```bash
python -m unittest discover -s tests          # 66/66
python eval.py --set samples                  # 離線基線
python eval.py --set challenge                # 泛化信號
python scripts/check_data_isolation.py        # 資料隔離門禁（held_out 建立後生效）
python scripts/check_provider.py              # 雲端連通性自檢（三槽位）
```

---

## 4. 本批 6 個 Commit 做了什麼（`origin/main..dev/local-run`）

| Commit | 內容 |
|---|---|
| `c1cf220` | **階段 0 止血**：4 個 UI P0（照片失敗死局、切換學生串資料、無法連續做題、未映射複核死指令）；新增 12 例獨立 `challenge` 集；服務端冪等 |
| `8520d5b` | 文件同步：外部方案 v1.1 對齊（HKEAA/EDB 紅線、元件採納決策） |
| `ed4614b` | **階段 1 首批**：SymPy 步驟判分 `verifier.py`、評測資料分層腳手架、資料隔離門禁、GSM8K ingest、`configured` fail-fast |
| `382aca8` | **接入 OpenCode Go**（hy4-flash-free）：混合 Provider、自訂 header、推理模型相容 |
| `350fa37` | UI 標籤：`線上模型 · 本地檢索` |
| `efd98ea` | **本地語義檢索**（paraphrase-multilingual-MiniLM-L12-v2）：兩階段打分、matchers 精度修復、hint 路由化 |

---

## 5. 接手必讀：五個關鍵設計決策

1. **誠實失敗不可退讓**：parse 非 ok 直接棄答；低於門檻標 `unmapped` 不猜；斷點必須能在原文中引用；低信心轉教師複核。任何改動都不得繞過這四條。
2. **Embedding 三模式**：
   - `api`：呼叫 `/embeddings`（純 cosine）
   - `local`：確定性哈希詞袋（**無中文能力**，僅保證離線可復現）
   - `local-st`：Sentence Transformers 本地語義（384 維，CPU，**中文可用**）
   - `PATHFINDER_OFFLINE=1` 永遠固定哈希 → 保證歷史評測可比。
3. **hint 路由化**：哈希路徑保留規則 hint（同源、是證據）；**語義路徑必須丟棄 hint**——實測模型的錯誤 hint 會把單位換算題（PHY-9.2）帶偏到牛二（PHY-9.1）。
4. **matchers 只放結構性/學科特異模式**：`solve`、`simplify`、`force`、`kg`、`is|are` 這類泛化詞已移除——它們讓多個節點同時滿分，把勝負交給噪聲。
5. **兩階段 grounding**：有 matcher 命中 → matcher 主導（0.75×ratio+0.25×sim）；全域無命中 → 純語義兜底（中文題靠這條）。

---

## 6. 協作分工建議

因專案由多人共同開發，建議按**檔案邊界**分工，降低衝突：

| 模組 | 檔案 | 負責範圍 |
|---|---|---|
| 診斷管線 | `diagnose.py`、`llm.py`、`grounding.py` | prompt、Provider、grounding、評分口徑 |
| 判分與排程 | `verifier.py`、`schedule.py` | CAS 規則、複習間隔 |
| 學生/教師 UI | `app.py`、`ui.py`、`ui.css` | 流程、元件、文案 |
| 資料與評測 | `store.py`、`eval.py`、`evaluation/`、`scripts/` | schema、指標、資料分層、門禁 |
| 知識庫 | `knowledge_base/*.json` | 知識點、練習、matchers（改動需跑三套評測） |

**提交規範**：
- Commit 用英文、祈使句、說明「為什麼」而非只說「改了什麼」。
- **禁止提交**：`.env`、`data/`、任何學生真實資料。
- 改 `knowledge_base/matchers.json` 或 `grounding.py` → 必須附 `eval --set samples` 與 `--set challenge` 的前後對照。
- 改 `store.py` schema → 必須提供遷移（`init_db` 的新表 + 舊庫相容），並補回歸測試。
- 不要修改 `evaluation/held_out.hash` 與已凍結的 `held_out.json`。

**PR 檢查清單**：
- [ ] `python -m unittest discover -s tests` 全綠
- [ ] 三套評測（samples / parsing / challenge）可跑且數字已附
- [ ] `python scripts/check_data_isolation.py` 通過
- [ ] 無新增密鑰、無 `.env`、無絕對路徑
- [ ] 若影響數字，README/交接文件的數字同步更新

---

## 7. 已知風險與限制（如實）

1. **錯因分類仍弱**：真實模型約 50%，離線規則 75% 是「照樣本寫的規則」的過擬合結果，不可作為模型能力。
2. **False blame**：對「資訊不足」案例仍會錯誤指認步驟（1/3）。
3. **樣本量小**：7 開發樣本 + 12 挑戰用例，無獨立教師校驗、無 κ；所有數字只能視為信號。
4. **未做身份與合規**：目前無登入/RBAC、無家長同意門、無刪除權、無審計日誌（審評 P0-11 全項待辦）。
5. **雲端 = 資料出境**：OpenCode Go / 任何雲端 chat 均涉及跨境；**真實學生資料接入前必須完成 DPA + 出境評估**。
6. **HKEAA 紅線**：DSE 歷年題、marking scheme、評估框架**禁止 AI/ML 使用**；本地化只能走公開授權資料集 + 自建題 + CAIE 國際線。
7. **`local-st` 首載成本**：模型首次載入約 2–4 秒（已懶加載）；無 GPU 亦可，但並發需評估。

---

## 8. 待辦（按優先級）

**P0（合規/阻斷）**
- [ ] 登入 + RBAC（學生只见自己、教師按班級授權）
- [ ] 家長同意門 + PII 遮蔽 + 照片 TTL/刪除權 + 審計日誌
- [ ] `correct` 狀態：正確答案不得再被記為錯題、不得污染熱力圖

**P1（能力/可信度）**
- [ ] 填充 `held_out.json`（≥100 例）+ ≥2 位教師雙標注 + κ
- [ ] 模型橫向對比（`glm-5.3-flash` / `qwen3.8-max` / `deepseek-v4-pro`）
- [ ] 錯因分類與棄答能力專項改進（few-shot / schema 強校驗）
- [ ] 教師佇列批次化、班級名冊匯入

**P2（產品化）**
- [ ] 自適應排程（SM-2/FSRS 輕量版）、KC 粒度知識點
- [ ] DSE 繁中/英文雙語、OpenCC 繁簡歸一
- [ ] 容器化 + CI + 資料庫遷移 + 備份 + 監控

---

## 9. 檔案地圖（接手先看這些）

```
diagnose.py          核心管線：parse → diagnose → ground → prescribe → schedule → store
llm.py               Provider 抽象：OpenAICompat / Stub(離線) / Hybrid(混合) + 本地 ST Embedder
grounding.py         知識點檢索：matchers + embedding 兩階段打分
verifier.py          SymPy 步驟級判分（pass/fail/unknown，絕不猜）
schedule.py          7 天複習排程
store.py             SQLite：錯題、複習、教師複核、KP override、submissions 冪等
eval.py              三套評測（samples / parsing / challenge）
app.py, ui.py        學生端 + 教師端（Streamlit）
knowledge_base/      10 知識點 + 30 練習 + matchers（改動需回歸）
evaluation/          資料分層契約、標注指南、held_out 模板、challenge 集
scripts/             check_provider.py / check_data_isolation.py / ingest_datasets.py
tests/               66 項測試（含 AppTest、verifier、isolation、embeddings）
traces/              執行與評測存檔（可重生成）
```

---

## 10. 環境變數速查（無密鑰）

| 變數 | 說明 |
|---|---|
| `PATHFINDER_OFFLINE` | `1` = 純離線基線（固定哈希） |
| `LLM_BASE_URL` / `LLM_API_KEY` | OpenAI 相容端點與密鑰（僅本地 `.env`） |
| `TEXT_MODEL` / `VISION_MODEL` | 診斷 / 照片轉錄模型 |
| `EMBEDDING_MODE` | `api` / `local` / `local-st` |
| `EMBEDDING_MODEL` | `api` 模式下的 embedding 模型 |
| `LOCAL_EMBEDDING_MODEL` | `local-st` 模式下的本地模型（預設 `paraphrase-multilingual-MiniLM-L12-v2`） |
| `LLM_EXTRA_HEADERS` | 需要自訂 header 的閘道（JSON，例如 `x-opencode-session`） |
| `GROUNDING_THRESHOLD` / `CONFIDENCE_REVIEW` / `MAX_PRACTICE_PER_DAY` | 管線參數 |
| `DB_PATH` | SQLite 路徑（預設 `data/pathfinder.db`） |

> 密鑰只存在本機 `.env`（已 gitignore）。若懷疑洩漏，立即換 key，**不要只刪 commit**。

---

## 11. 接手第一天建議

1. `git switch dev/local-run` → 跑 `python -m unittest discover -s tests`（應 66/66）。
2. 跑三套評測，記錄你機器上的基線數字。
3. 讀 `ASSESSMENT_AND_ROADMAP.md` §4（缺陷全景）與 §7（路線圖），挑一個 P0 或 P1 開 feature 分支。
4. 任何數字變動都更新 README 與本文件的對應表格，並在 PR 描述附前後對照。

> 本專案的核心資產是「**誠實**」：寧可說不知道，也不要編一個看起來合理的答案。改動時請守住這條線。
