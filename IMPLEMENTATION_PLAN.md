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

Legacy 分支另行补齐：`build-windows-php-legacy.yml` 现在同时输出 core runtime ZIP、devel pack 与逐扩展 runtime ZIP。5.3、5.4、5.5、5.6、7.0、7.1、7.2、7.3、7.4、8.0、8.1 共 11 个版本完成真实构建、正式包生成、逐模块加载和 Uploader 计划（5.3/5.4 为 x86 内容、按 x64 身份发布，5.5 为 core-only）。

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

全量测试的最终证据回传 `~/Downloads/ServBay-windows-v2-evidence-20261025-full.zip`，149,141 字节，SHA256 `a0efad3896f092f64039391f2cd64d43c2a9d3bd3ba35fe9ca2d31e01cf2659c`，含 v1 目录快照、35 包 POC、35 包原生/语法结果、153 版本矩阵、PHP 8.2–8.6 与 legacy 5.3–8.1（5.3/5.4 为 x86 内容按 x64 身份发布）的正式 meta/加载结果/finalization/split manifest、PostgreSQL 18.6 + pgvector 正式证据和验证摘要。早先的 28 包版本 `ServBay-windows-v2-evidence-20261025.zip` 仍保留，但已被前者取代。

Legacy PHP：`build-windows-php-legacy.yml` 已改为同时产出 v2 split 资产。5.3–8.1 共 11 个版本生成 11 个 core 包和 107 个模块包，全部模块在 Windows VM 加载成功，Uploader 计划均为 `validation_complete=true`。5.3/5.4 为 x86 内容、按 x64 身份发布（包内 `php.exe` PE Machine 实测 0x14c），5.5 为 core-only；详见 `runtime-windows/WINDOWS_V2_PHP_MODULES.md`。

另新增手工 `build-windows-v2-package.yml`：默认以已核验的 Adminer v1 输入演示完整下载、SHA 校验、`repack_v2.py`、artifact 上传流程；任意已支持包可显式提供公开 URL/SHA/version 复用。该入口默认不调用 Uploader、不建立 Release、不修改公共 catalog，真实运行仍需单独触发。

---

# 2026-10-26 PHP 8.6.0RC4 完整发布补跑

**用户要求与授权**：修复 RC 检测后提交、推送并线上补跑；用户明确指出仅检查清单并未注册 API，不满足完成标准。用户进一步明确选择“这次一并补齐 Windows v2”。本轮目标是 PHP 8.6.0RC4 的编译、真实验证、正式上传、API 登记和 CDN 字节回读，不扩展到其他软件包，不修改既有发布门禁。

**当前记录**：Utility 修复提交 `dd44550`、Builder 修复提交 `21b4ee4` 已推到 GitHub。此前运行 `40268009854` 和 `40268199101` 仅检查版本，不能作为发布完成证据。现已开启 LIVE orchestrator：`40272343132` 为 macOS v1 双架构和 Windows v1 x64，`40272350331` 为 macOS v2 双架构；二者 `upload_dry_run=false`。Windows 原始 producer 为 `ServBay/PHP-Windows-Portable` run `40272362001`、attempt 1、commit `5f5af83f797ae89a7187ddca87492cf8064d68d7`，明确构建 `8.6.0RC4`、x64、NTS。不得重复触发这些运行。

**架构**：复用 Portable 原始 core/逐扩展 artifact、已有 collector 和 Builder 的 `prepare_php_payloads` / `finalize_php_payloads`；新增的薄入口只串联输入核验、原生执行和正式产物导出。Uploader 使用现有 `runtime_version=v2, os=windows-assets` 路由，不增加上传实现。

**全局约束**：
- 不使用 merged release ZIP 逆拆 core/第三方扩展；必须消费指定 producer run/attempt 的原始 core 和全部 selected extension artifacts，并核对版本、producer、文件和 SHA。
- Collector 复用 Portable 已存在的 `b614c80` 实现；该提交比远端 main 多 1 个提交，仅涉及 split collector/Release 接线、文档和测试。纳入本轮独立审查后才能推送供 CI 固定读取，不另复制一份 collector。
- CI 必须使用原生 x64 Windows runner（与 8.6 VS18 工具链匹配），不得以 ARM64 VM 的 x64 仿真替代。不得在开发机安装系统级 VC++ runtime。
- 先从原始 core 实际执行 `php -n -i` 取得 ABI 和版本，再 prepare；逐个加载 selected 的全部第三方 DLL，Xdebug 使用 zend_extension；有 Imagick coder 时必须实际查询 PNG。
- finalize 后从正式 core/module 7z 重新解包并执行同样原生验证；只在全部通过后生成正式 ZIP 和可审计结果。不得用返回码 0、文件存在或 mocked native 结果替代线上实跑。
- RC4 core `build_revision=0`；Windows PHP8.6 beta3 的同版本模块已占用 revision 0，因此 RC4 模块必须显式使用经 API 确认未占用的新 revision（拟 1），不得覆盖已有内容。`msgpack-3.0.0` 等真实 ref 保持原值，仅可去掉前导 v。
- 保留现有 Uploader 精确身份、manifest、逐通道回读 SHA、模块先于 core 登记等门禁；先检查正式发布计划，再按已有授权 LIVE 发布。不能修改 gate/CI 判定来绕过失败。
- 不读真实 .env 或输出 Secrets；不清理、回滚、暂存或提交他人改动。新增临时目录必须是本次创建且有明确归属，不对既有输入/输出树递归删除。
- 用户已给出本任务提交、推送和正式发布授权；父负责精确暂存和 Git 操作，worker/reviewer 不提交、不推送、不另起 subagent。

## 阶段 RC4-1: 原生 Windows v2 CI 入口与离线回归
**目标**: 新建 `runtime-windows/ci/php_v2_pipeline.py`、`.github/workflows/build-windows-v2-php.yml` 和 `tests/test_windows_v2_php_pipeline.py`，复用三种同类实现：`repack_php_v2.py`、既有 `build-windows-v2-package.yml` 和 Portable `collect_split_assets.py`；旧 `/tmp/run_php_series_pipeline.py` 只作为流程参考，不直接执行其删除逻辑。
**成功标准**: 入口显式接受 producer run/attempt/version/module revision；workflow 获取精确 producer 及原始 artifacts、固定 collector commit，在原生 x64 runner 上执行薄入口，输出正式 ZIP、完整 native 证据和 manifest；不自动发布其他包，不新设生产开关。新文件和 collector 提交经一次独立影响锥审查通过，最多两轮。
**测试**: 先写失败用例并见证 RED，再实现 GREEN。测试真实外部行为：版本/producer/ABI 不一致、缺少 selected 扩展、任一 DLL 加载失败、PNG 失败、正式归档再加载失败必须阻断成功输出；revision 正确进入模块 meta 和 core default_modules；禁止重用既有非空输出；离线环境禁止网络和 Windows 二进制执行。运行相关既有 repack/collector 测试；不运行真实编译的全套测试。
**状态**: 已完成。独立影响锥审查 Spec PASS / Quality PASS，覆盖20个相关文件；Builder 51项与Portable 64项离线测试通过，真实7z合成正式ZIP已被既有Uploader消费为17包、模块revision1、core revision0。共享旧调用者额外5项通过；6个逐项内存突变均被识别。尚不代表原生CI完成。

## 阶段 RC4-2: 当前 producer 的原生 CI 验证与正式产物
**目标**: 在上游 `40272362001` 完成后，使用经审查并推送的入口消费其 exact attempt；不重编已成功的 RC4 原始输入。
**成功标准**: Windows x64 上原始 ABI、准备后的全部模块、Imagick PNG、finalize 和正式 7z 再加载全部成功；正式 ZIP/每个归档 SHA/producer/native 结果可回读，所有失败单元明确报告，不将运行中 job 当作完成。
**测试**: GitHub 原生 runner 实跑；核对 artifact ZIP digest、内部 package SHA、正式 meta/manifest 及实际 native JSON，确认版本为 RC4、模块数与 selected 一致。
**状态**: 已完成。干净原生 x64 run `40281499583`（head `b55f2b4de8536c5b8c51b1694e5361cf1ab68a50`）成功，原producer仍为 `40272362001` attempt1，不重编。artifact `12633375467` ZIP SHA `c8c656d6f884864c3e0069e2a7feb05a3e652e0ab3fdd4fa62d7a7e55e9d5e16`；正式ZIP SHA `604e3ceb74411cc803d510c6576f86a48c89c024af4adf93f5753aaa772b992d`，core SHA `f642c694e6584be5bb7e6ce39004d58f1ca626ba519be71d54c8fc4e132fa47d`。独立产物审查 Spec PASS / Quality PASS：17份metadata/归档、16份manifest/默认引用、两轮共32条原生加载及Imagick PNG全部通过；导出无全量phpinfo或凭据模式。此状态不代替LIVE/API/CDN验收。

06:20 UTC：该原生run的AMD64及16模块两轮加载、PNG、正式7z回读全部通过，但native诊断保存全量phpinfo，父在导出复核中确认包含GH_TOKEN环境项，因此该诊断artifact `12631365752` 不可交付/发布。wrapper正改为PHP环境明确allowlist、只导出解析后的版本/ABI、失败也不回显原始phpinfo，并补哨兵秘密回归；之后用同producer再生成干净产物。已按用户删除规则请求仅删除这份artifact，待明确答复；不记录或输出令牌值。

06:25 UTC：用户明确允许删除该artifact；DELETE已成功，并回读run的artifact列表为空。原始producer和已发布包保持不变。已告知关联UPSTREAM_DISPATCH_PAT需在GitHub轮换，删除产物不等于令牌失效；未读取/输出令牌值。诊断修复完成两条哨兵RED/GREEN、9项管线回归；第2轮只读增量审查Spec PASS / Quality PASS，独立9项通过，环境继承/错误回显/原始phpinfo导出三个逐项变异均被识别。后续仍以干净原生CI为验收证据。

## 阶段 RC4-3: LIVE 发布、API 与 CDN 回读
**目标**: 把 Windows v2 正式 ZIP 交给已有 windows-assets 路由，并持续收口已启动的 macOS v1/v2、Windows v1 发布。
**成功标准**: 全部目标的 Uploader LIVE 步骤成功；公开 package API 的 PHP8.6 为 `8.6.0RC4`，模块记录与 core default_modules 的 version/revision/hash/ABI 一致；每个目标下载 URL 的真实字节 SHA 等于 Builder/元数据/API。留存精确 run/attempt/commit、注册结果和 hash，不以 check-only/CI 绿灯代替注册。
**测试**: 对精确 artifact 运行现有 Uploader plan/dry-run，随后 `dry_run=false`；查询真实 API、下载 CDN 全部 core 和引用模块验证 SHA。对失败仅补对应单元，禁止无关全量重跑。
**状态**: 进行中。06:03 UTC已确认公开API的macOS v1 arm64/x86_64、Windows v1 x64全部为RC4；三包API hash、Builder package SHA和CDN实际下载SHA一致。macOS v2已注册的8模块CDN SHA一致；macOS v2 core/其余模块和Windows v2尚未完成。

## 阶段 RC4-M: macOS v2 的 PHP 8.6 扩展兼容修复
**目标**: 修复本次真实构建发现的 9 个扩展、双架构共 18 个失败单元：memcached 3.4.0、APCu 5.1.28、Phalcon 5.23.0 的旧补丁不再适用；SQLSRV 5.13.3、IMAP 1.0.3、SCWS 1.2.4-pre、Xdebug 3.4.3 使用了已移除的 INI 宏；PDO_SQLSRV 5.13.3 和 Swoole 6.1.7-88fc55029c 使用旧的 stream/error API。保留已通过的核心包和其余 4 个模块，不减少模块、不修改版本、不绕过编译或 finalize 门禁。
**成功标准**: 按 exact 上游源码和 PHP 8.6.0RC4 实际头文件修复 PHP8.6 专属补丁/接线；worker 仅负责 `runtime-v2/scripts/build_package.sh` 的 PHP8.6 扩展部分、必要的 `runtime-v2/patch/php8.6/*` 和对应行为测试，保留该脚本中并行提交 b67168ad 的 PostgreSQL/ICU 修复。新增增量经独立只读影响锥审查（最多两轮），线上双架构 13 个模块和 final core 均成功后进入发布。
**测试**: 全量逐 job 日志分类、exact 源码补丁应用和必要的 C/C++ 编译/行为回归；新 recipe 提交在 GitHub runner 上实际编译，最终 core/module 原生加载和 ABI 检查全部通过。旧 SHA 的 rerun 不代表修复代码已验证；不能拿 candidate core 代替正式 core。
**状态**: 进行中

代码审查已在最多两轮内通过：第1轮独立找到SQL两opener的未命名context、Swoole剩余21处参数宏和实际启用php84 curl两处INI调用，共3个P1；原worker修正后，第2轮独立Spec PASS / Quality PASS，3个delta测试及26/26逐处变异通过。完整源码测试8/8及既有recipe18/18通过；保留全部模块与原行为，待新提交在双架构CI实际编译/加载。

修复提交 `b910cea7110059bb047de99db5d5566b4ac1133d` 已推送；精确目标macOS v2 PHP8.6RC4、双架构LIVE orchestrator `40279549768` 于06:23 UTC启动，消费已发布的兼容模块，补齐失败模块及正式core。源码及测试文件whitespace检查通过；作为数据保存的unified patch上下文含原始缩进/空白，Git的通用whitespace扫描会报这些上下文，不修改原始补丁上下文或检查配置。

## 阶段 RC4-N: 原生补跑的剩余 API 与基座 revision 闭包
**目标**: 根据真实run `40279549768` 的新增编译证据，补齐 APCu `apc_persist.c` 两处移除的 `EMPTY_SWITCH_DEFAULT_CASE`、Swoole `swoole_process_pool.cc` 移除的 `zend_parse_parameters_throw`，并有范围核对两模块实际源码与PHP8.5→8.6头的同类移除项。另按当前实际基座2.1.3重新生成PHP8.6全套模块revision1，core仍revision0；不混用已发布的2.1.2模块，不覆盖已有revision0。
**成功标准**: 仅 APCu/Swoole 专属补丁与真实源码回归、两个macOS workflow的显式 `macos_module_revision`/`module_revision` 输入、`module_pipeline.py` 的预期revision数据传递及对应测试发生变化。默认0和旧不可变plan保持原行为；新plan冻结每core全部模块name→revision，finalizer从预期plan绑定，不能从可用产物随意选择revision；base/producer/精确身份/就绪保护不变。新输入显式为1时，同base2.1.3的26个模块与两个core正式完成；独立增量影响锥审查最多两轮，不重审上一阶段无delta的补丁。
**测试**: 两新增API真实源码/RC4头RED→GREEN及逐调用点变异；新模块revision1进入模块及依赖、core默认引用与SHA，旧默认0/旧plan不变、错误revision不就绪；离线测试禁网、不跑真实phpize/configure/make/宿主PHP二进制。随后GitHub原生双架构实际编译及加载、Uploader plan/LIVE、公开API和CDN字节SHA闭环。
**状态**: 进行中

**新增运行事实**：旧已发布8模块manifest的base为2.1.2，当前run候选/plan的base为2.1.3（arm64 Runtime SHA `d2940bb56fafe67f4f6e7982cf7bdc3c716bc81cf20826774d0e5df11009a547`、Devlib SHA `1f033b80c22fc03e14ac32f030caca3f5036e84eefff19bed8635908689527a7`；Intel以当前plan为准）。因此旧8模块在新plan中不再兼容，state也不会冒用旧producer。两个新失败由job `121432350396`、`121432350405` 实际日志确认；其余已成功单元证明上一轮对应修复，但不能代替新的revision1整套产物。

本阶段代码第1轮独立Spec PASS / Quality PASS，覆盖23个相关文件；独立21项管线/producer、2项新增真实源码测试通过，4个revision、1个诊断和3个源码调用点变异均被识别，Actionlint语法与表达式通过。无需第2轮；真实原生CI和Phalcon诊断仍未完成。

修复提交 `3472f71e53ef38e20f1defe9dac2ddc802ed19ca` 已推送，精确目标run `40283045159` 于06:59 UTC启动，core revision0、macOS module revision1，双架构基座2.1.3。

07:23 UTC：本run双架构candidate core成功，26模块已完成21成功/4失败，Intel MongoDB仍运行。APCu双架构已通过；新增真实失败仅Swoole `swoole_coroutine.cc:975/976` 的 `EG(in_autoload)` 字段移除，以及Phalcon五个SessionHandler类缺少 `create_sid()` 的启动警告。Phalcon此次严格诊断门禁已准确输出原因，不能屏蔽。worker仅补现有Swoole/Phalcon专属patch与真实行为测试；第二轮独立审查覆盖本轮delta。required default模块仍为memcache/memcached/redis，Swoole/Phalcon为可选模块；待final core产生后可按原契约先发布有效core/成功模块，再从公开schema2 host请求仅补剩余模块，不伪造candidate来源或重编成功core。

07:07 UTC：Windows同一正式ZIP已上传既有Release `windows-v2-php-20261026` 的新增asset `php-8.6.0RC4-formal.zip`，未覆盖旧asset。Release digest和实际下载82,101,213字节SHA均与已审产物一致；Uploader dry-run `40283795396` 已启动，待计划门禁通过后LIVE。

07:13 UTC：Windows v2 dry-run `40283795396` 校验通过，LIVE `40283915516` 已完成：`published=true`、17项注册、OSS/R2共34项后端SHA回读成功、无未执行项。公开package API已为RC4/core revision0、16个默认引用与正式manifest一致；extension API的全部16项revision1/version/hash/manifest均匹配，core与16模块共17个CDN真实下载SHA全部一致。证据集中在 `/tmp/servbay-php-rc4-release-d8heet_i/windows-public-verification.json` 和 `windows-publication-result.json`。Windows v2已完成，剩余macOS v2双架构。

07:32 UTC：macOS run `40283045159` 已结束，22模块/两个final core成功，仅Swoole/Phalcon双架构4项失败。父独立下载24个正式artifact，逐份ZIP digest、package SHA、模块manifest SHA、producer/base及core三个default_modules的revision1/version/SHA一致。core arm64 SHA `b35083cf69644ac8006a4cfe0f2e4fd58ee95b855e218b19940bf2abbc28a63d`，Intel SHA `7b8b111aca39e39dc4bd2375f4140d1549ee7838279ca93fae6f8dab031e8058`。Uploader dry-run `40286057541` 的24项计划和这些产物逐项一致且通过；LIVE `40286253700` 执行中。自动dispatch `40285811425` 仍因公开下载基址缺失在plan前失败，手动传入已有CDN地址，不修改变量。

07:38 UTC：macOS LIVE `40286253700` 完成，`published=true`、24项注册、OSS/R2共48项后端SHA回读成功，无未执行项。公开API两个core均RC4且manifest/default模块revision1匹配；22扩展的version/revision/hash/manifest匹配，全部24个CDN字节SHA一致。由真实公开core记录生成两个catalog-host请求，后续仅补Swoole/Phalcon，不重编core。至此六个core目标均已LIVE/API/CDN闭环，不能据此把剩余4模块冒称完成。

本阶段最后源码delta：Swoole原核心autoload递归表改用内嵌 `EG(autoload_current_classnames)`，保留原删除当前类guard及协程队列/yield/resume；270个选定源的EG/CG成员链语法核对通过。Phalcon两个根类AbstractAdapter/Noop的方法表共用官方 `php_session_create_id(NULL)` 处理器，具体类继承并实现SessionIdInterface；真实头/方法表/生成器委托/参数和异常行为13项源码测试通过，四处逐项变异被识别。新增原生SID功能用例检查四具体类SID与active handler回调，恢复session/INI；父仅对显式已验证8.6 host_version接线，旧5.6/7.x不会让旧parser读取新PHP文件。原Reflection/任何stderr/名称版本检查保持；外部进程失败/诊断及旧host排除10项离线回归通过，4项父接线逐点内存变异被识别。第二轮最后一次delta审查进行中，真实双架构编译和SID功能仍待验。

07:41 UTC：第2轮最终delta独立审查Spec PASS / Quality PASS，覆盖约19个相关文件，独立10项producer/4项源码探针、成功返回及非Phalcon排除、4个接线缺陷变异均通过。已核对真实RC4 lookup guard、官方SID生成器和用户handler回调、两个根类/四具体类及已验证host_version来源。阶段N两轮代码审查收口；下一步只跑四个剩余原生模块及API/CDN，不重审无变动代码。

08:34 UTC：最终补丁提交 `b5b03d5cddb0554e213f11fada3b6a115f4a07a8` 已推送。首次选择orchestrator的host请求run `40293024382`/`40293030290` 未启动编译：实际orchestrator有意不向reusable workflow传递host_request；改用已有直接入口build-macos.yml，arm64 `40293140056`、Intel `40293144863`，均为同一b5b03d5源码、实际公开RC4 core及module revision1。未改workflow或来源门禁。

08:43 UTC：两个直接入口仍未编译；依据四单元已discovered/failures1且无lease的实际state，再发 `40293585909`/`40293591061` 后仍跳过，遂停止重复调度。独立只读诊断确认：state提交ac1b099精确预留ARM两模块，5105e9精确预留Intel两模块，矩阵各至少2，正常设置outputs且无secretmask；module-build的无状态函数if被skipped core祖先的隐式success阻断，随后collector释放lease但未增加失败计数。其他独立入口复用同一workflow，无等价的现成仅补模块入口。父已准备未应用的一行patch `/tmp/servbay-php-rc4-release-d8heet_i/independent-host-workflow.patch`：显式要求!cancelled、plan/module-plan成功、count非零，语法检查通过。因宿主AGENTS.md:157要求CI判定变更须用户批准，已提出具体授权请求，待答复前不改项目workflow、不继续调度。

08:56 UTC：用户明确允许应用该CI条件修复、提交推送并补跑四项。仅module-build.if改为显式未取消、两个plan成功和count非零；不修改预留/失败阈值、原生加载/SID、SHA或发布检查。用户另确认正常merge授权；此前安全修复合并b55f2b4已经完成，本轮当前分支仅落后自动state，按实际差异快进同步，没有重写历史或重复制造merge。

09:21 UTC：用户质询重复记录、显示名及双CDN，期间只读核对、未新增发布。生产数据库4141/4143和4142/4144除ID/时间外所有字段相同，无数据库外键引用；建议保留先登记4141/4142、删除后登记4143/4144，不删除共同CDN文件；未获删除许可，未执行删除。v1显示名来自Uploader php_meta的完整版本标签，v2按系列PHP8.6，尚未修改名称或注册逻辑。

此前公共CDN下载命中阿里云ESA，Uploader OSS/R2后端回读不能代表CF CDN；已明确纠正该证据边界。本轮固定CF IP并保持原Host/SNI、绕过本机代理，确认server=cloudflare、CF-Ray LHR，完整GET六个core，全部SHA与ESA/数据库/Builder一致，CF均HIT。原URL和查询参数URL的ESA下载一致；包内版本/Windows二进制标记为RC4。双链路证据保存于 `/tmp/servbay-php-rc4-release-d8heet_i/cloudflare-cdn-six-core-verification.json`、`cdn-content-and-cache-evidence.json`；仅代表已测ESA及CF LHR路径，不声称逐个全球PoP。双侧核对完成后接续已批准的四模块构建，后续仅发布尚未登记的四扩展，不重新注册core。

Phalcon在该run双架构均已编译、链接并通过dlopen，但 `verify_php_payload` 读取stdout时仅报JSONDecodeError，真实启动诊断被JSON错误掩盖。父仅给PHP验证命令显式设置 `display_errors=stderr`，沿用原有“任何stderr即失败”条件，不放宽解析或忽略诊断；新增外部行为回归先RED（只能看到JSON错误）、后GREEN（读到诊断原因且仍阻断），ModuleProducerTests 8/8离线通过。此两文件增量一并纳入本阶段影响锥审查；后续真实CI用准确诊断继续定位Phalcon，不能把加载成功误报为完整验证成功。

**Git与Windows补充**：安全修复提交 `d0d8191` 推送时遇到自动build-state提交分叉；误用旧head的run `40280094578` 已取消，回读无artifact。用户回复“继续”后，按所提普通merge保留安全修复与自动状态，合并提交 `b55f2b4` 已推送，未重写历史。干净Windows原生验证于06:43 UTC从包含修复的main重新启动；仍消费原producer `40272362001` attempt1。

**执行记录**：2026-10-26 05:20 UTC，macOS v1 双架构正式 artifacts 已生成；其 ZIP/package SHA 已独立下载验证：arm64 package `e02bda6e6d92671819beb0508727f850bab9067e61fa91b171016441196cf0eb`，x86_64 package `13003c398ab4b2b7163fcd32b872490bc0f4050c1ad647f39616e74df88a162f`。macOS v2 arm64 仅 candidate core，不是可发布 final core；Intel core、Windows upstream/扩展仍在进行。Windows v1 API 的正确架构参数为 `x64`，Windows v2 归一化为 `x86_64`。Windows v2 beta3 当前 API 已正式发布，但不能将旧包验证结果借给 RC4。

**执行预检与裁定**：阶段1的 formal ZIP/证据是阶段2验证输出、阶段3发布输入，路径/版本/revision 约定一致；阶段2、3不改打包规则，不与阶段1冲突。每阶段内部测试与成功标准一致。此计划按用户指定追加于现有 `IMPLEMENTATION_PLAN.md`，本节兼作进度账本，不另建过程目录；worker 只读取本节、按明确文件 ownership 工作。用户已明确要求直接完成本轮工作，不再另行询问执行方式。父独立审查增量并管理提交/推送/发布。

**05:45 UTC 补充**：Windows v2 真实 repacker/7z 离线集成复现了非零模块 revision 无法进入 core default_modules 的数据传递缺陷；阶段1 ownership 扩展到 `runtime-windows/repack_php_v2.py` 和 `runtime-v2/ci/core_manifest.py` 的显式 revision 传递及相关回归，保留精确身份与就绪检查。公开 Windows8.6 的16模块均为 revision0，未发现revision1。macOS v2 原运行完成了8个模块产物，其自动 Uploader `40274235486` 因缺少公开下载基址输入而在 plan 前退出，未执行上传/注册；按实际 API 的 `https://dl-v2.servbay.com` 显式参数补跑，不修改生产开关或仓库配置。新的 recipe 提交不能冒用旧 candidate 的 producer 身份；现有流水线将 core 与同 run/attempt 绑定，没有可直接复用旧候选的入口，必要时仅重建 PHP8.6 目标 core，并通过 catalog 复用已发布、ABI 兼容的成功模块。

**06:02 UTC 补充**：Windows producer `40272362001` 和 v1 Builder `40272343132` 均已成功。Windows v1 artifact `12630207514` 的ZIP digest和内部package SHA已独立回读，package为 `c15a9602c37c6b2b811e4b4a064de4f10a9c3e87dbfb44f316398720b6ab0643`。macOS v2 已成功的8模块经 Uploader dry-run `40275955275` 与 LIVE `40276237888` 注册，8个CDN下载均与API SHA一致。macOS v1经 LIVE `40276360977`、随后原始orchestrator自动 LIVE `40276547013`（含Windows v1）完成登记；原始数据库已只读核对RC4及hash。旧legacy POST未清理包列表缓存，故公开GET仍返回beta3；仅清除v1 arm64/macOS、x86_64/macOS、x64/Windows的new/installed共6个精确包列表键，再回读公开API，不改数据库、其他缓存或部署代码。
