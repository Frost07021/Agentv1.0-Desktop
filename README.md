# Project F AI 宠物管家 Agent

基于 Project F 需求、架构和多模态 Demo 演进而来的完整本地客户端。它不是开放式通用 Agent，而是一个受控的宠物领域 Agent：宠物管家负责流式多轮对话，“报告检测”和“居家检测”是两个一级产品功能，Skill 被固化在功能内部，用户不需要也不能手动选择 Skill。

## 已完成

- 宠物管家 Conversation / Message / Run / Event 数据模型；
- 本机持久化对话历史：自动过滤空会话，支持全部/当前宠物筛选、详情、继续对话和删除；
- 宠物档案上下文、最近 12 条消息和较早会话摘要；
- SSE 事件流：`token.delta`、`message.completed`、Run 状态事件；
- Fake 与 OpenAI 兼容的真实流式对话模型；
- 急症信号优先就医引导，以及专业检测入口隔离；
- `client_message_id` 会话内幂等；
- 固定 Route Registry，APP 不直接指定任意 Skill；
- 与手机版 APP 同产品体系的电脑端双栏工作台：健康功能入口位于主动关怀上方，右侧保留独立长对话区；
- 沿用手机版 FURA 蓝、薄荷青、粉、橙视觉规范，并针对 1440px 宽屏重新排版；
- 报告检测内置 `pet-report-analysis`，支持图片与多页 PDF 逐页识别；
- 居家检测内置牙科、便便、步态、行为、X 光五个独立 Skill；
- 图片/视频/PDF 上传、拍摄指南、处理中状态和结构化结果弹层；
- 与手机端一致的“智能压缩/原始质量”上传策略：视频 5–15 秒，智能模式源文件上限 100MB 并压缩至 50MB 内，原始模式超限明确拦截；
- 检测结果可一键带回宠物管家继续追问；
- 检测结果生成会话级 `result_id`，继续追问会执行 `structured-response` Skill，并以五段式结构、蓝色关键词和推荐问题流式呈现；
- 加载项目一期 7 个 Skill 定义，并以 6 条固定产品路由承载检测能力；
- 图片/视频/PDF 校验、Qwen 原生完整视频理解、结构化结果校验与 JSON 落盘；
- 五类居家检测插件由 `config/home-check-plugins.yaml` 显式配置，视觉 Prompt 直接读取各 Skill 第三节原文；
- 步态与行为严格执行“整段视频理解 → 2–3 个证据关键帧复核 → 结构化汇总”；完整视频单次等待窗口为 150 秒，传输、读取超时或响应异常都会自动重试一次，两次均失败后使用覆盖全时段的 12 帧顺序分析并显式标记降级质量；
- 每次检测失败都会保存步骤级诊断记录，明确标注媒体、模型、关键帧或结构化校验失败阶段；
- 报告检测与 X 光均支持图片和 PDF，PDF 使用 `pdftoppm -jpeg -r 200` 分页后按顺序分析；
- 普通对话、分析结果、结构化追问均执行单个文本字段最多 150 字的前后端双重约束；
- 53 项自动化测试，并完成真实 X 光分析、真实普通对话、真实结构化追问及同视频步态回归。

## 本地运行

要求 Python 3.12、3.13 或 3.14。

可直接双击 `启动电脑端.bat`。首次运行会自动检测受支持的 Python、创建 `.venv` 并安装依赖，因此需要能够访问 Python 包源；后续运行会直接复用环境。脚本优先使用 PowerShell 7，并兼容 Windows PowerShell 5.1。它会替换旧版本服务、检查健康状态后打开独立电脑端窗口。若启动失败，窗口会保留错误提示，详细日志位于 `.runtime/server-error.log`。

```powershell
cd "ProjectF-Agent1.0-Desktop"
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app serve --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000` 使用客户端；`http://127.0.0.1:8000/docs` 用于查看 OpenAPI。

### 1. 创建宠物管家会话

```powershell
$conversation = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/v1/conversations" `
  -ContentType "application/json" `
  -Body (@{
    user_id = "user_001"
    pet = @{
      pet_id = "pet_001"
      pet_name = "警长"
      species = "cat"
      breed = "英短"
      age_years = 4
    }
    mode = "fake"
  } | ConvertTo-Json -Depth 5)
```

### 2. 发送消息

```powershell
$accepted = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/v1/conversations/$($conversation.conversation_id)/messages" `
  -ContentType "application/json" `
  -Body (@{
    text = "警长最近走路一瘸一拐，我应该先观察什么？"
    client_message_id = "app_msg_001"
  } | ConvertTo-Json)
```

### 3. 订阅 SSE

```powershell
curl.exe -N "http://127.0.0.1:8000/v1/conversations/$($conversation.conversation_id)/events?after_sequence=0"
```

也可以读取最终消息和 Run：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/v1/conversations/$($conversation.conversation_id)/messages"
Invoke-RestMethod "http://127.0.0.1:8000/v1/runs/$($accepted.run_id)"
```

### 4. 固定分类报告分析

```powershell
$body = @{
  media_path = "C:\absolute\path\to\report.jpg"
  pet = @{ pet_id = "pet_001"; pet_name = "警长" }
  mode = "fake"
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/v1/analysis/report/general/tasks" `
  -ContentType "application/json" `
  -Body $body
```

步态视频使用 `/v1/analysis/home-check/gait/tasks`。旧的 `/v1/agent/tasks` 和 `/v1/agent/tasks/upload` 暂时保留为迁移兼容接口。

## 使用真实模型

不要把 Key 写进仓库。启动前设置环境变量：

```powershell
$env:AGENT_MODEL_BASE_URL = "https://your-provider.example/v1"
$env:AGENT_MODEL_API_KEY = "your-key"
$env:AGENT_MODEL_NAME = "your-model"
```

创建会话或分析任务时将 `mode` 改为 `real`。模型服务需兼容 OpenAI `chat/completions`；宠物管家使用流式响应，多模态检测使用 JSON 结构化响应。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前结果：`71 passed`。覆盖客户端入口、对话历史持久化/筛选/删除、智能与原始上传异常流、普通流式聊天与近期检测上下文、报告图片及多页 PDF 检测、三类图片检测、两类视频检测、各居家 Skill 的状态颜色映射、检测报告程度标签及颜色渲染、结构化纠偏后的机械契约稳定、失败结果弹窗与原文件重试、视频时序证据稳定、步骤级失败诊断、具体观察证据、去示例化 Skill Prompt、视频三步流水线、原生视频读取超时自动重试、密集顺序帧降级、降级质量标记、X 光图片/PDF 分步汇总，以及检测结果结构化追问、完整正常项证据保留、修复轮原始输入保留、五段/高亮校验和跨会话结果隔离。常规自动化测试使用 Fake 模型，不访问网络，也不作真实医学判断。

### 检测结果继续追问

报告检测和居家检测会返回 `result_id`。客户端发送追问时把它写入 `reply_to_result_id`，服务端读取完整分析结果并执行 `structured-response` Skill：

```text
result_id → 规范化并裁剪 analysis_json → structured-response Skill
          → JSON Schema + 五段标题 + highlights 双向校验
          → structured.segment SSE → 客户端分段渲染
```

真实模型候选首次校验失败时会自动要求模型修正一次；修复请求会继续携带原始图片、PDF 页面或视频关键帧及工作流证据，避免脱离素材补写。再次失败则终止该 Run，不向客户端展示不符合契约的内容。普通宠物管家聊天继续使用 `token.delta`，结构化追问使用 `structured.segment` 与 `structured.suggested_questions`。

### 视频理解插件

本机没有 OpenClaw `tools.media.video` 运行时，因此当前以阿里云 OpenAI 兼容接口原生支持的 `video_url` 作为等价视频理解插件。执行顺序与 Skill 完全对应：

```text
Step 1：MP4 → 原生时序理解 → 异常/典型信号时间点
Step 2：FFmpeg 按时间点提取 2–3 帧 → Qwen 图像插件逐张复核（失败逐张重试）
Step 3：视频描述 + 关键帧证据 → Qwen JSON 汇总 → Schema 校验
  └─ Step 1 原生请求失败时：使用 FFmpeg 提取覆盖全片的 12 帧，按时间顺序完成降级视频理解
```

插件配置位于 `config/home-check-plugins.yaml`：

```yaml
home-health-check-gait:
  video_fps: 6.0
  workflow: [video_understanding, evidence_frame_extractor, image_understanding, result_composer]
home-health-check-behavior:
  video_fps: 4.0
  workflow: [video_understanding, evidence_frame_extractor, image_understanding, result_composer]
```

所有类别的 `prompt_source` 固定为 `skill.visual_recognition_prompt`，程序会从 Skill 的“视觉识别 Prompt 指令”代码块读取原文，不在代码里维护第二份 Prompt。每份真实视频结果的 `report_meta.analysis_runtime` 会记录完整/降级质量、FPS、关键帧时间点、降级帧数和已完成的 Skill steps；底层服务身份不会写入对外结果。

## 目录

```text
ProjectF-Agent1.0-Desktop/
├── app/
│   ├── api.py               # Conversation、SSE、Run、检测 API
│   ├── chat_runtime.py      # 宠物管家执行与事件输出
│   ├── chat_adapter.py      # Fake/真实流式对话适配器
│   ├── state.py             # 单机状态仓库与 JSON 历史持久化
│   ├── route_registry.py    # 产品分类到 Skill 的固定路由
│   ├── harness.py           # 多模态分析执行入口
│   ├── home_check_workflow.py # Skill 对齐的居家检测插件流水线
│   ├── media.py             # 图片/视频/PDF 预处理
│   └── model_adapter.py     # Fake/真实视觉模型适配器
├── config/                  # 居家检测插件配置
├── static/                  # 成熟客户端 HTML、CSS 与交互逻辑
├── skill-definitions/       # 项目一期 7 个 Skill
├── tests/
├── runtime/                 # 本机历史、临时上传、视频帧（被忽略）
└── output/                  # 分析结果（被忽略）
```

## MVP 边界

会话、消息和分析结果会写入 `runtime/desktop-history.json`，重启电脑端后仍可读取；运行中的 SSE 事件和 Run 状态仍只保存在单进程内存。媒体保存在本机运行目录；RAG、对象存储、Redis Streams、PostgreSQL、鉴权、限流和多 Worker 尚未接入。Fake 回复只用于产品与接口联调，不代表兽医诊断。生产化应按架构文档后续阶段替换本机 JSON 仓库、媒体服务与知识检索。

## 文档与 GitHub

- 产品和架构设计：`docs/PROJECTF-AGENT-DESIGN.md`
- 当前实现说明：`docs/PROJECTF-AGENT-IMPLEMENTATION.md`
- 安全说明：`SECURITY.md`

首次上传 GitHub：

```powershell
git init -b main
git add .
git commit -m "Initial Project F desktop agent"
git remote add origin <your-github-repository-url>
git push -u origin main
```

真实模型配置只允许写入本地 `.env` 或部署 Secret，禁止提交。GitHub Actions 会在 Windows 和 Python 3.12 环境运行自动化测试。
