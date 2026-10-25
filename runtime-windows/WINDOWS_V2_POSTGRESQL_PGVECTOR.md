# Windows v2 PostgreSQL 与 pgvector

本轮把 PostgreSQL core 和 pgvector 从“诊断归档”推进到可被 Uploader 直接消费的 schema-2 候选。两者均在 Windows 11 VM 使用真实公开输入和原生数据库测试生成；没有上传、注册 API 或修改生产环境。

## PostgreSQL 18.6 core

- 公开源：`https://get.enterprisedb.com/postgresql/postgresql-18.6-5-windows-x64-binaries.zip`
- 源 SHA256：`e2246ba91d22345bc3d017586c09ede52d9df180b1eeb480f050445f1cad84e2`
- 产物：`postgresql-18.6-windows-x86_64.7z`
- 大小：241,027,682 字节
- SHA256：`f500d3df630bb450a83699f1ad1a46e2220f50360a44c36db5d1d434078ee263`
- 根目录：`postgresql/18`；入口：`postgresql/18/bin/postgres.exe`
- `minimum_os=10.0`；无 Runtime/Devlib `base`
- `package_manifest.default_modules=[]`，PostgreSQL contrib 继续留在 core

原生结果验证了 `pg_config`、`postgres`、`initdb`、`pg_ctl`、`psql`、初始化集群、`server_version=18.6`、UTF8、查询、启动和停止；测试结束时进程和监听均为 0。最终 metadata SHA256 为 `00ecfb54c0321efed26eb8f5134a6b162b2b4bebfb9043efc843ca6571132669`。

## pgvector 0.8.7

- 公开源：`https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.7.tar.gz`
- 源 SHA256：`cac0b10c360f05b2d521200105ba3697e773d4cd3731f5a915a7e37ebe0bea85`
- 产物：`ext-vector-0.8.7-pgsql18-windows-x86_64.7z`
- 大小：122,421 字节
- SHA256：`6993987fec661ce52e8af005d0785fd8c7f8a404d1fc43f681dc4bdf91fdb3ca`
- 文件：47 个，其中 42 个 SQL；提供 `vector`
- Host：PostgreSQL 18；`host.package_sha256` 精确绑定上面的 core

原生结果验证 `CREATE EXTENSION vector`、版本 0.8.7、L2 距离查询、drop/recreate、停止和零残留。最终 metadata SHA256 为 `efb9ba706849054b6ae471c0ee1806748858f5d7238da7872891d9cf4b90d0d7`。

## 共享兼容上下文

```json
{"distribution":"windows-x64","toolchain":"msvc-19.44.35229","crt":"msvc-static"}
```

`vector.dll` 的导入表只有 `postgres.exe` 和 `KERNEL32.dll`，没有动态 VC runtime 依赖。该值必须在未来同一 PostgreSQL ABI 的模块与 core manifest 中保持一致。

## Uploader 消费

Uploader 的 `windows_v2.build_windows_publish_plan()` 已同时读取两份 meta 和相邻 7z，结果是 `validation_complete=true`，core 与模块的 schema、SHA、host ABI、兼容上下文和 `minimum_os` 全部通过。当前批次仍把模块排在 core 前，因为模块没有把 core 类型包列为可排序 dependency；避免部分发布的排序策略需按用户裁决处理。

本轮只执行离线 dry-run。LIVE 上传、schema-2 注册、CDN 回读和 Windows 客户端 API 安装仍未执行。
