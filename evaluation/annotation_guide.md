# 标注指南（Annotation Guide）· v0.1 模板

> 供 ≥2 名教师独立标注使用。标注者不得是规则/提示词作者；对系统输出盲评。
> κ < 0.6 时先修订本指南，再重新标注。

## 标注对象

每例输出四元组：`kind` / `error_type` / `knowledge_point` / `broken_step`（断点定位）。

## kind（路由参考，单选）

| 值 | 定义 |
|---|---|
| `error` | 作答含错误推理，可定位断点 |
| `correct` | 作答数学上正确（含一题多解的等价路径） |
| `insufficient` | 信息不足/题目歧义/未作答，无法诊断 |
| `retake` | 图像不可辨认（parse 层处理） |

## error_type（仅 kind=error 必填；封闭集）

| 值 | 定义 | 判别要点 |
|---|---|---|
| `conceptual` | 对概念/规则的理解错误（如漏乘常数项、平方漏交叉项） | 学生**相信**自己用的规则是对的 |
| `procedural` | 过程/格式错误（如单位未换算、方程未配平的机械步骤） | 规则知道，执行顺序/格式错 |
| `careless` | 抄写/符号/计算疏漏，学生能力范围内可自查 | 前一步对了，本步笔误 |
| `unreadable` | 该步无法辨认 | 输入质量，非认知错误 |

**常见争议判例**（供仲裁）：
- 移项变号错误（`x-4=10 → x=10-4`）：概念性（等式性质理解错）优先于 careless——学生把"移项"当"交换"。**以指南修订为准，先标注后仲裁**。
- 一题多处错：标**首个**断点，`notes` 里记录其余。
- 一题多解的等价路径：kind=correct，不得因路径不同判 error。

## knowledge_point

从知识库 `knowledge_base/knowledge_points.json` 的封闭集选择；确实超出库范围时填 `null` 并在 notes 写明建议新增的知识点。

## broken_step

- 给出**恰好唯一匹配一步**的片段（`broken_step_contains`）或步号（`step_number`，1-based）。
- 断点定义：**首个**与题目约束/前序步骤不再一致的步骤；correct/insufficient 填 null。

## notes

自由文本：争议理由、题目质量问题、建议。仲裁时必填。
