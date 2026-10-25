## 阶段 1: 建立 Ollama 新旧构建契约
**目标**: 明确 macOS 与 Windows 在 Ollama 0.30 前后的构建和产物布局。
**成功标准**: 新版必须包含独立 llama-server/llama-quantize，旧版仍可使用历史流程。
**测试**: 对照上游 0.31.1 构建脚本、CMake 目标和搜索路径。
**状态**: 已完成

## 阶段 2: 修复 macOS 构建脚本
**目标**: 让 runtime/build_package 为 Ollama 0.30+ 构建并打包完整原生 payload。
**成功标准**: 产物同时包含 ollama、llama-server、llama-quantize，并保留旧版构建能力。
**测试**: 新增构建脚本契约测试，执行现有 Python 测试。
**状态**: 已完成

## 阶段 3: 修复 Windows 独立编译仓库
**目标**: 让 Ollama-Windows-Portable 使用上游完整 Windows 构建流程并发布完整 ZIP。
**成功标准**: ZIP 内含 ollama.exe 和 lib/ollama 下的 native helper/runtime 文件。
**测试**: 校验 workflow 语法、构建步骤和打包输入的静态契约。
**状态**: 已完成

## 阶段 4: 集成验证与交付
**目标**: 验证两个仓库的脚本、测试和最终产物契约。
**成功标准**: 所有可运行测试通过，无残留旧版单文件验证逻辑。
**测试**: Python 单测、zsh 语法检查、YAML 解析、关键路径断言。
**状态**: 已完成

## 完成总结
- macOS 对 Ollama 新源码布局使用 CMake superbuild，固定 metal_v3，并打包完整 native payload；旧源码保留 legacy 流程。
- Windows 独立仓库使用上游 cpu + ollama 构建步骤，完整发布 dist/windows-amd64，并验证 ZIP 内 helper 路径。
- macOS/Windows 均从仅验证主程序升级为强制验证 llama-server 与 llama-quantize。
- 本机仅执行单元测试与静态语法检查，未执行任何编译、打包或环境切换命令。

---

## Apache DLL 修复阶段 1: 定位产物缺陷
**目标**: 确认缺失 DLL 的准确名称、引用方及缺陷所在仓库。
**成功标准**: 同时核对发布 ZIP 文件清单和 PE 导入表，排除 Builder 二次打包漏文件。
**测试**: 下载 Apache 2.4.68 Windows x64 发布 ZIP，检查 `libaprutil-1.dll` 的导入表。
**状态**: 已完成

## Apache DLL 修复阶段 2: 建立回归契约
**目标**: 为 Apache 上游构建增加 Expat 绑定和便携运行时依赖测试。
**成功标准**: 测试能够在修复前识别 Strawberry Perl Expat 污染风险。
**测试**: Python 静态契约测试。
**状态**: 已完成

## Apache DLL 修复阶段 3: 修复上游构建参数
**目标**: 让 APR-util 显式链接 Apache 安装前缀内刚构建的 Expat。
**成功标准**: `libaprutil-1.dll` 导入 `libexpat.dll`，不再导入 `libexpat-1__.dll`。
**测试**: 静态契约测试、构建脚本中的 `dumpbin /dependents` 门禁。
**状态**: 已完成

## Apache DLL 修复阶段 4: 集成验证与交付
**目标**: 验证上游脚本和 Builder 现有测试，明确重新发布步骤。
**成功标准**: 所有可运行测试通过，构建产物在隔离 PATH 下可执行 `httpd.exe -v`。
**测试**: Python 单测、批处理静态检查、Builder 全量单测。
**状态**: 已完成

## Apache DLL 修复完成总结
- Apache 2.4.65、2.4.66、2.4.68 的现有 Windows x64 发布包均受影响；2.4.63 不受影响。
- 根因是 APR-util 的 `FindEXPAT` 受 Strawberry Perl PATH 污染，错误链接到 `C:/Strawberry/c/lib/libexpat.a`。
- 上游构建参数现已固定为 `%PREFIX%/include` 与 `%PREFIX%/lib/libexpat.lib`。
- 构建结束前会检查 `libaprutil-1.dll` 导入表，并在隔离 Strawberry PATH 后运行 `httpd.exe -v`。
- Apache 上游 3 项回归测试和 Builder 8 项现有测试全部通过。
- 本机不是 Windows 环境，尚未执行完整 Apache 源码编译；修复后的 Release 仍需在 GitHub Actions 中重建。

---

## 组合结果计数补修阶段 1: 固定真实调用与失败回归
**目标**: 落实用户批准的两项组合失败计数：core 编译成功后最终化失败，以及模块编译成功后依赖绑定失败。以当前 `Build v2 <unit>`、`Finalize Build v2 <unit>`、`Bind Build v2 <unit>` 的实际 workflow 和已有两份受控入口复现为依据。
**成功标准**: 新测试经真实 `job_outcomes` → `collect_results` 和离线 collector CLI 在旧实现下显示计数仍为零；不把编译前置失败、取消、未开始、来源不明或候选产物计为正式成功。
**测试**: VM119 隔离 Builder 测试根中执行新增回归，保留红灯日志；宿主仅静态编辑和传输。
**状态**: 已完成

## 组合结果计数补修阶段 2: 最小阶段配对
**目标**: 仅修正 `runtime/ci/build_state.py` 的结果分类。以精确单元身份、已验证的 run/attempt 来源及 SHA 配对已执行的编译与对应后续阶段；可用 job 来源字段矛盾、重复或未知状态时继续锁定。
**成功标准**: 真实发生编译且对应最终阶段失败/超时的单元只增加一次连续失败；取消、前置失败不增加；保持原连续三次停止、标准单 job、正式 Artifact 优先及 invalid Artifact 锁定机制，不修改 state schema、workflow 或发布协议。
**测试**: 两类组合的红→绿；取消/跳过/前置失败、未知/重复/错误来源、不同单元、有效正式产物和候选隔离回归。
**状态**: 已完成

## 组合结果计数补修阶段 3: 原生回归与逐处证伪
**目标**: 在 VM119 执行相关既有 state/reservation/collection/入口回归，逐处恢复缺陷行为以证明新增守卫有效，汇总原始证据与当前输入 SHA。
**成功标准**: 相关测试通过、每处必要突变被真实行为断言抓住并还原源码；保留原两份失败复现、Next Testing App/数据和其他已有工作。不重复旧 254 项及 104 处全集，不再开启本阶段第三轮审查，不执行 Git 写入、Runner、上传或部署。
**测试**: 针对性 unittest、受影响旧回归、独立逐点突变、输入/源码还原哈希核对；不在宿主执行项目代码。
**状态**: 已完成


## 组合结果计数补修完成记录（用户裁决后补修）
- 两个已有受控入口复现已转换为公开白名单夹具；core 名称按当前 workflow 的 `Finalize Build v2 <unit>` 正规化，原复现字节保留在客机证据。旧生产源 `60196b8f…` 下新增 10 项产生 38 个真实行为失败，包含真实离线 collector CLI 的零计数；红灯版测试与后续补强版测试分别保存，未把后续输入冒称为原红灯输入。
- 修复只改结果分类：精确单元、SHA 及可用 run/attempt 字段匹配，编译成功后读取该单元实际 Finalize/Bind 阶段。真实失败/超时进入原连续失败计数，同一次 attempt 只计一次，三次仍停止。GitHub 的已验证 attempt 专用 jobs 接口锚定来源，单 job 缺省 `run_attempt` 可接受；矛盾值、重复 job/step 或未知状态继续锁定。
- 取消、跳过、未执行编译或最终阶段的前置失败不计；标准单 job 行为保留。正式有效 Artifact 仍优先进入 build-ready，invalid 仍锁定，candidate-core/candidate-module 仍不可发布。未修改 workflow、state schema、阈值、Uploader 或生产协议。
- VM119 原生 UID501 最终 53 项受影响回归全部通过（含新增 10 项），21/21 本批逐点行为突变均被断言捕获并立即恢复原字节；最终生产源 SHA256 为 `d891a0519b97d7f5074297f9147b348be97569516bb2bcee5a9424334270884e`。首次相关回归只有 root 身份的 Git 属主前提错误，原始失败已保留，未修改 Git 配置或旧断言。
- 原始红、绿、最终回归、21 处逐点映射/日志、两份旧复现、旧生产源与当前输入统一保存在客机 Builder 测试根的 `composite-outcomes-evidence.zip`，回执为 `logs/composite-outcomes-verification.json`。保留以前阶段证据，不重跑旧 254/104 全集，不新增第三轮阶段审查。
- 宿主仅静态读写与传输；本批未运行真实 GitHub/API 写入、Runner、上传、部署或 Git 写入，未触碰 Next Testing App/数据和 Runtime 基座。真实在线 job 调度仍未在本次启动。


## 双版本更新入口补齐阶段 1: 冻结输入和失败行为
**目标**: 按已批准设计§3.1/3.3补齐仅更新入口的all及默认关闭的v2定时刷新；保留现有v1 cron和具体v1/v2构建身份。
**成功标准**: 以当前五文件基线记录新增行为红灯；宿主仅静态读写和传输，不设置仓库变量或调用真实GitHub。
**测试**: VM119新增all解析、四种定时模式、真实workflow步骤的受控gh请求，以及检查模式不预留/不编译行为。
**状态**: 已完成

新增7项在旧实现下产生20个预期AssertionError（缺少all/调度功能），无导入或语法错误，原始日志为`logs/schedule-refresh-red.log`。

## 双版本更新入口补齐阶段 2: 最小同仓刷新接线
**目标**: 更新入口all明确展开v1原运行和一次v2/macOS刷新运行；后者复用已有精确SHA接续和构建交接。定时变量off/check/build/publish默认off，check不启动macOS或模块host任务。
**成功标准**: all不进入具体build/meta/cache；all与pending-only/windows冲突拒绝；取消不fanout，v2和pending子运行不递归；同仓GITHUB_TOKEN仅新job actions:write，不新增secret、不改三失败/产物门禁。
**测试**: 新增行为绿灯及现有workflow/run-control受影响回归；固定gh夹具只捕获命令和payload。
**状态**: 已完成

## 双版本更新入口补齐阶段 3: 增量证伪和集中交付
**目标**: 必要逐点突变、源码还原及输入SHA、原始红绿证据，交付本次五文件增量供fresh影响锥审查。
**成功标准**: 真实新行为与受影响旧回归通过，语法/actionlint无新问题；独立审查仅本次接线约20文件内，不重审已结束3b或分类器，不启动Runner、Git写入、上传部署。
**测试**: VM119限定测试与既有actionlint；逐处新守卫突变及最终还原；原生编译/在线GitHub调度不在本批执行。
**状态**: 已完成（本轮实现、VM定向验证及第一轮独立增量审查；生产未启用）


### 双版本更新入口实现及验证交接

- 只有`check-updates`新增`runtime_version=all`。原运行明确执行v1，再在其check阶段结束后一次同仓dispatch新v2/macOS刷新运行（`pending_only=false`）；v1编译可独立继续。两者配置提交按现有main契约顺序处理，具体build/meta/cache/API仍只携带v1或v2。`all + pending_only`和`all + target=windows`拒绝，v2和pending子运行不再次fanout。
- `V2_PACKAGE_SCHEDULE_MODE`是非secret仓库变量，未设置或`off`时新定时job跳过；本次未设置任何变量。`check`映射`continue_build=false/upload_dry_run=true`，沿现有检查入口刷新包表/待处理记录，不预留构建、不启动macOS或模块host任务、不通知Uploader；`build`映射true/true，确实消耗编译资源且Uploader仅dry-run；`publish`映射true/false，代码具备但生产启用仍需另获许可。未知模式明确失败，不默默生产。
- manual all沿原有continue_build/upload_dry_run/full_rebuild输入，默认false/true/false；v1定时的cron、平台范围及既有编译/发布选择不变。取消条件阻止新fanout，API失败传播且不重试；未修改原三次失败、候选隔离、正式Artifact优先、来源或发布门禁。
- 原生VM119：新增7项在旧基线下20个预期断言失败，当前7项通过；直接受影响workflow/run-control/module-workflow三组27项全部通过。13项直接行为突变和1项取消job-if配置契约突变均被断言捕获，逐处还原后7项再次通过。取消项只证明工作流条件契约，不冒称执行了真实GitHub取消。actionlint退出0，三个修改Python输入AST解析通过，未新增工具/依赖。
- 五文件修改前基线为`inputs/schedule-refresh-baseline.zip`，精确增量diff、所有原始红绿/突变日志与输入SHA集中交接；不使用整份dirty Git diff冒称本批范围。第一轮fresh影响锥审查已通过，无本轮可证P0–P3；不以旧3b审查替代，也不追加无新增代码的复审。
- 独立审查覆盖Builder接线及直接消费者、Uploader两份文档与对应契约、Utility主线范围/发布前提记录，共约27份高相关文件。审查者独立重建精确diff，核49个载荷SHA、15份冻结输入与原始红绿/14项逐点突变；actionlint调用禁用shellcheck和pyflakes，不表述为这两项已通过。真实GitHub调度/取消、生产上传和稳定双架构基座仍未验证。
- 证据已回传宿主Downloads：`ServBay-package-builder-schedule-refresh-20261022-evidence.zip`，144412字节，SHA256 `6f301af37b47ba5c692e52376655fe96e2c1d3b4c889db279ebae3cd0d7a8a38`；对应verification为5356字节，SHA256 `75fd96753f97c2194f7ea9036b97d2c24e43c40dc97b6df0da007ee89b500b47`。审查后仅更新本计划完成状态，四份生产/测试输入保持冻结SHA；不重跑已绿测试。
- 官方语义已核：[GITHUB_TOKEN的显式dispatch例外](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)、[dispatch的Actions write权限及branch/tag ref](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)、[默认分支触发条件](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)、[未配置vars为空字符串](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#vars-context)。新job仅使用本仓库github.token并授予actions:write，旧跨仓Uploader通知凭据沿用，没有新增secret。
- 本批仅五文件静态修改及VM119小测试；未调用真实GitHub写入、设置变量、Runner、Git写入或生产上传部署，未碰Next/Runtime环境。不扩入用户本轮新增的旧PHP配方调整，不重跑旧全集或大C构建。

---

## Windows EOL PHP 补丁包阶段 1: 源码编译覆盖 7.0–8.1
**目标**: 让 build_php_legacy.ps1 按分支选择 vc14/vc15/vs16，7.2+ 从 php.net series 清单取依赖，并保持 VC14 shim 隔离。
**成功标准**: 七个分支都能走同一条源码编译路径，补丁可 apply，产物布局与既有包一致。
**测试**: 本地对 6 个上游源码包执行 `git apply --check`，全部通过；契约测试锁定 CRT 映射与 series 下载。
**状态**: 已完成

## Windows EOL PHP 补丁包阶段 2: 补齐 PECL 扩展
**目标**: 源码构建只产出核心 ext/，需按 php-exts.json 叠加与现有包相同的必需扩展。
**成功标准**: 包内 ext/ 同时含核心模块与 imagick/xdebug/redis/mongodb 等 10–13 个 PECL 扩展。
**测试**: workflow 生成 manifest 并按 VERSION/ARCH 过滤；脚本下载、解包、放入 ext/ 并做加载冒烟测试。
**状态**: 已完成（待 CI 实跑）

## Windows EOL PHP 补丁包阶段 3: workflow 路由
**目标**: build-and-dispatch 将 php 7.0–8.1 全部路由到源码编译，不再落到重打包路径。
**成功标准**: build-windows 与 build-windows-legacy 的条件互斥且覆盖 7 个分支。
**测试**: 契约测试逐个断言 7 个版本前缀同时出现在两条 if 中。
**状态**: 已完成

## Windows EOL PHP 补丁包阶段 4: CI 实跑与发布
**目标**: 在 windows-2022 上验证脚本，随后发布 7.1/7.2/7.3/7.4/8.0/8.1 六个补丁版。
**成功标准**: 每个包通过 CVE 退出码校验、扩展加载冒烟测试，并由下游 LIVE 上传。
**测试**: 先单版本 dry-run，再逐版本实跑。
**状态**: 未开始

## 已知事实（本轮核查）
- CDN 上 7.1.33.1/7.2.34.1/7.3.33.1/7.4.33.1/8.0.30.1/8.1.34.1 全部 404，从未构建或发布。
- 已发布的 7.0.33.1 由 VC14 源码路径产出，但当时未叠加 PECL 扩展，ext/ 只有 34 个核心模块，缺 imagick/xdebug/redis 等 16 项必需扩展中的全部。

## Windows EOL PHP 补丁包阶段 4: CI 实跑与发布
**目标**: 在 windows-2022 上验证脚本，随后发布 7.1/7.2/7.3/7.4/8.0/8.1 六个补丁版并重发 7.0.33.1。
**成功标准**: 每个包通过 CVE 退出码校验、扩展加载冒烟测试，并由下游 LIVE 上传。
**测试**: 七个版本在最终提交 cffba82 上全部 dry-run 通过，随后 LIVE 上传。
**状态**: 已完成（7.0.33.1 需 CDN 刷新，见下）

### 实测中暴露并修复的问题
- 新 SDK 的 MSYS2 GNU tar 把 `C:\...` 当远程主机，改用系统 bsdtar 绝对路径。
- configure.js 的编译器表止于 VS2017，v143 编译时 `PHP_COMPILER_ID` 变成版本号，与官方扩展的 `VC15/VS16` 不匹配，全部扩展被拒绝加载；按分支写入官方 ID（MSVC 2015-2022 共享 vcruntime140 ABI）。
- 冒烟测试原只匹配 "Unable to load dynamic library"，而 "Unable to initialize module" 是警告且退出码为 0，导致扩展全未加载仍判绿；改为读取模块列表逐项比对。
- PECL 扩展包自带运行时依赖（`CORE_RL_*.dll`、`yaml.dll`），只复制 `php_*.dll` 会让 imagick/yaml 无法启动；非模块 DLL 放到 PHP 根目录。
- 8.0/8.1 的 `config.w32.h` 只在注释里提及 `PHP_COMPILER_ID`，需按 `#define` 判断。

### 发布结果（2026-10-23）
- LIVE 成功并注册 API：7.1.33.1、7.2.34.1、7.3.33.1、7.4.33.1、8.0.30.1、8.1.34.1。
- 7.0.33.1 已重发（17.4 MB、10 个 PECL 扩展），但 CDN 仍返回旧对象（15.4 MB、last-modified 14:15），`cache-control: max-age=31536000`，需手动刷新该 URL。

## Windows EOL PHP 补丁包阶段 5: 补齐与原版包的内容差异
**目标**: 新包与原版发布包逐文件对齐，而不是只做到"能跑"。
**成功标准**: 原版的每个文件都能在新包中找到；体积差异控制在几个百分点。
**测试**: 下载 CDN 新对象，与原版包做 basename 级差集；CI 内验证 archive 布局、imagick 转换。
**状态**: 已完成

### 根因与修复
- 源码编译只产出 PHP 本身，缺 `dev\*.lib`（phpize 必需）、`sasl2\`、`lib\enchant`、`extras\ssl`、`deplister.exe`、phar 启动器、文档，以及 com_dotnet/oci8/pdo_oci（需 .NET / Oracle SDK）。
  → 改为**以官方 release archive 为基底**，源码编译的二进制覆盖未打补丁的部分，archive 独有扩展保留，archive 的依赖 DLL 不被构建树覆盖。
- apcu、grpc 不在 php-exts.json 里，按分支钉住最后一次可用构建；8.1 从未提供过，不钉。
- 扩展包的运行库（ImageMagick 的 `CORE_RL_*`/`IM_MOD_RL_*`、libyaml 的 `yaml.dll`）必须放在 **php.exe 同级**：Windows 只按 exe 目录和 PATH 解析依赖，放进 `ext\` 会让 php_imagick/php_yaml 报 "The specified module could not be found"。
- `dev\` 只保留 `php<major>.lib`，排除 php-cgi/php-win/phpdbg 及每扩展一个的导入库。
- 扩展包自带的 `config\`（ImageMagick policy/delegates XML）与 `*.ini` 一并入包，`policy.xml` 关系到 ImageMagick 自身的安全限制。
- 7.0 的 imagick 最高可用版本是 3.4.3，PECL 在该版本**不提供 IM_MOD_RL_* coder**（3.4.4 起才有，且只到 7.1），因此 7.0 的图像转换依赖外部 ImageMagick；构建时显式告警，不静默跳过。

### 最终结果（2026-10-23 20:39 UTC 上传）
- 七个分支的"仅原版有"文件集合均为空。
- 体积差异：+0.9%～+3.7%，增量来自源码编译所需的 deps DLL 与扩展自带配置。
- 每包均通过：CVE `_exit(1)` 门禁、扩展全集加载校验、imagick PNG 编解码往返（7.0 除外，见上）。

### 未完成：CDN 缓存
七个 URL 的 CDN 仍返回 18:00 的旧对象（`cache-control: max-age=31536000`），源对象已是 20:39 版本。带查询参数可获取新文件，说明上传正确，需要在 CDN 控制台对下列路径发起刷新：
`windows-packages/x64/php/php-{7.0.33.1,7.1.33.1,7.2.34.1,7.3.33.1,7.4.33.1,8.0.30.1,8.1.34.1}-windows-x64.7z`

---

## Windows v2 普通包与扩展阶段 1: 真实 v1 目录收敛
**目标**: 用公开 Windows v1 包验证普通包到 v2 目录的重排，不再用合成路径代替产品数据。
**成功标准**: 当前 API 中全部 28 个候选包都取得真实 v1 归档并验证成员、入口和文件哈希，完成 v2 7z 回读。
**测试**: Windows VM 按包类别逐批下载、校验 catalog SHA、调用 `repack_v2.py`、比较 v1/v2 文件集合并重新读取输出。
**状态**: 已完成（结构 POC；原生客户端验收另列）

结果记录见 `runtime-windows/WINDOWS_V2_PACKAGE_POCS.md`。实际修正包括 `memcached` 完整树、Adminer/phpMyAdmin 入口去重复前缀、Windows 7-Zip 的 CRLF/反斜杠/空字段/`Attributes=AL` 处理，以及大型树一次解包后逐文件哈希。`typesense` 当前没有 Windows v1 producer，已从 Windows v2 映射移除；macOS 映射保留。

全版本矩阵进一步覆盖公开 v1 API 中 35 个受支持普通包名的 153 个版本条目，2026-10-25 在 Windows VM 得到 153 PASS / 0 FAIL。Rust 与 Simple ACME 的真实 v1 包也纳入并完成；Rust 采用 component overlay，将 cargo/rustc/rust-docs/rust-std 等目录合并为统一 toolchain 根。

## Windows v2 普通包与扩展阶段 2: PHP 第三方模块全量重排
**目标**: 不再把 PHP 独立模块限制为 Redis、Memcache、Memcached、Xdebug、Imagick 五项，而由 split producer 的 `selected_extensions` 决定。
**成功标准**: 当前默认/可选 17 个第三方扩展的 ZIP 命名、共享 DLL、配置候选和 core-consumed host ABI 都能生成独立 7z；PHP 内置扩展仍留在 core。
**测试**: Windows VM 运行 PHP repack 与布局 33 项测试；额外的全模块合成集合覆盖 17 个扩展。
**状态**: 已完成。8.2.35（16 模块）、8.3.36（16）、8.4.27（17）、8.5.12（17）、8.6.0beta3（16）五个 series 均从 PHP-Windows-Portable 的真实 split 产物生成正式 core/module 包，全部模块在 Windows VM 实际加载成功，Uploader 五个 series 均 `validation_complete=true`。逐 series 证据见 `runtime-windows/WINDOWS_V2_PHP_MODULES.md`。

Legacy 分支另行补齐：`build-windows-php-legacy.yml` 现在同时输出 core runtime ZIP、devel pack 与逐扩展 runtime ZIP。5.3、5.4、5.5、5.6、7.0、7.1、7.2、7.3、7.4、8.0、8.1 共 11 个版本完成真实构建、正式包生成、逐模块加载和 Uploader 计划（5.3/5.4 按 x86 身份交付，5.5 为 core-only）。

## Windows v2 普通包与扩展阶段 3: PostgreSQL core 与 pgvector 正式候选
**目标**: 直接将 POC 原生结果绑定到 schema-2 core/module metadata，而不是把诊断归档的 pending 字段当作正式发布包。
**成功标准**: PostgreSQL core 使用公开 EDB 源 ZIP、native core 结果和空 `default_modules`；pgvector 绑定同一 core SHA、42 个 SQL、47 个文件、数据库 query/create/drop 与零残留进程证据。
**测试**: VM 合成回归 16 项 PostgreSQL、12 项 pgvector；真实 VM 生成两份正式 7z/meta。Uploader 合并计划验证 `validation_complete=true`，模块 host SHA 与 core 一致。
**状态**: 已完成（本地正式候选与 Uploader dry-run 消费；同批 host SHA 排序为 PostgreSQL core → pgvector；GitHub 发布入口未启用）

正式候选的 URL、SHA、原生证据和剩余发布边界记录在 `runtime-windows/WINDOWS_V2_POSTGRESQL_PGVECTOR.md`。

## Windows v2 普通包与扩展阶段 4: 全量原生与发布接线
**目标**: 对 35 个已生成普通包完成客户端安装、服务启动、请求和卸载验收，并接通 Windows v2 Builder artifact、reviewed bundle 和 Uploader workflow。
**成功标准**: 普通包 native 结果绑定 exact artifact SHA；PHP 17 个扩展在本机实际加载；Uploader dry-run 先完成，LIVE 仍需单独许可。
**测试**: Windows 11 VM/客户端原生验收、Uploader workflow 契约、真实 reviewed bundle 回读。
**状态**: 进行中（35/35 普通包目录与入口验证、153/153 普通包版本矩阵 PASS；PHP 8.2–8.6 共 5 个 series、87 个正式包全部模块加载成功；Builder Windows v2 回归 124/124 通过，全量 513 项中 16 项失败已在 HEAD 基线逐一复现、与本次改动无关；Uploader Windows v2 14/14 通过（全量 140 项中 2 项为 macOS `/var` 与 `/private/var` 路径差异）；客户端安装/服务/HTTP/卸载、真实 reviewed bundle 和 LIVE 发布接线未完成）

补充：Adminer/phpMyAdmin 使用原生 Windows PHP v2 CLI 执行语法检查；Maven 使用精确 v1 OpenJDK 27 运行版本检查。其余可执行包从 v2 产物直接跑版本/帮助命令。经明确授权，`frp` 的 PUA 测试仅在专用目录临时排除 Defender，完成后已删除并回读确认。

全量测试的最终证据回传 `~/Downloads/ServBay-windows-v2-evidence-20261025-full.zip`，148,727 字节，SHA256 `1d19ecdb72ab77f7fc9d3f59221756d56d9969bcdb971a898841f3bd6038f44d`，含 v1 目录快照、35 包 POC、35 包原生/语法结果、153 版本矩阵、PHP 8.2–8.6 与 legacy 5.3–8.1（含 5.3/5.4 x86）的正式 meta/加载结果/finalization/split manifest、PostgreSQL 18.6 + pgvector 正式证据和验证摘要。早先的 28 包版本 `ServBay-windows-v2-evidence-20261025.zip` 仍保留，但已被前者取代。

Legacy PHP：`build-windows-php-legacy.yml` 已改为同时产出 v2 split 资产。5.3–8.1 共 11 个版本生成 11 个 core 包和 107 个模块包，全部模块在 Windows VM 加载成功，Uploader 计划均为 `validation_complete=true`。5.3/5.4 按 x86 身份交付（包内 `php.exe` PE Machine 实测 0x14c），5.5 为 core-only；详见 `runtime-windows/WINDOWS_V2_PHP_MODULES.md`。

另新增手工 `build-windows-v2-package.yml`：默认以已核验的 Adminer v1 输入演示完整下载、SHA 校验、`repack_v2.py`、artifact 上传流程；任意已支持包可显式提供公开 URL/SHA/version 复用。该入口默认不调用 Uploader、不建立 Release、不修改公共 catalog，真实运行仍需单独触发。
