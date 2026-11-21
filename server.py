"""Local stdio entry point. stdout belongs exclusively to MCP JSON-RPC."""
import logging
import sys
import threading
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from workflow import WorkflowDriver as Driver
from errors import DriverError

LOG = logging.getLogger("sscnc_mcp")


def create_server(controller=None):
    app = FastMCP("sscnc-mf-local")
    controller_lock = threading.Lock()

    def call(operation, *args):
        nonlocal controller
        try:
            with controller_lock:
                if controller is None: controller = Driver()
            return getattr(controller, operation)(*args)
        except DriverError as exc:
            return dict(ok=False, error=exc.as_dict())
        except Exception:
            LOG.exception("Unexpected %s error", operation)
            return dict(ok=False, error={"code": "internal_error", "message": "Unexpected local driver error; inspect stderr log"})

    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    simulation = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def launch_simulator(bring_to_front: bool = True, maximize: bool = False) -> dict[str, Any]:
        """核对并置前已有斯沃，maximize可规范最大化以便截图；未运行时仅打开正常登录器，不选择授权或加工。"""
        return call('launch_simulator', bring_to_front, maximize)

    @app.tool(annotations=read_only)
    def status() -> dict[str, Any]:
        """只读检查固定版本 FANUC0iMF 的进程、窗口、程序位置、XYZ 与运行证据。possible_complete 不是已验证完成。"""
        return call("status")

    @app.tool(annotations=simulation)
    def start() -> dict[str, Any]:
        """从已加载程序的初始索引启动；要求 MEM、非急停、无模态、无活动 worker。不会自动切模式、回零或 Reset。"""
        return call("start")

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def pause() -> dict[str, Any]:
        """有活动 worker 时发送一次 Feed Hold，等待退出和稳定位置；已停止则不发消息。未保证插补中即时暂停。"""
        return call("pause")

    @app.tool(annotations=simulation)
    def resume() -> dict[str, Any]:
        """仅续跑本服务已确认且仍未改变的暂停位置；要求 MEM、非急停、无模态。不重新加载/Reset 程序。"""
        return call("resume")

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def set_view(view: Literal["front", "right", "top", "zoom_in", "zoom_out"]) -> dict[str, Any]:
        """停止时发送一个固定视角或一次整体缩放命令。无任意角度接口；返回发送结果，不冒称画面已核对。"""
        return call("set_view", view)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def save_workpiece(filename: str) -> dict[str, Any]:
        """停止时经正常 Save As 对话框创建 .wp 文件。请使用 SSCNC_WORKSPACE 内的新文件绝对路径，父目录须存在。"""
        return call("save_workpiece", filename)

    @app.tool(annotations=read_only)
    def setup_status() -> dict[str, Any]:
        """停止时读回当前程序/工件路径、毛坯、夹具、G54～G59和H/D表、刀具映射与有效刀长。"""
        return call('setup_status')

    @app.tool(annotations=read_only)
    def read_program(start_line: int = 1, max_lines: int = 100) -> dict[str, Any]:
        """分页读取斯沃当前编辑器的真实 NC 缓冲和哈希，含尚未保存的内容。"""
        return call('read_program', start_line, max_lines)

    @app.tool(annotations=simulation)
    def load_program(filename: str, discard_unsaved: bool = False) -> dict[str, Any]:
        """经普通打开对话框加载 NC 并逐内容读回核对。存在未保存编辑时默认取消加载。"""
        return call('load_program', filename, discard_unsaved)

    @app.tool(annotations=simulation)
    def open_workpiece(filename: str, discard_unsaved: bool = False) -> dict[str, Any]:
        """打开已有 .wp 毛坯或成品，校验格式并核对应用内路径。"""
        return call('open_workpiece', filename, discard_unsaved)

    @app.tool(annotations=simulation)
    def set_mode(mode: Literal['edit', 'mem', 'reference']) -> dict[str, Any]:
        """停止时切换EDIT/MEM/REF模式并读回确认。"""
        return call('set_mode', mode)

    @app.tool(annotations=simulation)
    def set_emergency(active: bool) -> dict[str, Any]:
        """停止时将急停设为明确的目标状态，先读回后决定是否发送一次切换。"""
        return call('set_emergency', active)

    @app.tool(annotations=simulation)
    def initialize_controller(home_axes: bool = False) -> dict[str, Any]:
        """核对已上电的控制器并切到REF；home_axes=True时按Z/X/Y回参考点并读回。"""
        return call('initialize_controller', home_axes)

    @app.tool(annotations=simulation)
    def reset_program() -> dict[str, Any]:
        """停止时发送控制器RESET，返回实际程序位置变化。"""
        return call('reset_program')

    @app.tool(annotations=simulation)
    def open_setup(panel: Literal['stock', 'fixture', 'placement', 'tools', 'machine']) -> dict[str, Any]:
        """打开毛坯/装夹/放置/刀具/机床设置对话框，返回真实控件及当前值。"""
        return call('open_setup', panel)

    @app.tool(annotations=read_only)
    def inspect_setup_dialog() -> dict[str, Any]:
        """读取本服务打开的设置对话框，刷新可用字段、按钮和选项。"""
        return call('inspect_setup_dialog')

    @app.tool(annotations=simulation)
    def setup_action(control_id: int | None = None, value: str | None = None,
                     button_id: int | None = None, row_index: int | None = None) -> dict[str, Any]:
        """对刚读取的设置控件执行一次字段填写、列表行选择或已识别按钮动作，返回读回结果。"""
        return call('setup_action', control_id, value, button_id, row_index)

    @app.tool(annotations=simulation)
    def controller_keys(keys: list[str]) -> dict[str, Any]:
        """停止时使用控制器导航、数字和偏置输入键。OFS显示刀补/坐标页，MSG显示报警页；按截图和setup_status核对输入。"""
        return call('controller_keys', keys)

    @app.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def capture_screenshot(filename: str, include_owned: bool = True) -> dict[str, Any]:
        """将真实斯沃屏幕区域保存为全新PNG，检测遮挡、黑屏和最小化，返回截图路径及状态。"""
        return call('capture_screenshot', filename, include_owned)

    @app.tool(annotations=simulation)
    def show_program_page(start_line: int = 1, max_lines: int = 80) -> dict[str, Any]:
        """仅当原生G代码列表已显示且有内容时滚动到请求行；空列表请用controller_program_page。"""
        return call('show_program_page', start_line, max_lines)

    @app.tool(annotations=simulation)
    def controller_program_page(action: Literal['open', 'previous', 'next'] = 'open') -> dict[str, Any]:
        """EDIT停止状态下打开/翻动控制器程序页，读回显示行与编辑光标，核对执行位置和程序内容未变。"""
        return call('controller_program_page', action)

    @app.tool(annotations=simulation)
    def rotate_view(dx: float = .12, dy: float = .08) -> dict[str, Any]:
        """停止时通过已确认3D视图进行相对拖动旋转，dx/dy各限±0.35，结果需截图核对。"""
        return call('rotate_view', dx, dy)

    @app.tool(annotations=simulation)
    def set_display(item: Literal['workpiece', 'stock', 'tool', 'toolpath', 'coordinates'], visible: bool) -> dict[str, Any]:
        """设置工件、毛坯、刀具、刀轨或坐标显示，核对菜单勾选状态。"""
        return call('set_display', item, visible)

    @app.tool(annotations=read_only)
    def inspect_program(filename: str, start_line: int = 1, max_lines: int = 100) -> dict[str, Any]:
        """离线检查NC文件的内容、哈希、分页、G10/G54和T/H/D字面指令。"""
        return call('inspect_program', filename, start_line, max_lines)

    @app.tool(annotations=read_only)
    def inspect_workpiece(filename: str) -> dict[str, Any]:
        """离线读取原生.wp的尺寸、网格和哈希。"""
        return call('inspect_workpiece', filename)

    @app.tool(annotations=simulation)
    def validate_pocket(filename: str, output_directory: str) -> dict[str, Any]:
        """用已配置的本地核验脚本检查 .wp 工件。报告写入工作区内的全新目录，passed 表示脚本的核验结论。"""
        return call('validate_pocket', filename, output_directory)

    @app.tool(annotations=simulation)
    def completion_monitor(action: Literal['arm', 'status', 'close'] = 'status') -> dict[str, Any]:
        """为后续运行启用/读取/关闭运行来源监视，区分真实M30、正常退出与几何验收。"""
        return call('completion_monitor', action)

    return app


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
