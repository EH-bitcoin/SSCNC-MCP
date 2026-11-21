# 接入本地工件核验脚本

`inspect_workpiece` 可以直接读取版本 2 的原生 `.wp`，返回顶点、面、尺寸和水平层高。如果需要按项目图纸检查指定区域的槽深、轮廓和公差，可把可信的本地 Python 脚本接给 `validate_pocket`。

1. 将 `SSCNC_VALIDATOR_SCRIPT` 设置为脚本的绝对路径。
2. 如果脚本有自己的依赖环境，把 `SSCNC_VALIDATOR_PYTHON` 指向该环境的 Python 可执行文件。默认使用 MCP 服务的 Python。
3. 调用 `validate_pocket`，传入一个 `.wp` 文件和 `SSCNC_WORKSPACE` 内的新输出目录。目标目录的父目录需要先创建。

核验脚本需要实现两个函数。

| 函数 | 契约 |
|---|---|
| `analyze(path, kind, description, tolerances)` | 读取工件，返回 `(report, evidence)`。`report` 至少提供 `source.sha256`、布尔 `passed` 和 `limitations` 数组 |
| `write_validation(output, report, evidence, plot=True)` | 在已创建的 `output` 目录生成报告和可选图像 |

`path` 与 `output` 都是 `pathlib.Path`。服务会对工件做有界读取和格式检查，核对文件 SHA-256 后才返回报告。脚本会以本机用户权限在单独的 Python 进程中执行，时间预算为 45 秒。把诊断输出写到 stderr，以保留结果所需的 stdout。

核验脚本负责定义具体的测量项和适用公差。选择脚本时，请检查它使用的尺寸、坐标与待验收图纸是否一致。
