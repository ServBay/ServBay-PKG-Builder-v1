# Windows v2 普通包真实 POC

核查日期：2026-10-25。目录来自公开 Windows v1 API 的真实响应；普通包矩阵按 35 个受支持名称选取当时语义版本最高的 x64 条目，不用测试夹具代替下载包。POC 验证 v1 归档到 v2 目录、路径安全、文件集合、逐文件 SHA 和产物 7z 回读；另对可执行入口做了 VM 启动验证。

| 包 | v1 版本 | 结果 | v2 根目录 / 入口 | 文件数 |
|---|---:|---|---|---:|
| adminer | 6.2.0 | PASS | adminer / `adminer/index.php` | 1 |
| ai-gateway | 1.31.0 | PASS | ai-gateway / `ai-gateway/bin/servbay-ai-gateway.exe` | 1 |
| apache | 2.4.69 | PASS | apache / `apache/bin/httpd.exe` | 2776 |
| bun | 1.4.4 | PASS | bun / `bun/bin/bun.exe` | 1 |
| caddy | 2.11.7 | PASS | caddy / `caddy/bin/caddy.exe` | 3 |
| cloudflared | 2026.10.3 | PASS | cloudflared / `cloudflared/bin/cloudflared.exe` | 1 |
| composer | 2.10.3 | PASS | composer/2 / `composer/2/bin/composer` | 1 |
| deno | 2.9.8 | PASS | deno / `deno/bin/deno.exe` | 1 |
| disk-cleaner | 1.1.0 | PASS | disk-cleaner / `disk-cleaner/bin/servbay-diskcleaner.exe` | 1 |
| frp | 0.71.1 | PASS | frp / `frp/bin/frpc.exe` | 5 |
| git | 2.56.0 | PASS | git / `git/cmd/git.exe` | 9611 |
| go | 1.27.3 | PASS | go/1.27 / `go/1.27/bin/go.exe` | 15657 |
| mailpit | 1.32.0 | PASS | mailpit / `mailpit/bin/mailpit.exe` | 3 |
| mariadb | 13.1.1 | PASS | mariadb/13.1 / `mariadb/13.1/bin/mysqld.exe` | 651 |
| maven | 3.9.15 | PASS | maven / `maven/bin/mvn.cmd` | 96 |
| meilisearch | 1.55.0 | PASS | meilisearch / `meilisearch/bin/meilisearch.exe` | 1 |
| memcached | 1.6.45 | PASS | memcached / `memcached/bin/memcached.exe` | 7 |
| minio | 2025.10.15 | PASS | minio / `minio/bin/minio.exe` | 1 |
| mongodb | 8.3.13 | PASS | mongodb/8.3 / `mongodb/8.3/bin/mongod.exe` | 7 |
| mongosh | 2.13.0 | PASS | mongosh / `mongosh/bin/mongosh.exe` | 8 |
| mongotools | 100.19.1 | PASS | mongotools / `mongotools/bin/bsondump.exe` | 13 |
| mysql | 9.7.4 | PASS | mysql/9.7 / `mysql/9.7/bin/mysqld.exe` | 242 |
| nginx | 1.31.8 | PASS | nginx / `nginx/sbin/nginx.exe` | 32 |
| ngrok | 3.39.13 | PASS | ngrok / `ngrok/bin/ngrok.exe` | 1 |
| node | 26.12.1 | PASS | node/26 / `node/26/bin/node.exe` | 1938 |
| ollama | 0.40.10 | PASS | ollama / `ollama/bin/ollama.exe` | 36 |
| openjdk | 27.0.1 | PASS | openjdk/27 / `openjdk/27/bin/java.exe` | 560 |
| openssl | 3.5.0 | PASS | openssl / `openssl/bin/openssl.exe` | 152 |
| phpmyadmin | 5.2.3 | PASS | phpmyadmin / `phpmyadmin/index.php` | 4314 |
| pinggy | 0.6.1 | PASS | pinggy / `pinggy/bin/pinggy.exe` | 1 |
| python | 3.15.1 | PASS | python/3.15 / `python/3.15/python.exe` | 4838 |
| redis | 7.4.11 | PASS | redis/7.4 / `redis/7.4/bin/redis-server.exe` | 16 |
| ruby | 4.0.9 | PASS | ruby/4.0 / `ruby/4.0/bin/ruby.exe` | 18253 |
| rust | 1.99.1 | PASS | rust / `rust/bin/rustc.exe` | 66117 |
| simple-acme | 2.4.1 | PASS | simple-acme / `simple-acme/wacs.exe` | 97 |

结果：当前 Windows v2 支持的 35 个普通包名称全部完成真实 v1→v2 7z 生成与回读。

35 个包的 CLI/可执行入口均已在 Windows 11 VM 从 v2 产物或字节等价的精确定位树启动并核对版本或语法：Apache、Bun、Caddy、Cloudflared、Composer、Deno、frp、Git、Go、Mailpit、MariaDB、MeiliSearch、Memcached、MinIO、MongoDB、MongoShell、MongoDB Database Tools、MySQL、NGINX、Node.js、Python、Redis、Rust、Simple ACME、ngrok、Ollama、OpenJDK、OpenSSL、Pinggy、Ruby、AI Gateway、DiskCleaner；Adminer 与 phpMyAdmin 使用原生 Windows PHP v2 CLI 执行 `php -n -l`；Maven 使用同一批精确 v1 SHA 的 OpenJDK 27 运行 `mvn -version`。

其中 28 个包的结果保存在 `native-results.json`，后补的 7 个（nginx、redis、python、node、composer、rust、simple-acme）保存在 `native-results-extra.json`，均在 2026-10-25 全量证据包内。`simple-acme` 启动并报告版本 `2.4.1.2444`，但启动日志同时出现 `DigitalOcean.API` 插件类型加载错误；主程序入口通过，该插件功能未验证。

## 全版本普通包矩阵

2026-10-25 的 Windows v1 API 共返回 37 个包名、179 个版本条目。其中的 153 个普通包版本由 `repack_v2.py` 负责；PHP 16 个版本和 PostgreSQL 10 个版本使用专用 producer，不计入普通包矩阵。

Windows VM 对 153/153 个普通包版本逐一执行了：API/catalog SHA 校验、下载、完整 listing 路径与链接校验、v1→v2 目录重排、文件集合比对、文件 SHA、7z CRC 测试和输出 metadata 生成。矩阵用压缩级别 1 加快结构验证；实际生产 repacker 默认仍为级别 7。最终结果 153 PASS、0 FAIL。这个矩阵覆盖 Go 1.11–1.27、MariaDB 10.4–13.1、MySQL 5.1–9.7、OpenJDK 7–27、Node.js 12–26、Python 2.7/3.5–3.15、Ruby 2.4–4.0、MongoDB 4.4–8.3 等历史 series，而不是只抽最新版。

## 覆盖边界与配套结果

- PHP：8.2.35、8.3.36、8.4.27、8.5.12、8.6.0beta3 五个 series 已完成真实 split 打包、正式 meta、逐模块加载和 Uploader 消费；每个 series 的 core SHA、ABI、工具链、模块集合与结果见 [`WINDOWS_V2_PHP_MODULES.md`](WINDOWS_V2_PHP_MODULES.md)。
- Legacy PHP：5.3–8.1 共 11 个版本已由 legacy workflow 输出 core/逐扩展 split 资产并完成 v2 打包、逐模块加载与 Uploader 计划。5.5 为 core-only；5.3/5.4 按 x86 身份交付（上游只有 x86 构建，v1 的 x64 路径实际也是 x86 内容），其余为 x64。详见 [`WINDOWS_V2_PHP_MODULES.md`](WINDOWS_V2_PHP_MODULES.md)。
- PostgreSQL：core 18.6 与 pgvector 的正式候选、原生证据与 Uploader 顺序见 [`WINDOWS_V2_POSTGRESQL_PGVECTOR.md`](WINDOWS_V2_POSTGRESQL_PGVECTOR.md)。
- Next 引导包：`next-bootstrap-packages.json` 固定五包的 v1 输入已重新下载核对，SHA256 与字节数 5/5 完全一致；`adminer.php → index.php` 的转换由 `repack_for_next.py` 的既有规则和测试覆盖。该路径是固定 bootstrap 契约，不由本次 v2 普通包/PHP producer 生成。

## 阻塞项

- `typesense`：当前 Windows v1 API 中没有该包，Windows v1 构建入口也没有配方；已从 Windows v2 映射中移除，macOS 映射继续保留。不能把 macOS 配方当作 Windows 支持。

`frp` 需要读取的 v1 `frps.exe` 被 Windows Defender 判为 PUA。经用户明确授权，测试只在 `C:\Windows\Temp\servbay-v1-pocs` 加入临时排除，完成源文件 SHA、v2 7z 回读和 `frpc -v` 后立即删除并回读确认；没有修改 PUA 策略或其他 Defender 配置。

## 由此修正的契约

- `memcached` 不是单文件包：v1 真实成员包含 `bin/`、`include/` 和构建说明，因此 v2 按完整树保留，入口为 `memcached/bin/memcached.exe`。
- `rust` 的 v1 归档是 cargo、rustc、rust-docs、rust-std、clippy 等 component 目录；v2 将各组件的安装内容重叠合并到统一 toolchain 根，入口为 `rust/bin/rustc.exe`，各组件 `manifest.in` 保留在 `share/doc/rust/components/<component>/manifest.in`。
- `simple-acme` 是 Windows-only 完整树，保留根 `wacs.exe`、plugins 和默认配置；`rust` 与 `simple-acme` 已加入普通 repacker，而非只存在于测试候选表。
- Windows 7-Zip 输出使用反斜杠、CRLF，空字段可能写成 `Block =`，符号链接使用 `Attributes = AL`。Builder 与 Uploader 的解析器均按这些真实输出修正并保留字面成员重选。
- 大型树不能在 7-Zip 中逐成员启动数千个进程。repacker 改为先校验完整 listing，再一次性解包到新目录，比较实际文件集合并逐文件计算 SHA；恶意路径和链接仍在解包前拒绝。
- Adminer 与 phpMyAdmin 的归档内入口是根相对路径；两平台统一为 `adminer/index.php`、`phpmyadmin/index.php`，避免重复包名前缀。

## 尚待完成

- 35 个包的归档、二进制启动/语法已经验证，但普通包 producer 的 `native_validation` 仍保持 pending；还必须由客户端完成实际安装、服务启动、API/HTTP 请求和卸载验收。
- `typesense` 若确需 Windows 支持，应先由上游或自建 producer 提供可验证的 Windows x64 产物，再新增 Building、布局、原生验收和 Uploader 契约。
