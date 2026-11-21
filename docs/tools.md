# 工具参考

服务通过 stdio 提供 28 个工具。参数说明以 MCP `tools/list` 当前返回的 schema 为准。业务拒绝通过 `ok: false` 和 `error.code` 表达。`ok: true` 表示该步骤处理成功，后续还要看 `outcome` 和专门的验证字段。

## 状态与只读检查

| 工具 | 常用参数 | 观察结果 |
|---|---|---|
| `status` | 无 | 固定程序身份、窗口响应、模态窗口、运行标志、当前位置和程序位置 |
| `setup_status` | 无 | 停止时读取程序和工件路径、毛坯、夹具、G54～G59、H/D 和刀具几何 |
| `inspect_setup_dialog` | 无 | 读回本会话打开的设置窗口和已识别控件 |
| `read_program` | `start_line=1, max_lines=100` | 当前 NC 编辑缓冲的分页、哈希和未保存文本。每页最多 300 行 |
| `inspect_program` | `filename, start_line=1, max_lines=100` | 离线读取 NC 文本、分页以及 G/T/H/D 字面指令。每页最多 500 行 |
| `inspect_workpiece` | `filename` | 离线读取版本 2 原生 `.wp` 的尺寸、顶点、面、层高与哈希 |

## 窗口、准备与文件

| 工具 | 常用参数 | 作用 |
|---|---|---|
| `launch_simulator` | `bring_to_front=true, maximize=false` | 核对并置前已启动的仿真器。未启动时打开已识别的登录器 |
| `load_program` | `filename, discard_unsaved=false` | 使用原生打开对话框加载 `.cnc`、`.nc` 或 `.txt`，再核对路径和程序内容 |
| `open_workpiece` | `filename, discard_unsaved=false` | 预检原生 `.wp` 并打开，再读回应用中的路径 |
| `save_workpiece` | `filename` | 停止后经原生另存流程创建新的 `.wp`。目标位于工作区且父目录存在 |
| `open_setup` | `panel` | 打开 `stock`、`fixture`、`placement`、`tools` 或 `machine` |
| `setup_action` | `control_id, value, button_id, row_index` | 执行一次已识别的字段、列表或按钮操作。先读回当前对话框再选择参数 |
| `validate_pocket` | `filename, output_directory` | 调用用户配置的本地脚本检查工件。报告写入工作区内的新目录 |

文件参数应使用本机绝对路径。需要保留的编辑请在打开其他文件前保存。`discard_unsaved=true` 会允许丢弃未保存数据。

## 控制器与运行

| 工具 | 常用参数 | 作用 |
|---|---|---|
| `set_mode` | `mode` | 停止时切换 `edit`、`mem`、`reference` 并读回 |
| `set_emergency` | `active` | 停止时切换仿真急停状态并读回 |
| `initialize_controller` | `home_axes=false` | 检查上电、解除仿真急停、切到 REF。显式请求才发送 Z、X、Y 回参考点操作 |
| `reset_program` | 无 | 发送控制器 RESET，读回实际程序位置，报告效果是否确认 |
| `controller_keys` | `keys` | 使用允许的导航、数字和偏置输入键。输入后读回当前页与参数 |
| `start` | 无 | 在 MEM、上电、停止、无模态且程序处于初始位置时请求启动 |
| `pause` | 无 | 发送 Feed Hold，等待 worker 停止和稳定位置 |
| `resume` | 无 | 从本会话记录的暂停位置请求续跑。暂停期间应保持程序和准备参数稳定 |
| `completion_monitor` | `action='status'` | 通过 `arm`、`status`、`close` 管理可选监视器，观察 M30 与正常退出 |

`possible_complete` 是停止态信号。实际验收要结合运行证据、对应的程序与新保存的工件。监视器的详细边界见 [架构与已知限制](architecture.md)。

## 视图与截图

| 工具 | 常用参数 | 作用 |
|---|---|---|
| `set_view` | `view` | 发送 `front`、`right`、`top`、`zoom_in` 或 `zoom_out` |
| `rotate_view` | `dx=.12, dy=.08` | 在已识别的 3D 视图上相对旋转，各轴范围 ±0.35 |
| `set_display` | `item, visible` | 切换工件、毛坯、刀具、刀轨或坐标显示并读回菜单状态 |
| `controller_program_page` | `action='open'` | EDIT 停止时打开或翻动控制器编程页，操作为 `open`、`previous` 或 `next` |
| `show_program_page` | `start_line=1, max_lines=80` | 滚动已显示且有内容的原生 G 代码列表 |
| `capture_screenshot` | `filename, include_owned=true` | 写入工作区内的新 PNG，返回窗口范围、遮挡检查和画面质量结果 |

视角和翻页要结合实际画面读回。截图前检查窗口是否置前，保存后查看 `usable` 字段。
