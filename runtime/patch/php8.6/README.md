# PHP 8.6 扩展编译补丁

本目录包含为 PHP 8.6-dev 编译 PECL 扩展所需的补丁文件。

## 补丁列表

### ✅ 完全支持（已测试通过）

1. **MongoDB (1.19.1)** - 无需补丁
   - 编译状态：✅ 成功
   - 说明：此扩展与 PHP 8.6 完全兼容，无需任何修改

2. **APCu (5.1.24)** - `apcu.patch`
   - 补丁大小：267 字节
   - 修复内容：
     - `zval_dtor(&data)` → `zval_ptr_dtor_nogc(&data)`
   - 编译状态：✅ 成功

3. **PHPRedis (6.2.0)** - `phpredis.patch`
   - 补丁大小：49 KB
   - 修复内容：
     - `zval_dtor()` → `zval_ptr_dtor_nogc()` (所有形式)
     - `zval_is_true()` → `zend_is_true()` (所有形式)
   - 编译状态：✅ 成功
   - 说明：此补丁基于 PHP 8.5 补丁后的代码生成

4. **Memcached (3.2.0)** - `memcached.patch`
   - 补丁大小：1.0 KB
   - 前置补丁：`patch/php8.5/memcached.diff` (头文件 + 异常类修复)
   - 修复内容：
     - `zval_dtor()` → `zval_ptr_dtor_nogc()` (所有形式)
   - 编译状态：✅ 成功
   - 说明：此补丁基于 memcached.diff 补丁后的代码生成

5. **Memcache (8.0)** - `memcache.patch` + `build_package` 内联 sed
   - 补丁大小：2.8 KB
   - 前置补丁：`patch/php8.5/memcache.diff` (头文件修复)
   - 修复内容（`.patch`）：
     - `zval_dtor()` → `zval_ptr_dtor_nogc()` (所有形式)
     - `zval_is_true()` → `zend_is_true()` (所有形式)
   - 修复内容（`build_package` 内联 sed，见 `_build_php_mod_memcache`）：
     - `PS_FUNCS(memcache);` 手动展开为各 `PS_*_FUNC` 声明（绕开 8.6 PS_FUNCS 宏缺分号）
     - `save_path` 由 `const char*` 变 `zend_string*`：`path = save_path;` → `path = ZSTR_VAL(save_path);`
     - **`PS_MOD(memcache)` 手动展开为 8.5 语义**：8.6 把 `PS_MOD` 的 create_sid / validate_sid / update_timestamp
       三个槽位从 core 默认函数改成要求扩展自带的 `ps_create_sid_##x` / `ps_validate_sid_##x`；memcache 8.0 从不实现
       它们，导致 `ps_mod_memcache` 引用未定义符号。填回 core 默认 `php_session_create_id` /
       `php_session_validate_sid` / `php_session_update_timestamp` 即可。
   - 状态：✅ 编译 + `dlopen` 加载均通过（`php --ri memcache` 无 warning）
   - ⚠️ 教训：仅"生成 .so"不代表可用。macOS 扩展用 `-undefined dynamic_lookup` 链接，缺符号在链接期
     不报错，运行 `dlopen` 才炸。判定成功必须加一步 `php --ri <ext>` / `php -m` 加载冒烟测试。

### ⚠️ 部分支持（需要进一步工作）

6. **Swoole (6.0.3-dev)** - `swoole.patch`
   - 补丁大小：49 KB
   - 修复内容：
     - `ZVAL_IS_NULL()` → `Z_ISNULL_P()`
     - `zval_is_true()` → `zend_is_true()`
     - `zval_dtor()` → `zval_ptr_dtor_nogc()`
     - PHP 版本检查：`80500` → `80600`
   - 编译状态：❌ 失败
   - 问题：Swoole 使用了大量已弃用的 PHP 内部 API，需要更广泛的修改
   - 建议：等待 Swoole 官方发布 PHP 8.6 兼容版本

## PHP 8.6 API 变更摘要

### 1. 头文件路径变更
- **旧路径**：`ext/standard/php_smart_string.h`
- **新路径**：`Zend/zend_smart_string.h`
- **影响扩展**：PHPRedis, Memcached, Memcache

### 2. Zval 操作函数变更
- `zval_dtor()` → `zval_ptr_dtor_nogc()`
- `ZVAL_IS_NULL()` → `Z_ISNULL_P()`  (用于指针)
- `zval_is_true()` → `zend_is_true()`
- **影响扩展**：APCu, Swoole

### 3. 异常处理变更
- `zend_exception_get_default()` → `zend_ce_exception`
- **影响扩展**：Memcached

### 4. Session save handler 宏变更（`ext/session/php_session.h`）
- 8.5 及以前 `PS_MOD(x)` 的 create_sid / validate_sid / update_timestamp 槽位填 core 默认函数
  （`php_session_create_id` / `php_session_validate_sid` / `php_session_update_timestamp`）
- 8.6 改为引用扩展自带的 `ps_create_sid_##x` / `ps_validate_sid_##x`（第三槽为 `NULL`）
- 后果：未实现这两个函数的旧 session handler 扩展会引用未定义符号，运行时 `dlopen` 失败
- **影响扩展**：Memcache（8.0）。较新的 Memcached / PHPRedis 已自带这两个函数，不受影响

## 使用方法

补丁会在编译过程中自动应用（通过 `build_package` 脚本）。

对于 PHP 8.6，补丁应用顺序：
1. 先应用 PHP 8.5 补丁或 .diff 补丁（如果存在）
2. 再应用 PHP 8.6 补丁

示例（PHPRedis）：
```bash
patch -p0 < patch/php8.5/phpredis-6.2.0-php8.5.patch
patch -p0 < patch/php8.6/phpredis.patch
```

示例（Memcached）：
```bash
patch -p0 < patch/php8.5/memcached.diff
patch -p0 < patch/php8.6/memcached.patch
```

## 手动测试

如需手动测试扩展编译：

```bash
# 初始化编译环境
source ~/php55-servbay-build/php_build_env.sh

# 设置 PHP 路径
PHP_BIN="/Applications/ServBay/package/php/8.6/8.6.0-dev-20251105/bin"

# 解压并进入扩展目录
tar -xzf phpredis-6.2.0.tgz
cd redis-6.2.0

# 应用补丁
patch -p0 < /Users/sam/ServBay-Utility/runtime/patch/php8.6/phpredis.patch

# 编译
$PHP_BIN/phpize
./configure --with-php-config=$PHP_BIN/php-config
make -j4
```

## 编译成功的扩展模块

所有成功编译的扩展都会生成相应的 `.so` 文件：

- `mongodb.so` (无需补丁)
- `apcu.so` (~103 KB)
- `redis.so` (~705 KB)
- `memcached.so` (~123 KB)
- `memcache.so` (~136 KB)

## 已知问题

### Swoole 扩展
当前版本的 Swoole (6.0.3-dev) 使用了许多 PHP 8.6 中已废弃或移除的内部 API。
虽然已创建部分补丁，但仍有以下问题：
- 使用了内部宏 `ZVAL_IS_ARRAY` 等
- 某些内部函数签名变更
- 需要等待官方支持或进行更深入的修改

**建议**：使用 PHP 8.5 或等待 Swoole 官方发布 PHP 8.6 兼容版本。

## 重要说明

### 补丁生成方法

**关键**：PHP 8.6 补丁必须基于 **已应用 PHP 8.5 补丁后的代码** 生成，而不是基于原始源码。

生成步骤：
1. 解压原始源码
2. 应用 PHP 8.5 补丁（如果存在）
3. 创建备份文件（.bak）
4. 应用 PHP 8.6 修复
5. 使用 `diff -u` 生成补丁

这样生成的补丁才能在 build_package 中按顺序正确应用：
```bash
# PHP 8.5 补丁先应用
patch -p0 < patch/php8.5/phpredis-6.2.0-php8.5.patch
# PHP 8.6 补丁再应用（基于 PHP 8.5 补丁后的代码）
patch -p0 < patch/php8.6/phpredis.patch
```

## 更新日志

### 2025-11-06 (下午 - 第三次修复)
- ✅ 发现 memcached 和 memcache 还有额外的 .diff 补丁需要先应用
- ✅ 重新生成 Memcached 补丁（1.0 KB）
  - 基于 memcached.diff 补丁后的代码
  - 只包含 zval_dtor 修复（头文件和异常类已在 .diff 中）
- ✅ 重新生成 Memcache 补丁（2.8 KB）
  - 基于 memcache.diff 补丁后的代码
  - 只包含 zval_dtor 和 zval_is_true 修复（头文件已在 .diff 中）
- ✅ 验证完整补丁链：.diff → .patch → 编译成功

### 2025-11-06 (下午 - 第二次修复)
- ✅ 修复 sed 替换规则：使用全局替换而非特定模式匹配
- ✅ 重新生成 PHPRedis 补丁（49 KB）
  - 修复所有 `zval_dtor()` 调用（不仅是 `zval_dtor(&var)` 形式）
  - 修复所有 `zval_is_true()` 调用
- ✅ 所有扩展编译成功验证通过

### 2025-11-06 (下午 - 第一次修复)
- ✅ 修复补丁生成方法：所有 PHP 8.6 补丁现在基于 PHP 8.5 补丁后的代码生成
- ✅ 验证所有补丁在 build_package 工作流中正确应用
- ✅ 补丁应用顺序测试通过

### 2025-11-06 (上午)
- ✅ 创建 APCu 补丁（zval_dtor 修复）
- ✅ 创建 PHPRedis 补丁（头文件路径修复）
- ✅ 创建 Memcached 补丁（头文件 + 异常类修复）
- ✅ 创建 Memcache 补丁（头文件路径修复）
- ✅ 确认 MongoDB 无需补丁
- ⚠️ Swoole 补丁部分完成，但仍有兼容性问题
- ✅ 更新 build_package 脚本以自动应用补丁

## 贡献指南

如需为其他扩展创建 PHP 8.6 补丁：

1. 提取原始源码
2. **如果存在 PHP 8.5 补丁，必须先应用它**
3. 创建备份文件（.bak）
4. 进行 PHP 8.6 必要的修改
5. 使用 `diff -u` 生成补丁：
   ```bash
   # 从 PHP 8.5 补丁后的代码生成 PHP 8.6 补丁
   diff -u original_file.c.bak original_file.c > extension.patch
   ```
6. 测试补丁在完整工作流中应用（PHP 8.5 → PHP 8.6 → 编译）
7. 将补丁文件添加到此目录
8. 更新 `build_package` 脚本
9. 更新此 README

参考自动化脚本：`/tmp/regenerate-php86-patches.sh`

## 参考资料

- [PHP 8.6 变更日志](https://www.php.net/ChangeLog-8.php#PHP_8_6)
- [PHP Internals Book](https://www.phpinternalsbook.com/)
- [Zend Engine API](https://www.php.net/manual/en/internals2.php)
