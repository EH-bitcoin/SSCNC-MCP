# SSCNC-MCP

面向 Swansoft SSCNC FANUC0iMF 的 Windows 本地 MCP 服务。连接 Codeg、Codex 或其他支持 stdio 的 MCP 客户端后，可以读取仿真状态、检查 NC 程序、设置毛坯和刀具、操作仿真视图，并检查导出的工件。

项目提供 28 个工具。基础 NC 和 `.wp` 读取可以离线运行。模拟器操作需要本机 SSCNC、交互式 Windows 桌面和一个可识别的目标实例。

## 快速开始

- Windows 和 Python 3.10 或更新版本
- 本机安装的 SSCNC FANUC0iMF 7.2.5.2。设置面板按中文版控件识别。服务还核对 [constants.py](constants.py) 中的目标程序 SHA-256
- 一个可写的输出目录。截图、另存工件和核验报告都写到该目录下

~~~powershell
git clone https://github.com/EH-bitcoin/SSCNC-MCP.git
Set-Location SSCNC-MCP
py -3.10 -m venv .venv
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
~~~

将下面的路径替换为实际安装目录和输出目录。安装目录应包含 `CNCLogin.exe` 与 `FANUC\FANUC0iMF\sscnc.exe`。

~~~powershell
$env:SSCNC_INSTALL_DIR = 'C:\Apps\SSCNC'
$env:SSCNC_WORKSPACE = 'C:\SSCNC-output'
New-Item -ItemType Directory -Path $env:SSCNC_WORKSPACE -Force | Out-Null
& '.\.venv\Scripts\python.exe' -X utf8 -B accept_readonly.py
~~~

验证脚本会启动一个临时 stdio 连接，执行初始化、工具枚举和 `status`。`protocol_ok: true` 与 `tool_count: 28` 表示连接正常。斯沃尚未启动时，`state` 为 `not_running`。

## 接入 MCP 客户端

| 客户端 | 配置 |
|---|---|
| Codeg 的 Codex CLI 智能体、Codex | 把 [TOML 示例](examples/codex.toml) 合并到 Codex 的 `~/.codex/config.toml`，替换 Python、服务脚本和环境变量中的绝对路径 |
| 采用 `mcpServers` 的客户端 | 使用 [JSON 示例](examples/mcp.json)，将内容合并到客户端的 MCP 设置并替换路径 |

保存配置后新建会话，先调用 `status`。斯沃未运行时，可通过正常入口手动启动，或调用 `launch_simulator` 打开登录器，再由用户完成登录和机型选择。

| 环境变量 | 用途 |
|---|---|
| `SSCNC_INSTALL_DIR` | SSCNC 安装根目录。默认 `C:\Program Files (x86)\Nanjing Swansoft\SSCNC` |
| `SSCNC_WORKSPACE` | 输出目录。默认仓库目录 |
| `SSCNC_VALIDATOR_SCRIPT` | 可选的本地工件核验脚本绝对路径 |
| `SSCNC_VALIDATOR_PYTHON` | 可选的核验环境 Python 可执行文件绝对路径。默认使用服务的 Python |

## 工具和操作流程

| 场景 | 入口 |
|---|---|
| 检查连接、状态和准备参数 | `status`、`setup_status`、`inspect_setup_dialog` |
| 浏览和加载程序 | `inspect_program`、`load_program`、`read_program`、`controller_program_page` |
| 毛坯、夹具、工件放置、刀具 | `open_setup`、`setup_action`、`open_workpiece` |
| 仿真控制和视图 | `set_mode`、`start`、`pause`、`resume`、`rotate_view`、`capture_screenshot` |
| 输出和检查工件 | `save_workpiece`、`inspect_workpiece`、`validate_pocket` |

完整参数、返回结果和限制见 [28 个工具说明](docs/tools.md)。

1. 用 `status` 检查进程身份、窗口响应、加工活动和弹窗。
2. 用 `inspect_program` 查看 NC 文件，再按需加载。加载前保存已有编辑，并在返回后读回编辑器内容。
3. 用 `setup_status` 核对毛坯、工件原点、H/D 和刀具尺寸。出现设置弹窗时，先观察控件再操作。
4. 停止后将工件另存为新的 `.wp`，用 `inspect_workpiece` 核对保存网格的基本尺寸与层高。

基础文件检查使用 Python 标准库。若需要 `completion_monitor`，在同一虚拟环境安装 [可选监视依赖](requirements-monitor.txt)。需要对特定工件做采样几何验收时，可以配置 [本地核验脚本](docs/validator.md)。

## 文件说明

~~~text
SSCNC-MCP/
├── server.py                 # FastMCP 入口，工具 schema 和错误映射
├── constants.py              # 安装与输出路径，固定构建的哈希和字段映射
├── driver.py                 # 目标识别、命令前置检查、暂停续跑与另存
├── workflow.py               # 文件加载、模式和准备操作的编排
├── win32_backend.py          # Windows 进程、窗口、消息和只读遥测接口
├── memory.py                 # 加工状态、当前位置和计数器的读取
├── file_state.py             # 未保存标志和原生打开提示条件的读取
├── preparation.py            # 毛坯、刀具与相关对话框的状态管理
├── initialization.py         # 控制器初始化、参考点操作与 RESET
├── read_setup.py              # 工件、刀具、坐标系和程序缓冲的读取
├── simulator_launcher.py      # 识别现有窗口，或启动 SSCNC 的登录器
├── view_extensions.py         # 控制器分页、视角、截图和窗口遮挡检查
├── completion_monitor.py     # 可选 Frida 监视器与结束事件核验
├── file_inspection.py         # 离线 NC 分页、WP 网格检查与核验脚本接口
├── errors.py                  # 结构化驱动错误
├── accept_readonly.py         # stdio 握手、目录和状态的验证脚本
├── requirements.txt          # 服务依赖
├── requirements-monitor.txt  # 完成监视器的可选依赖
├── examples/                  # MCP 的 JSON 与 Codex TOML 配置示例
├── docs/                      # 工具、架构、支持范围和核验接口
└── tests/                     # 合成控件、测试网格和协议回归用例
~~~

## 测试与支持范围

~~~powershell
& '.\.venv\Scripts\python.exe' -X utf8 -B -m unittest discover -s tests
~~~

测试使用替身或临时文件，可以在斯沃关闭时运行。仓库面向软件仿真。准备和保存操作请在同一 MCP 会话中串行执行，动作返回后核对状态再继续。请参阅 [架构与已知限制](docs/architecture.md)。

## 常见状态

| 返回码 | 处理方式 |
|---|---|
| `not_running` | 启动配置路径下的 SSCNC FANUC0iMF，再调用 `status` |
| `binary_hash_mismatch` | 用 PowerShell 的 `Get-FileHash -Algorithm SHA256` 核对目标程序版本和哈希 |
| `launcher_hash_mismatch` | 通过自己安装的 SSCNC 登录器手动启动，再调用 `status` |
| `unsaved_document` | 保存当前编辑，再重试加载 |
| `analysis_environment_unavailable` | 检查 [核验脚本配置](docs/validator.md) 和对应 Python 环境 |

SSCNC 由用户单独安装
