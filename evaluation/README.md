# Evaluation 数据分层规则（诚实评测的物理边界）

> 对应 `ASSESSMENT_AND_ROADMAP.md` §6。**在引入任何新模型/组件前，本目录的分层必须先存在。**

## 目录契约

| 位置 | 角色 | 规则 |
|---|---|---|
| `../samples/` | **dev 集**（7 例） | 规则/提示词开发专用，**明标"仅开发、不得对外报告"**；同时被 UI 示例引用——这是已知泄漏通道 |
| `evaluation/classroom_challenge.json` | 对抗集（12 例） | 独立编写，探测泛化；作者指定参考，无独立教师校验 |
| `evaluation/held_out.json` | **冻结留出集** | ≥100 例真实课堂脱敏转录；**一旦冻结不得修改**，`held_out.hash` 由 CI 校验 |
| `evaluation/held_out.hash` | 冻结指纹 | `sha256(held_out.json)`；改动文件即 CI 失败 |
| `evaluation/labels/` | 教师标注 | 与题目输入物理分离；标注者不得是规则/提示词作者；盲评 |

## 硬性规则

1. **dev ∩ held_out = ∅**：`python scripts/check_data_isolation.py` 必须通过（文本归一化后逐例 hash，重叠即非零退出）。
2. **held_out 冻结**：`--freeze` 仅允许执行一次；之后文件 hash 不匹配即失败。
3. **KB 练习题文本不得出现在 held_out 题目里**（防 item leakage）——校验脚本同时检查 `knowledge_base/knowledge_points.json` 的题目文本。
4. **对外报告的每个数字**必须能由当前代码对 `held_out.json` 重生成，并附：provider、model snapshot、prompt hash、日期、数据 hash、n 与 95% CI。
5. 标注一致性 κ < 0.6 时，先修标注指南（`annotation_guide.md`）再评，不得直接报告模型指标。

## 标注流程（κ 研究前置）

1. 冻结 held_out（`--freeze`）。
2. ≥2 名教师按 `annotation_guide.md` 独立标注 → `labels/annotator_A.json`、`labels/annotator_B.json`。
3. 计算 Cohen's κ（错因分类）/ Krippendorff α（多类）；争议例仲裁后定稿 `labels/reference.json`。
4. 只有 κ ≥ 0.6 后，模型指标才允许对外引用。
