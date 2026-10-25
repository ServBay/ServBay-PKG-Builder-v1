# Windows v2 普通包真实 POC

核查日期：2026-10-25。目录来自公开 Windows v1 API 的真实响应；每个名称选择当时语义版本最高的 x64 条目，不用测试夹具代替下载包。POC 只验证 v1 归档到 v2 目录、路径安全、文件集合、逐文件 SHA 和产物 7z 回读，不代表客户端安装或二进制运行验收。

| 包 | v1 版本 | 结果 | v2 根目录 / 入口 | 文件数 |
|---|---:|---|---|---:|
| adminer | 6.2.0 | PASS | adminer / `adminer/index.php` | 1 |
| ai-gateway | 1.31.0 | PASS | ai-gateway / `ai-gateway/bin/servbay-ai-gateway.exe` | 1 |
| apache | 2.4.69 | PASS | apache / `apache/bin/httpd.exe` | 2776 |
| bun | 1.4.4 | PASS | bun / `bun/bin/bun.exe` | 1 |
| caddy | 2.11.7 | PASS | caddy / `caddy/bin/caddy.exe` | 3 |
| cloudflared | 2026.10.3 | PASS | cloudflared / `cloudflared/bin/cloudflared.exe` | 1 |
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
| ngrok | 3.39.13 | PASS | ngrok / `ngrok/bin/ngrok.exe` | 1 |
| ollama | 0.40.10 | PASS | ollama / `ollama/bin/ollama.exe` | 36 |
| openjdk | 27.0.1 | PASS | openjdk/27 / `openjdk/27/bin/java.exe` | 560 |
| openssl | 3.5.0 | PASS | openssl / `openssl/bin/openssl.exe` | 152 |
| phpmyadmin | 5.2.3 | PASS | phpmyadmin / `phpmyadmin/index.php` | 4314 |
| pinggy | 0.6.1 | PASS | pinggy / `pinggy/bin/pinggy.exe` | 1 |
| ruby | 4.0.9 | PASS | ruby/4.0 / `ruby/4.0/bin/ruby.exe` | 18253 |

结果：当前公开 Windows v1 API 中 28 个可选包全部完成真实 v1→v2 7z 生成与回读。

28 个包的 CLI/可执行入口均已在 Windows 11 VM 从 v2 产物或字节等价的精确定位树启动并核对版本或语法：Apache、Bun、Caddy、Cloudflared、Deno、frp、Git、Go、Mailpit、MariaDB、MeiliSearch、Memcached、MinIO、MongoDB、MongoShell、MongoDB Database Tools、MySQL、ngrok、Ollama、OpenJDK、OpenSSL、Pinggy、Ruby、AI Gateway、DiskCleaner；Adminer 与 phpMyAdmin 使用原生 Windows PHP v2 CLI 执行 `php -n -l`；Maven 使用同一批精确 v1 SHA 的 OpenJDK 27 运行 `mvn -version`。每项记录 v2 产物 SHA、退出码和标准化输出 SHA。

## 阻塞项

- `typesense`：当前 Windows v1 API 中没有该包，Windows v1 构建入口也没有配方；已从 Windows v2 映射中移除，macOS 映射继续保留。不能把 macOS 配方当作 Windows 支持。

`frp` 需要读取的 v1 `frps.exe` 被 Windows Defender 判为 PUA。经用户明确授权，测试只在 `C:\Windows\Temp\servbay-v1-pocs` 加入临时排除，完成源文件 SHA、v2 7z 回读和 `frpc -v` 后立即删除并回读确认；没有修改 PUA 策略或其他 Defender 配置。

## 由此修正的契约

- `memcached` 不是单文件包：v1 真实成员包含 `bin/`、`include/` 和构建说明，因此 v2 按完整树保留，入口为 `memcached/bin/memcached.exe`。
- Windows 7-Zip 输出使用反斜杠、CRLF，空字段可能写成 `Block =`，符号链接使用 `Attributes = AL`。Builder 与 Uploader 的解析器均按这些真实输出修正并保留字面成员重选。
- 大型树不能在 7-Zip 中逐成员启动数千个进程。repacker 改为先校验完整 listing，再一次性解包到新目录，比较实际文件集合并逐文件计算 SHA；恶意路径和链接仍在解包前拒绝。
- Adminer 与 phpMyAdmin 的归档内入口是根相对路径；两平台统一为 `adminer/index.php`、`phpmyadmin/index.php`，避免重复包名前缀。

## 尚待完成

- 28 个包的归档和二进制启动/语法已经验证，但普通包 producer 的 `native_validation` 仍保持 pending；还必须由客户端完成实际安装、服务启动、API/HTTP 请求和卸载验收。
- `typesense` 若确需 Windows 支持，应先由上游或自建 producer 提供可验证的 Windows x64 产物，再新增 Building、布局、原生验收和 Uploader 契约。
