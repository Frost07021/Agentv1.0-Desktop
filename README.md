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
- 步态与行为严格执行“整段视频理解 → 2–3 个证据关键帧复核 → 一次结构化汇总”；未超硬像素预算的步态只运行一次Qwen原生视频Step 1，SSE仅用于耗时遥测，不再触发或选择密集帧；原生明确失败后直接返回结构化原因，不从头重跑帧链路；首轮原生结论正常时只增加一次不携带首轮JSON的原视频盲复核，独立上限90秒；
- 同一次检测的两份 Step 1 若出现正常/异常冲突，最终结果固定为橙色待确认，并在总述开头说明素材限制和管家已结合两次视频分析及关键帧尽力给出建议；字段无法判断时保留完整结果并给出原因与复测方法；
- 每次检测失败都会保存步骤级诊断记录，客户端同步显示失败阶段、用户可理解的原因与下一步建议；
- 报告检测与 X 光均支持图片和 PDF，PDF 默认使用随 Python 依赖安装的 PDFium 以200 DPI分页，系统已有 Poppler 时保留兼容回退；
- 普通对话、分析结果、结构化追问均执行单个文本字段最多 150 字的前后端双重约束；
- 125 项自动化测试，并完成真实 X 光分析、真实普通对话、真实结构化追问及四条用户确认阳性步态素材回归。

## 本地运行

要求 Python 3.12、3.13 或 3.14。

从 GitHub 下载 ZIP 并解压后：

1. 需要真实模型时先双击 `配置模型.bat`，输入 OpenAI 兼容接口地址、API Key 和模型名；密钥只写入本地 `.env`，不会进入 Git。
2. 双击 `启动电脑端.bat`。首次运行会自动检测受支持的 Python、创建 `.venv` 并安装依赖，因此需要能够访问 Python 包源；后续运行直接复用环境。
3. 只体验界面和 Fake 流程时可跳过模型配置；真实对话和真实检测必须使用有效模型配置。

脚本优先使用 PowerShell 7，并兼容 Windows PowerShell 5.1。它会检查同端口上的 Fura 服务版本、检查健康状态后打开独立电脑端窗口。若启动失败，窗口会保留错误提示，详细日志位于 `.runtime/server-error.log`。完整首次使用说明见 `QUICKSTART.md`。

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

对于 `qwen3.7-plus` 等已知混合思考模型，程序默认使用有限思考预算。质量关键的完整视频与正常漏检复核保持 4096；中间关键帧证据为 3072；证据汇总为 2048，若最终合同或证据校验不通过则回到 4096 修复。一次检测内复用 HTTP 连接；相同输入仅复用已经过完整校验的原生视频严重/red 结果，降级结果和正常结论不缓存。图片/PDF 的最终汇总轮会重新附带原始视觉证据；步态视频严格按 Skill，仅汇总 Step 1 完整视频描述与 Step 2 关键帧描述。可用 `AGENT_MODEL_THINKING=auto|on|off` 控制策略，用 `AGENT_MODEL_THINKING_BUDGET` 覆盖默认预算。真实回归和优化过程属于本地交付资料，不放入 GitHub 源码包。

桌面启动也支持 `.runtime/model-config.path`：文件中只保存用户外部 `env` 文件的绝对路径，启动后读取外部配置，不会把 API Key 复制进项目 `.env`。进程环境变量和项目 `.env` 仍具有更高优先级。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

当前结果：`125 passed`。覆盖客户端入口、对话历史持久化/筛选/删除、智能与原始上传异常流、普通流式聊天与近期检测上下文、报告图片及多页 PDF 检测、三类图片检测、两类视频检测、各居家 Skill 的状态颜色映射、检测报告程度标签及颜色渲染、结构化纠偏后的机械契约稳定、失败结果弹窗与原文件重试、视频时序证据稳定、步骤级失败诊断与面向用户的结构化失败原因、具体观察证据、去示例化 Skill Prompt、视频三步流水线、手机视频旋转标准化、逐周期后肢评估、跨周期筛查旗标、异常候选优先定向提帧、正常结论单次原视频盲复核、盲复核90秒独立上限、正常/异常冲突强制橙色待确认、密集时间轴正常倾向强制橙色、无法准确判断的原因与复测输出、SSE仅遥测且不参与路由、原生失败不重跑密集Step 1、超硬预算确定性密集时间轴、原生与盲复核独立截止、密集帧单次失败边界、Qwen原生FPS单一配置与范围校验、流式首包/SSE/请求ID遥测、原生请求并发闸门、公网URL映射输入、原生视频分阶段超时诊断、HTTP 413原片受控压缩重试、快速故障重试、降级质量标记、关键帧有界并发与隔离失败兜底、分阶段推理预算与证据完整性回退、验证后结果缓存、连续视频/关键帧跨插件证据门、单帧正常步态相位防误报、重复动态异常防丢失、生物力学证据词表、步态red→严重映射、X光图片/PDF分步汇总、受控思考请求策略、图片/PDF最终汇总原始视觉证据、跨阶段证据冲突纠错、正常步态建议安全门、桌面外部模型配置指针，以及检测结果结构化追问、完整正常项证据保留、修复轮原始输入保留、五段/高亮校验和跨会话结果隔离。常规自动化测试使用Fake模型，不访问网络，也不作真实医学判断。

### 检测结果继续追问

报告检测和居家检测会返回 `result_id`。客户端发送追问时把它写入 `reply_to_result_id`，服务端读取完整分析结果并执行 `structured-response` Skill：

```text
result_id → 规范化并裁剪 analysis_json → structured-response Skill
          → JSON Schema + 五段标题 + highlights 双向校验
          → structured.segment SSE → 客户端分段渲染
```

真实模型候选首次校验失败时会自动要求模型修正一次；修复请求会继续携带原始图片、PDF 页面或视频关键帧及工作流证据，避免脱离素材补写。再次失败则终止该 Run，不向客户端展示不符合契约的内容。普通宠物管家聊天继续使用 `token.delta`，结构化追问使用 `structured.segment` 与 `structured.suggested_questions`。

### 视频理解插件

本机没有 OpenClaw `tools.media.video` 运行时，因此当前以阿里云 OpenAI 兼容接口原生支持的 `video_url` 作为替代视频理解插件。模型和 Prompt 可保持一致，但媒体预处理与采样实现并不天然等价。执行顺序与 Skill 对应：

```text
Step 1：MP4 → 负载准入 → 连续时序理解 → 异常候选时间点
  ├─ 步态未超硬预算：方向标准化 → 单次Qwen原生视频
  │    ├─ SSE：仅记录首包、事件数和请求ID，不启动、不取消也不选择其他输入模态
  │    ├─ 原生异常：直接进入关键帧复核
  │    ├─ 原生正常：相同原视频执行一次独立盲态Step 1（≤90秒，不传首轮JSON）
  │    └─ 原生明确失败：结束Step 1并返回结构化失败原因，不从头执行密集帧
  └─ 超过原生像素硬预算：从开始确定性执行一次约6 FPS全时段密集时间轴
       └─ 密集时间轴仅得正常倾向：最终强制orange/inconclusive，不确认整体正常
Step 2：异常候选优先 → FFmpeg 定向提取 2–3 帧 → Qwen 按原时间顺序分析并汇成关键帧证据 JSON
Step 3：两份 Step 1 JSON（若盲复核已执行）+ 一份 Step 2 证据 JSON + Skill 维度 → Qwen 一次汇总 → Schema/证据一致性校验 → 保存
```

插件配置位于 `config/home-check-plugins.yaml`：

```yaml
home-health-check-gait:
  video_fps: 10.0
  workflow: [video_understanding, evidence_frame_extractor, image_understanding, result_composer]
home-health-check-behavior:
  video_fps: 4.0
  workflow: [video_understanding, evidence_frame_extractor, image_understanding, result_composer]
```

所有类别的`prompt_source`固定为`skill.visual_recognition_prompt`，程序会从Skill的“视觉识别 Prompt 指令”代码块读取原文，不在代码里维护第二份Prompt；输出协议与运行上下文放在system指令中，不改写分类Prompt。普通素材继续使用原生步态10 FPS或行为4 FPS，`AGENT_VIDEO_FPS`由缓存、旧适配器和HomeCheck共用同一解析入口，并限制在Qwen支持的`0.1–10`范围。桌面端默认以Base64 `video_url`发送；部署环境已安全映射HTTP(S)目录时，可同时设置`AGENT_VIDEO_PUBLIC_ROOT`与`AGENT_VIDEO_PUBLIC_URL_PREFIX`改用远程URL，程序本身不会上传或公开文件。原生请求按连接、写入、连接池、读空闲和300秒总耗时分阶段控制；SSE只记录首包、末包、响应字节及上游请求ID，不再触发输入切换。未超硬预算的步态始终只执行原生视频Step 1，原生明确失败后不再发送几十张密集帧从头重跑。原生正常时，系统以相同原视频执行一次不携带首轮结论的盲复核；复核拥有独立90秒截止，超时后继续生成橙色待确认结果，不会取消已成功的首轮分析。两次原生Step 1正常/异常冲突时，Step 3不能被关键帧单独推回green或red。只有超过100%像素硬预算才从开始直接执行一次密集时间轴；密集时间轴的正常倾向固定转为orange/inconclusive。带显示旋转矩阵的视频仍生成不修改原件的物理旋正缓存；HTTP 413仍会从原片生成不超过7MB的代理并留在原生路线。Step 2继续从原片定向提取2–3张`921600`像素关键帧，关键帧最多3路并发且单帧失败后隔离重试。每份真实视频结果的`report_meta.analysis_runtime`记录SSE是否到达、实际FPS、原生输入模式、估算帧数/像素、分阶段网络遥测、原生与盲复核独立截止、方向标准化、413恢复状态、关键帧时间点和已完成步骤；底层服务身份不会写入对外结果。

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

从发布 ZIP 建立 GitHub 仓库：

```powershell
git init -b main
git add .
git commit -m "Initial Project F desktop agent"
git remote add origin <your-github-repository-url>
git push -u origin main
```

必须在解压后的项目目录内执行上述命令；不要在磁盘根目录执行 `git init` 或 `git add .`。真实模型配置只允许写入本地 `.env` 或部署 Secret，禁止提交。GitHub Actions 会在 Windows 和 Python 3.12 环境运行自动化测试。发布前可运行 `./scripts/verify-package.ps1` 检查文件边界。
