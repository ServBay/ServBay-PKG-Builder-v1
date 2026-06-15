#!/bin/bash

# PHP 8.6 扩展补丁应用脚本
# 使用方法: ./apply-patches.sh [扩展名称]
# 例如: ./apply-patches.sh mongodb
#      ./apply-patches.sh all  (应用所有补丁)

set -e

SRC_DIR="/Users/sam/ServBay-Utility/runtime/src"
PATCH_DIR="/Users/sam/ServBay-Utility/runtime/patch/php8.6"

cd "$SRC_DIR"

apply_patch() {
    local name=$1
    local patch_file="${PATCH_DIR}/${name}.patch"

    if [ ! -f "$patch_file" ]; then
        echo "⚠️  补丁文件不存在: $patch_file"
        return 1
    fi

    echo "📦 应用 ${name} 补丁..."
    if patch -p0 < "$patch_file"; then
        echo "✅ ${name} 补丁应用成功"
        return 0
    else
        echo "❌ ${name} 补丁应用失败"
        return 1
    fi
}

case "${1:-all}" in
    mongodb)
        apply_patch "mongodb-1.19.1"
        ;;
    apcu)
        apply_patch "apcu-5.1.24"
        ;;
    phpredis|redis)
        apply_patch "phpredis-6.2.0"
        ;;
    swoole)
        apply_patch "swoole-6.0.3-dev"
        ;;
    phalcon)
        apply_patch "phalcon-5.9.3"
        ;;
    memcached)
        apply_patch "memcached-3.2.0"
        ;;
    memcache)
        apply_patch "memcache-8.0"
        ;;
    all)
        echo "========================================="
        echo "应用所有 PHP 8.6 兼容性补丁"
        echo "========================================="
        echo ""

        apply_patch "mongodb-1.19.1"
        echo ""

        apply_patch "apcu-5.1.24"
        echo ""

        apply_patch "phpredis-6.2.0"
        echo ""

        apply_patch "swoole-6.0.3-dev"
        echo ""

        apply_patch "phalcon-5.9.3"
        echo ""

        apply_patch "memcached-3.2.0"
        echo ""

        apply_patch "memcache-8.0"
        echo ""

        echo "========================================="
        echo "✅ 所有补丁应用完成！"
        echo "========================================="
        ;;
    *)
        echo "用法: $0 [mongodb|apcu|phpredis|swoole|phalcon|memcached|memcache|all]"
        echo ""
        echo "可用的补丁:"
        echo "  mongodb      - MongoDB 1.19.1"
        echo "  apcu         - APCu 5.1.24"
        echo "  phpredis     - PhpRedis 6.2.0"
        echo "  swoole       - Swoole 6.0.3-dev"
        echo "  phalcon      - Phalcon 5.9.3"
        echo "  memcached    - Memcached 3.2.0"
        echo "  memcache     - Memcache 8.0"
        echo "  all          - 应用所有补丁"
        exit 1
        ;;
esac
