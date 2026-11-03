# ServBay Package Builder v1

ServBay 1.0 的软件包编译仓库，负责编译所有软件包并交给下游处理。

## Windows v2 单包受控增量

`build-windows-v2-incremental.yml` 仅支持手动触发。必须指定一个普通包；同一包有多个待更新系列时必须再指定真实 v2 `major`。默认 `continue_build=false`，只读公共 v1/v2 目录并显示计划，不构建、不通知下游，不修改版本配置，也不启用日常更新。

例如选择 Cloudflared 的 `major=0`，或分别选择 Composer 的 `major=2`、`major=2.2`。系列取值沿用 `runtime/ci/package_meta.py`；不能直接使用 v1 目录中的旧 alias。选择器先过滤所选包，再解析其版本，不因未选择的软件包版本表示而失败。目标目录版本相同或更新时跳过，不降级；合法空计划与目录请求/数据错误分别处理。

显式 `continue_build=true` 后，只复用现有 `build-windows-v2-package.yml` 重打包所选公开源 URL/SHA，并通知下游处理同一个精确 run/attempt/commit。`upload_dry_run` 默认 `true`；真实发布仍需另行审核并通过下游既有发布开关与预检。通知使用目标默认分支，不自动消费本地或另一仓库的同名分支。

普通增量排除 PHP、PostgreSQL、独立模块、AI Gateway、MCP 和 simple-acme；disk-cleaner 只分发既有占位包。它不编译上游源码、不运行 Windows 程序，也不替代目标系统验收。v1 的 `check-updates`、`build-and-dispatch` 与既有固定 batch 入口保持原流程。

原单包手动入口继续接受精确 source URL/SHA，只产 artifact。它和可复用入口都支持显式非负 `build_revision`，artifact 名称由真实 metadata 生成，非零 revision 带 `-rN`，7z 文件名仍遵循原契约。v1/v2 revision 各自独立，不能比较大小判断是否更新；同版本重发需要明确证据及经过审核的 v2 revision，不由增量选择器猜测源字节变化。

## 对应离线回归

使用已安装的 Python 及 7z/7zz。选择器实际使用已有 `requests`、`packaging` 依赖；workflow 测试复用现有 `test_v2_workflows.py` 的 PyYAML 解析 helper。测试依赖明确安装如下；`PyYAML==6.0.3` 沿用既有 `build-windows-pgvector.yml` 的测试版本，生产选择器和增量 workflow 不需要 PyYAML。

```sh
python3 -m pip install requests packaging PyYAML==6.0.3
python3 -m unittest discover -s tests -p 'test_windows_v2_incremental.py' -v
python3 -m unittest discover -s tests -p 'test_windows_v2_workflow.py' -v
python3 -m unittest discover -s tests -p 'test_windows_v2_repack.py' -v
python3 -m unittest discover -s tests -p 'test_windows_v2_metadata.py' -v
```

归档负例要求所用工具确实保存符号链接身份；可通过已有 `SEVEN_ZIP` 环境变量指定已安装的 7z/7zz。部分旧 p7zip 会把链接存成普通文件，此时应先检查 listing，不能把退化的 fixture 当作门禁验收。

测试只使用临时目录、保存的 catalog 和本地 fixture，不触发工作流、真实通知或生产发布。选择器也支持 `--v1-catalog`、`--v2-catalog` 显式读取已保存响应，并通过 `--output` 指定计划文件；它不会默认重写固定 batch 清单。
