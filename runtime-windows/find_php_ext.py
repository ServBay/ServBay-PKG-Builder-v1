import requests
from bs4 import BeautifulSoup
import time
from urllib.parse import urljoin
import re
from decimal import Decimal, InvalidOperation
import json

# --- 配置 ---
# PECL_MODULES = [
#     "xdebug", "swoole", 
#     "phalcon"
# ]
PECL_MODULES = [
    "imagick", "xdebug", "redis", "apcu", "yaml",
    "memcached", "mongodb", "pcov", "amqp", "memcache", 
    "igbinary", "ssh2", "sqlsrv",
    "rdkafka", "pdo_sqlsrv", "mailparse", "oci8", 
    "msgpack", "geoip", "uploadprogress", "libsodium", 
    "protobuf", "phalcon", "grpc", "oauth", "imap"
]
HTTP_TIMEOUT = 300 # 设置 HTTP 请求超时 (秒)
# 1. 定义实际关心的 PHP 版本列表 (字符串格式)
#    根据实际存在的 PHP 版本修改此列表
TARGET_PHP_VERSIONS_LIST = [
    '5.6',
    '7.0', '7.1', '7.2', '7.3', '7.4',
    '8.0', '8.1', '8.2', '8.3', '8.4', '8.5'
]
# 2. 边界检查仍然有用，以防 PECL 页面提供范围外的版本
MIN_PHP_VERSION_DEC = Decimal(TARGET_PHP_VERSIONS_LIST[0])
MAX_PHP_VERSION_DEC = Decimal(TARGET_PHP_VERSIONS_LIST[-1])

# --- 全局变量 ---
PECL_BASE_URL = "https://pecl.php.net"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}
# 用于存储最终结果的字典
# 结构: final_results[pecl_name][php_version_str] = {'pecl_version': latest_pecl_version, 'download_url': url}
final_results = {}
# 将列表转换为 Set 以便快速查找和比较
target_php_versions_set = set(TARGET_PHP_VERSIONS_LIST)


# --- 辅助函数 ---
def safe_get_request(url, retries=3, delay=2):
    """带重试和延迟的GET请求 (改进编码处理)"""
    last_exception = None
    for i in range(retries):
        try:
            response = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT) # Slightly longer timeout

            # 检查状态码，4xx/5xx 会抛出异常
            response.raise_for_status()

            # --- 编码处理 ---
            # 1. 获取原始字节
            content_bytes = response.content
            if not content_bytes:
                 print(f"  请求成功但响应内容为空 (尝试 {i+1}/{retries}): {url}")
                 raise requests.exceptions.RequestException("Empty response content")

            # 2. 确定编码
            detected_encoding = None
            if response.encoding: # 服务器在 header 中声明了编码
                detected_encoding = response.encoding
            else: # Header 未声明，requests 根据内容猜测
                detected_encoding = response.apparent_encoding

            # 3. 修正编码 (PECL 页面通常是 UTF-8，如果检测不是，强制或优先使用 UTF-8)
            final_encoding = detected_encoding
            if detected_encoding and detected_encoding.lower() not in ['utf-8', 'utf8']:
                print(f"  警告: 检测到非 UTF-8 编码 for {url}. Declared/Apparent: {detected_encoding}. 尝试强制 UTF-8.")
                # 强制使用 UTF-8 通常对 PECL 更可靠
                final_encoding = 'utf-8'
            elif not detected_encoding:
                 print(f"  警告: 无法检测到编码 for {url}. 默认使用 UTF-8.")
                 final_encoding = 'utf-8' # 如果完全无法检测，也默认 UTF-8

            # 4. 使用最终确定的编码解码
            try:
                response_text = content_bytes.decode(final_encoding, errors='replace') #用替换避免解码错误中断
            except Exception as decode_err:
                 print(f"  使用编码 '{final_encoding}' 解码失败 (尝试 {i+1}/{retries}): {url} - {decode_err}")
                 # 如果解码失败，尝试用 'iso-8859-1' (Latin-1) 作为最后手段，它通常不会抛出错误
                 try:
                     print(f"    尝试使用 'iso-8859-1' 解码...")
                     response_text = content_bytes.decode('iso-8859-1')
                 except Exception as fallback_decode_err:
                     print(f"    使用 'iso-8859-1' 解码也失败: {fallback_decode_err}")
                     raise requests.exceptions.RequestException("Failed to decode response content with any encoding") from fallback_decode_err


            # --- 内容错误检查 (在解码后的文本上进行) ---
            lower_text = response_text.lower()
            error_indicators = [
                "cannot connect to database", "internal server error",
                "package not found", "an error occurred"
            ]
            for indicator in error_indicators:
                if indicator in lower_text:
                    print(f"  请求成功但页面包含错误信息 '{indicator}' (尝试 {i+1}/{retries}): {url}")
                    # 认为包含错误的页面是失败的，以便重试或最终放弃
                    raise requests.exceptions.RequestException(f"PECL page contains server-side error: {indicator}")

            # --- 成功 ---
            # 将解码后的文本附加到 response 对象上，方便后续使用 (虽然这不是标准做法，但能简化调用代码)
            # 或者，可以直接返回解码后的文本和 response 对象 (如果需要状态码等)
            # 为了保持接口一致，我们还是返回 response，但在调用处使用我们解码的 text
            # *** 重要: BeautifulSoup 应该直接接收 bytes 和我们确定的编码 ***
            # return response # 返回原始 response 对象，调用者需要知道用哪个编码解析

            # *** 修改：返回 response 对象，并附带我们确定的编码和解码后的文本 ***
            # 这有点 hacky，但避免修改调用代码太多
            response._decoded_text = response_text
            response._final_encoding = final_encoding
            return response # 返回修改后的 response

        except requests.exceptions.HTTPError as e:
            print(f"  HTTP 错误 (尝试 {i+1}/{retries}): {url} - {e}")
            last_exception = e
            if response and response.status_code == 404:
                 print(f"    页面未找到 (404)，停止重试: {url}")
                 return None # 不重试 404
        except requests.exceptions.RequestException as e:
            print(f"  请求或处理失败 (尝试 {i+1}/{retries}): {url} - {e}")
            last_exception = e
        except Exception as e:
            print(f"  处理请求时发生意外错误 (尝试 {i+1}/{retries}): {url} - {e}")
            last_exception = e

        if i < retries - 1:
            print(f"    将在 {delay} 秒后重试...")
            time.sleep(delay)
        else:
            print(f"  已达到最大重试次数，放弃请求: {url}")
            # 可以选择性地抛出最后遇到的异常
            # raise last_exception if last_exception else RuntimeError(f"Request failed after {retries} retries")
            return None # 或者只是返回 None
    return None # Fallback

def parse_php_version_string(version_str):
    """从字符串中提取并验证PHP版本号，返回 Decimal 或 None"""
    match = re.search(r'(\d+\.\d+)', version_str)
    if match:
        try:
            # 直接返回字符串版本号，因为我们现在用字符串集合比较
            # return Decimal(match.group(1))
            return match.group(1) # Return as string 'X.Y'
        except Exception: # Wider catch if needed, though unlikely for regex match
            return None
    return None

def is_target_php_version_str(php_version_str):
    """检查PHP版本字符串是否在目标集合中"""
    return php_version_str in target_php_versions_set

def version_key(v):
    """生成用于排序的版本键 (处理 RC, beta, alpha)"""
    # (保持之前的 version_key 函数不变)
    parts = re.findall(r'(\d+|[a-zA-Z]+)', v)
    key = []
    current_num = []
    for part in parts:
        if part.isdigit():
            current_num.append(int(part))
        else:
            if current_num:
                key.extend(current_num)
                current_num = []
            # Assign lower precedence to alpha, beta, RC
            if 'alpha' in part.lower(): key.append(-3)
            elif 'beta' in part.lower(): key.append(-2)
            elif 'rc' in part.lower(): key.append(-1)
            else: key.append(0) # Treat other text as stable part separator
            # Append numeric part if exists (e.g., RC1, RC2)
            num_part = re.search(r'\d+$', part)
            if num_part:
                key.append(int(num_part.group()))
            else:
                 key.append(0) # If just 'RC', treat as RC0 essentially

    if current_num: # Append trailing numbers if any
        key.extend(current_num)

    # Pad with zeros for comparison consistency, more robust padding
    first_non_num_idx = -1
    for i, item in enumerate(key):
        if not isinstance(item, int) or item < 0:
            first_non_num_idx = i
            break

    num_padding_needed = 4 - (first_non_num_idx if first_non_num_idx != -1 else len(key))
    if num_padding_needed > 0:
        if first_non_num_idx != -1:
            key = key[:first_non_num_idx] + [0] * num_padding_needed + key[first_non_num_idx:]
        else:
            key.extend([0] * num_padding_needed)

    while len(key) < 6: # Allow for num.num.num.num + type + type_num
        key.append(0)
    return key


# --- 主逻辑 ---

print("开始获取 PECL 模块最新可用 NTS DLL 链接...")
print(f"目标 PHP 版本: {', '.join(sorted(TARGET_PHP_VERSIONS_LIST, key=Decimal))}") # Use the defined list
print(f"目标 PECL 模块: {', '.join(PECL_MODULES)}")
print("-" * 30)

# 遍历每个预定义的 PECL 模块
for module_name in PECL_MODULES:
    print(f"\n处理模块: {module_name}")
    package_url = f"{PECL_BASE_URL}/package/{module_name}"
    final_results[module_name] = {}
    found_php_versions_for_module = set()

    # 3. 获取可用版本列表
    response = safe_get_request(package_url)
    if not response:
        print(f"  无法获取模块 {module_name} 的版本列表，跳过。")
        continue

    soup = BeautifulSoup(response.text, 'html.parser')
    pecl_versions_found = set()

    available_releases_header = soup.find(['th','h2'], string=lambda t: t and "Available Releases" in t)
    release_table = None
    if available_releases_header:
        release_table = available_releases_header.find_parent('table')
        if not release_table:
             release_table = available_releases_header.find_parent().find_next_sibling('table')
        if not release_table:
             content_div = soup.find(id='content" container') or soup.find('div', class_='content') or soup.body
             if content_div: release_table = content_div.find('table')

    if release_table:
        # Find rows, then links within the first cell (usually th)
        rows = release_table.find_all('tr')
        print(f"  找到版本表格: {len(rows)} 行")
        for row in rows:
            # print(f"  检查行: {row}")
            first_cell = row.find(['th', 'td']) # First cell could be th or td
            if not first_cell: continue
            link = first_cell.find('a', href=re.compile(rf'^/package/{module_name}/[\w\.\-]+$'))
            # print(f"  检查链接: {link}")
            if link:
                href = link['href']
                version = href.split('/')[-1]
                if version and link.text.strip() == version and re.match(r'^\d+(\.\d+)*([a-zA-Z]+\d*)?$', version):
                    # Check for DLL link more reliably in the 4th cell (index 3)
                    all_cells = row.find_all(['th', 'td'])
                    if len(all_cells) > 3 and all_cells[3].find('a', href=f"{href}/windows"):
                        pecl_versions_found.add(version)
                    # Optional: Add versions even without explicit DLL link in overview?
                    else: pecl_versions_found.add(version)
    else:
        print(f"  警告: 在 {package_url} 未能可靠定位到版本表格。尝试备用链接查找。")
        version_links = soup.find_all('a', href=re.compile(rf'^/package/{module_name}/[\d\.]+([a-zA-Z]+\d*)?$'))
        for link in version_links:
            version_match = re.match(r'/package/[^/]+/([\d\.]+([a-zA-Z]+\d*)?)$', link.get('href', ''))
            text_match = re.match(r'^([\d\.]+([a-zA-Z]+\d*)?)$', link.text.strip())
            if version_match and text_match and version_match.group(1) == text_match.group(1):
                 pecl_versions_found.add(version_match.group(1))

    if not pecl_versions_found:
        print(f"  未能提取 {module_name} 的任何可用版本。")
        continue

    pecl_versions = sorted(list(pecl_versions_found), key=version_key, reverse=True)
    print(f"  找到 {len(pecl_versions)} 个潜在版本 (按新旧排序): {', '.join(pecl_versions[:5])}...")

    # 4. 遍历 PECL 版本 (从新到旧)
    for version in pecl_versions:
        if found_php_versions_for_module == target_php_versions_set:
            print(f"  已找到所有目标 PHP 版本 ({len(target_php_versions_set)}) 的最新 PECL 链接，停止搜索 {module_name} 的更旧版本。")
            break

        print(f"  检查 PECL 版本: {version} ...")
        windows_dll_url = f"{PECL_BASE_URL}/package/{module_name}/{version}/windows"

        response_dll = safe_get_request(windows_dll_url)
        if not response_dll:
            print(f"    跳过 {version} (无法获取 Windows DLL 页面)")
            time.sleep(0.5)
            continue
        # Check if DLL page actually loaded correctly
        if f"Information - package {module_name}" not in response_dll.text and f"DLL List" not in response_dll.text :
             # Basic check for non-existent page or major error
             # PECL sometimes returns 200 for non-existent /windows pages
             if "Package Not Found" in response_dll.text or "NoSuchPackage" in response_dll.text or "error occurred" in response_dll.text.lower():
                 print(f"    跳过 {version} (Windows DLL 页面似乎不存在或错误)")
                 time.sleep(0.3)
                 continue
             else:
                 # Could be a different layout, proceed cautiously
                 print(f"    警告: {version} 的 DLL 页面布局可能不同，尝试解析...")


        soup_dll = BeautifulSoup(response_dll.text, 'html.parser')

        # 5. 查找 DLL 表格和链接
        dll_list_header = soup_dll.find(['th', 'h2', 'h3'], string=lambda t: t and "DLL List" in t)
        dll_table = None
        if dll_list_header:
            dll_table = dll_list_header.find_parent('table')
            if not dll_table: dll_table = dll_list_header.find_parent().find_next_sibling('table')
            if not dll_table:
                 wrapper_table = soup_dll.find('table', style=lambda s: s and "width: 90%" in s)
                 if wrapper_table: dll_table = wrapper_table.find('table')

        if not dll_table:
            # Fallback: Find any table containing links with '/pecl/releases/' in href
            all_tables = soup_dll.find_all('table')
            for table in all_tables:
                 if table.find('a', href=re.compile(r'/pecl/releases/')):
                     dll_table = table
                     print(f"    使用备用方法找到 DLL 表格...")
                     break

        if not dll_table:
             print(f"    跳过 {version} (未找到 DLL 表格)")
             time.sleep(0.3)
             continue

        tbody = dll_table.find('tbody') or dll_table # Handle missing tbody

        rows = tbody.find_all('tr', recursive=False)
        processed_in_this_version = False
        for row in rows:
            cells = row.find_all(['th', 'td'], recursive=False)
            if len(cells) == 2:
                th_cell, td_cell = cells[0], cells[1]
                # 使用修改后的 parse_php_version_string 返回字符串
                php_version_str = parse_php_version_string(th_cell.text)

                # 使用修改后的 is_target_php_version_str 进行检查
                if php_version_str and is_target_php_version_str(php_version_str):
                    if php_version_str not in found_php_versions_for_module:
                        found_link_for_php_ver = False
                        links = td_cell.find_all('a')
                        for link in links:
                            link_text = link.text.strip()
                            href = link.get('href')

                            # Prioritize x64 NTS links if available
                            is_nts = "Non Thread Safe (NTS)" in link_text
                            is_x64 = "x64" in link_text

                            if href and (href.endswith('.zip') or href.endswith('.dll')) and is_nts:
                                download_url = urljoin(windows_dll_url, href)

                                # Prefer x64 if multiple NTS links exist for the same PHP version
                                current_entry = final_results[module_name].get(php_version_str)
                                should_update = False
                                if not current_entry:
                                    should_update = True
                                elif is_x64 and 'x86' in current_entry.get('download_url', ''): # Prefer x64 over x86 if x86 was found first
                                    should_update = True
                                # If current is x64 and this one is x86, don't update
                                # If both are x64 or both are x86, the first one (newest PECL) is kept

                                if should_update:
                                    print(f"    -> 找到 PHP {php_version_str} 的最新 PECL: {version} ({'x64' if is_x64 else 'x86'} NTS)")
                                    final_results[module_name][php_version_str] = {
                                        'pecl_version': version,
                                        'download_url': download_url,
                                        'arch': 'x64' if is_x64 else ('x86' if 'x86' in link_text else 'unknown') # Store architecture
                                    }
                                    # Add to found set only after successfully storing
                                    found_php_versions_for_module.add(php_version_str)
                                    processed_in_this_version = True
                                    # Don't break immediately, allow finding x64 if x86 was first in the list
                                    # break # Break only if we are sure we got the preferred one (e.g., found x64)
                                    if is_x64: # If we found the x64 link, we are done for this PHP version row
                                        found_link_for_php_ver = True # Mark as found
                                        break


                        # If after checking all links in the row, we haven't stored an entry (e.g., only TS found)
                        # make sure we don't incorrectly think we are done with this PHP version
                        if not final_results[module_name].get(php_version_str):
                             pass # Keep searching in older PECL versions for this php_version_str

        # if not processed_in_this_version:
        #      print(f"    版本 {version} 未提供任何 *新的* 目标 PHP NTS 链接。")

        time.sleep(0.3)

    # Final check for missing versions for this module
    if not final_results.get(module_name):
         print(f"  模块 {module_name}: 未找到任何目标 PHP 版本的 NTS DLL。")
    else:
         missing_php = target_php_versions_set - found_php_versions_for_module
         if missing_php:
             print(f"  模块 {module_name}: 处理完成，但未能找到以下 PHP 版本的链接: {sorted(list(missing_php), key=Decimal)}")
         else:
             print(f"  模块 {module_name}: 已成功找到所有目标 PHP 版本的最新链接。")


print("\n" + "="*40)
print(" 所有模块处理完毕")
print("="*40 + "\n")

# --- 6. 输出最终清单到文件 ---
print("\n" + "="*40)
print(" 生成输出文件")
print("="*40 + "\n")

# 清理并排序最终结果
output_results = {module: data for module, data in final_results.items() if data}
for module, php_data in output_results.items():
    # 按 PHP 版本号 (Decimal) 降序排序
    sorted_php_data = {php_ver: data for php_ver, data in sorted(php_data.items(), key=lambda item: Decimal(item[0]), reverse=True)}
    output_results[module] = sorted_php_data

# --- 写入 JSON 文件 ---
json_filename = "php-exts.json"
try:
    with open(json_filename, 'w', encoding='utf-8') as f_json:
        json.dump(output_results, f_json, indent=4, ensure_ascii=False)
    print(f"JSON 结果已成功写入文件: {json_filename}")
except IOError as e:
    print(f"错误: 无法写入 JSON 文件 {json_filename} - {e}")

# --- 写入 TXT 文件 (Tab 分隔格式) ---
txt_filename = "php-exts.txt"
try:
    with open(txt_filename, 'w', encoding='utf-8') as f_txt:
        # 可选：写入表头 (如果需要，取消下面一行的注释)
        # f_txt.write("Module\tPHP_Version\tPECL_Version\tArch\tThread_Safety\tDownload_URL\n")

        if not output_results:
             # 即使没有结果，也创建一个空文件或只包含表头的文件
             pass # 或者写入一条消息 f_txt.write("No results found.\n")
        else:
            # 按模块名排序输出
            for module_name in sorted(output_results.keys()):
                php_links = output_results[module_name]

                # output_results 已经按 PHP 版本排序过了
                for php_version, data in php_links.items():
                    pecl_version = data.get('pecl_version', 'N/A')
                    download_url = data.get('download_url', 'N/A')
                    arch = data.get('arch', 'N/A')
                    thread_safety = "nts" # 固定为 nts

                    # 构建 Tab 分隔的行
                    line_items = [
                        module_name,
                        php_version,
                        pecl_version,
                        arch,
                        thread_safety,
                        download_url
                    ]
                    # 使用 '\t'.join() 来确保正确处理各种数据类型并用 Tab 分隔
                    line = '\t'.join(map(str, line_items))
                    f_txt.write(line + '\n') # 写入行并添加换行符

    print(f"TXT 结果 (Tab分隔) 已成功写入文件: {txt_filename}")
except IOError as e:
    print(f"错误: 无法写入 TXT 文件 {txt_filename} - {e}")

print("\n脚本执行完毕。")
