#!/bin/bash

# 合并所有 PHP 8.6 补丁文件
# 使用 cat 命令保持原始格式（tab、空格等）

set -e

PATCH_SRC="/tmp/php-8.6-patchs"
PATCH_DEST="/Users/sam/ServBay-Utility/runtime/patch/php8.6"

echo "开始合并补丁文件..."

# MongoDB - 已经是单文件
echo "[1/7] 处理 MongoDB..."
cat "${PATCH_SRC}/mongodb-Cursor.patch" > "${PATCH_DEST}/mongodb-1.19.1.patch"

# APCu - 已经是单文件
echo "[2/7] 处理 APCu..."
cat "${PATCH_SRC}/apcu-apc_cache.patch" > "${PATCH_DEST}/apcu-5.1.24.patch"

# PhpRedis - 合并 8 个文件
echo "[3/7] 合并 PhpRedis..."
cat "${PATCH_SRC}/phpredis-library.c.patch" \
    "${PATCH_SRC}/phpredis-redis_array.c.patch" \
    "${PATCH_SRC}/phpredis-redis_array_impl.c.patch" \
    "${PATCH_SRC}/phpredis-cluster_library.c.patch" \
    "${PATCH_SRC}/phpredis-redis_cluster.c.patch" \
    "${PATCH_SRC}/phpredis-redis_commands.c.patch" \
    "${PATCH_SRC}/phpredis-redis_session.c.patch" \
    "${PATCH_SRC}/phpredis-redis.c.patch" \
    > "${PATCH_DEST}/phpredis-6.2.0.patch"

# Swoole - 合并 9 个文件
echo "[4/7] 合并 Swoole..."
cat "${PATCH_SRC}/swoole-php_swoole_private.h.patch" \
    "${PATCH_SRC}/swoole-swoole_runtime.cc.patch" \
    "${PATCH_SRC}/swoole-swoole_http_request.cc.patch" \
    "${PATCH_SRC}/swoole-swoole_coroutine_scheduler.cc.patch" \
    "${PATCH_SRC}/swoole-swoole_async_coro.cc.patch" \
    "${PATCH_SRC}/swoole-swoole_server.cc.patch" \
    "${PATCH_SRC}/swoole-swoole_admin_server.cc.patch" \
    "${PATCH_SRC}/swoole-swoole_redis_server.cc.patch" \
    "${PATCH_SRC}/swoole-swoole_thread_atomic.cc.patch" \
    > "${PATCH_DEST}/swoole-6.0.3-dev.patch"

# Phalcon - 已经是单文件
echo "[5/7] 处理 Phalcon..."
cat "${PATCH_SRC}/phalcon-phalcon.zep.c.patch" > "${PATCH_DEST}/phalcon-5.9.3.patch"

# Memcached - 已经是单文件
echo "[6/7] 处理 Memcached..."
cat "${PATCH_SRC}/memcached-php_memcached.c.patch" > "${PATCH_DEST}/memcached-3.2.0.patch"

# Memcache - 合并 4 个文件
echo "[7/7] 合并 Memcache..."
cat "${PATCH_SRC}/memcache-memcache_binary_protocol.c.patch" \
    "${PATCH_SRC}/memcache-memcache_pool.c.patch" \
    "${PATCH_SRC}/memcache-memcache_session.c.patch" \
    "${PATCH_SRC}/memcache-memcache.c.patch" \
    > "${PATCH_DEST}/memcache-8.0.patch"

echo ""
echo "所有补丁文件已合并完成！"
echo "补丁文件位置: ${PATCH_DEST}/"
echo ""
ls -lh "${PATCH_DEST}"/*.patch
