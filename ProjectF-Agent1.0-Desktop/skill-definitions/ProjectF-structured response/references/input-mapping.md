# 输入裁剪与字段映射

仅在调用方提供原始分析 JSON、字段不一致或缺少 `source_type` 时读取本文件。

## 上下文数据包

服务端优先传入以下精简数据，避免把前端渲染字段注入模型：

| 数据 | 保留 | 去除或限制 |
|---|---|---|
| 当前分析 | `ai_summary` 全文、异常项完整、正常项标题、`health_suggestions` 全文 | `media.url`、`thumbnail_url`、`avatar`、`duration` 等渲染字段 |
| 宠物档案 | 名字、品种、年龄、体重、性别 | 头像、创建时间 |
| 历史记录 | 最近 3 份的日期、分类、`ai_summary` | 历史完整维度数组 |
| 对话历史 | 最近 5 轮，最多 10 条消息 | 更早消息 |

目标：当前分析约 800–1500 tokens，总输入不超过 8K tokens。裁剪是服务端标准 JSON/数据库操作，不要求模型调用工具。

## `source_type` 推断

优先使用显式 `source_type`。缺失时按以下顺序推断：

1. `report_meta.category` 为 `dental`、`gait`、`behavior`、`stool`、`xray` → `home_check`。
2. 存在 `indicators`，或 `report_meta.report_type` 为血常规、生化、尿检、影像报告等 → `report`。
3. 存在 `dimensions` 且维度项包含 `status_label`、`ai_analysis`、`suggestion` → `home_check`。
4. 仍无法判断时，不猜测类型；在回答中说明资料结构不完整，并只处理能够可靠读取的内容。

## 报告检测映射

兼容当前工作区和文档中的两种字段名称：

- 摘要：`analysis_json.ai_summary`。
- 指标数组：优先 `analysis_json.indicators`，若不存在则使用 `analysis_json.dimensions`。
- 指标名称：`full_display` → `title` → `name`。
- 检测值：`ui_label` → `value`；参考范围：`ref_range`。
- 状态：`status`；缺失时由 `ui_color` 映射：`Red/red` 为异常，`Yellow/yellow/orange` 为临界或需关注，`Green/green` 为正常。
- 解释与建议：`deviation`、`popular_science`、`item_advice`。
- 总体建议：`analysis_json.health_suggestions`。

异常排序优先级：显著异常/危急/红色 > 偏高或偏低/异常 > 临界/黄色/橙色 > 正常/绿色。同级保持原数组顺序。

## 居家检测映射

- 摘要：`analysis_json.ai_summary`。
- 维度数组：`analysis_json.dimensions`。
- 维度名称：`title`。
- 状态：`status_label`。
- 观察：`ai_analysis`。
- 维度建议：`suggestion`。
- 总体建议：`analysis_json.health_suggestions`。

状态排序优先级：`red`/异常/建议就医/重度 > `orange`/需关注/中度 > `blue`/轻微 > `green`/正常/良好/未见异常。同级保持原数组顺序。

## 缺失和冲突

- 以结构化字段为主，不从文件名或 URL 猜测医学结论。
- `ai_summary` 与明细冲突时，指出“摘要与明细存在不一致”，引用明细中的确切值，并建议人工复核原始报告。
- 缺少数值、单位、参考范围或关键观察时，不补造；用“当前分析未提供该信息”表达。
- 历史摘要只有日期和结论时，只描述趋势方向，不做数值比较。
