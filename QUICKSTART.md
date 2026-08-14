# Fura-AI 宠物管家桌面端：首次使用

## Windows 一键启动

1. 安装 64 位 Python 3.12、3.13 或 3.14，并勾选“Add Python to PATH”。
2. 如需真实模型，双击 `配置模型.bat`，输入 OpenAI 兼容接口地址、API Key 和模型名。密钥只保存在本地 `.env`，不会进入 Git。
3. 双击 `启动电脑端.bat`。
4. 首次启动会自动创建 `.venv`、安装依赖并打开桌面窗口；后续直接复用环境。

只体验界面和 Fake 流程时可以跳过第2步。真实对话和真实检测必须先配置可用模型。

## 首次启动失败

- 确认 Python 版本为3.12～3.14：`python --version`。
- 确认首次安装时能够访问 Python 包源。
- 查看 `.runtime/server-error.log`。
- 8000端口被其他程序占用时，可在终端执行：

```powershell
.\启动电脑端.bat -Port 8010
```

## GitHub 使用

本目录本身就是独立仓库内容，禁止把它放在磁盘根目录后直接对整个磁盘执行 `git add .`。正确做法是在本目录中运行：

```powershell
git init -b main
git add .
git commit -m "Initial Fura-AI desktop release"
```

`.env`、用户历史、上传媒体、检测结果、虚拟环境和运行日志均已被 `.gitignore` 排除。
