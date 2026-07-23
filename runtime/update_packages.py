#!/usr/bin/env python3
"""
Enhanced script to fetch latest versions for multiple version series
"""

import re
import requests
from typing import Dict, Optional, List
import sys
import time
import argparse
import os
import json

class PackageUpdater:
    # GitHub tags 可能包含从未正式发布的内部开发标签（幽灵标签）。
    # 以下 URL 模板用于通过 HTTP HEAD 请求验证版本是否真实存在于官方下载源。
    # 仅用于通过 GitHub tags 检测版本的软件包，使用官方 API 的软件包无需验证。
    DOWNLOAD_VERIFY_URLS = {
        'mysql': [
            'https://cdn.mysql.com/Downloads/MySQL-{series}/mysql-{version}.tar.gz',
            'https://downloads.mysql.com/archives/get/p/23/file/mysql-{version}.tar.gz',
        ],
        'php': [
            'https://www.php.net/distributions/php-{version}.tar.gz',
        ],
        'postgresql': [
            'https://ftp.postgresql.org/pub/source/v{version}/postgresql-{version}.tar.bz2',
        ],
        'python': [
            'https://www.python.org/ftp/python/{version}/Python-{version}.tgz',
        ],
        'ruby': [
            'https://cache.ruby-lang.org/pub/ruby/{series}/ruby-{version}.tar.gz',
        ],
        'redis': [
            'https://download.redis.io/releases/redis-{version}.tar.gz',
        ],
        'go': [
            'https://dl.google.com/go/go{version}.src.tar.gz',
        ],
        'mariadb': [
            'https://archive.mariadb.org/mariadb-{version}/source/mariadb-{version}.tar.gz',
        ],
    }

    def __init__(self, conf_file: str = "packages.conf", dry_run: bool = False, debug: bool = False):
        self.conf_file = conf_file
        self.packages = {}
        self.api_delay = 0.3  # Delay between API calls
        self.dry_run = dry_run
        self.debug = debug
        self.updated_pkgs = set() # Track updated packages for build command output
        self.updated_records = []  # Structured records for workflow matrix output.

    def record_update(self, name: str, version: str):
        """Record a package update once, preserving insertion order."""
        record = {"name": name, "version": version}
        if record not in self.updated_records:
            self.updated_records.append(record)
        self.updated_pkgs.add(f"{name}-{version}")

    def active_package_records(self):
        """Return active package records from packages.conf for full rebuilds."""
        records = []
        seen = set()
        if not os.path.exists(self.conf_file):
            return records

        with open(self.conf_file, 'r') as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                parts = stripped.split('\t')
                if len(parts) < 2:
                    continue
                name = parts[0].strip()
                version = parts[1].strip()
                if not name or not version or name.lower() == 'package':
                    continue
                key = (name, version)
                if key in seen:
                    continue
                seen.add(key)
                records.append({"name": name, "version": version})
        return records

    def build_matrix(self, records):
        """Build GitHub Actions matrix JSON for macOS package builds."""
        include = []
        seen = set()
        for record in records:
            name = record["name"]
            version = record["version"]
            for arch in ("x86_64", "arm64"):
                key = ("macos", arch, name, version)
                if key in seen:
                    continue
                seen.add(key)
                include.append({
                    "os": "macos",
                    "arch": arch,
                    "name": name,
                    "version": version,
                })
        return {"include": include}

    def emit_matrix(self, records, output_file=None, emit_stdout=False):
        """Write matrix JSON for workflow consumption."""
        matrix = self.build_matrix(records)
        payload = json.dumps(matrix, ensure_ascii=False)
        if output_file:
            with open(output_file, 'w') as f:
                f.write(payload)
                f.write("\n")
        if emit_stdout:
            print(payload)
        return matrix

    def verify_download(self, package, version, series=''):
        """通过 HTTP HEAD 请求验证版本是否存在于官方下载源。
        仅在所有验证 URL 都返回 404 时返回 False，其他情况返回 True。"""
        # 已在 packages.conf 中的版本无需验证（已经过验证）
        if package in self.packages:
            if any(v['version'] == version for v in self.packages[package]):
                return True

        url_templates = self.DOWNLOAD_VERIFY_URLS.get(package)
        if not url_templates:
            return True  # 未配置验证 URL，默认通过

        # 预发布版本跳过验证（URL 格式可能不同）
        if any(tag in version.lower() for tag in ['alpha', 'beta', 'rc', 'dev']):
            return True
        if re.search(r'\d[ab]\d', version):
            return True

        all_404 = True
        for template in url_templates:
            try:
                url = template.format(version=version, series=series)
                response = requests.head(url, timeout=10, allow_redirects=True)
                if response.status_code < 400:
                    return True
                if response.status_code not in (404, 410):
                    all_404 = False  # 非 404 错误（如 405/500），不能确定版本不存在
            except requests.exceptions.RequestException:
                all_404 = False  # 网络错误，不能确定版本不存在

        return not all_404

    def select_latest_verified(self, package, series, sorted_versions, max_attempts=5):
        """从已排序的版本列表中，从最新到最旧依次验证，返回第一个通过验证的版本。
        最多尝试 max_attempts 个版本，避免过多网络请求。"""
        attempts = 0
        for version in reversed(sorted_versions):
            if self.verify_download(package, version, series):
                return version
            attempts += 1
            print(f"    ⚠ {package} {version} not found at official download, trying earlier...")
            if attempts >= max_attempts:
                if self.debug:
                    print(f"    Reached max verification attempts for {package} {series}")
                break
        return None

    def load_packages(self):
        """Load current packages from conf file (including commented ones)"""
        if not os.path.exists(self.conf_file):
            print(f"Error: Config file not found at {self.conf_file}")
            return

        with open(self.conf_file, 'r') as f:
            for line in f:
                original_line = line.strip()
                if not original_line:
                    continue

                # Remove comment prefix if present
                if original_line.startswith('#'):
                    line = original_line[1:].strip()
                else:
                    line = original_line

                parts = line.split('\t')
                if len(parts) >= 3:
                    package = parts[0]
                    version = parts[1]
                    x86_file = parts[2]
                    arm_file = parts[3] if len(parts) > 3 else ''

                    # Skip lines with "NEW VERSION" marker
                    if 'NEW VERSION' in line:
                        continue

                    if package not in self.packages:
                        self.packages[package] = []
                    self.packages[package].append({
                        'version': version,
                        'x86_file': x86_file,
                        'arm_file': arm_file,
                        'line': original_line,
                        'is_commented': original_line.startswith('#')
                    })

    def get_github_api(self, url: str, headers: Optional[Dict] = None) -> Optional[Dict]:
        """Make GitHub API request with token support"""
        if headers is None:
            headers = {'Accept': 'application/vnd.github.v3+json'}

        # Add GitHub token if available
        token = os.getenv('GITHUB_TOKEN')
        if not token:
            env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
            if os.path.exists(env_file):
                with open(env_file, 'r') as f:
                    for line in f:
                        if line.startswith('GITHUB_TOKEN='):
                            token = line.split('=', 1)[1].strip()
                            break

        if token:
            headers['Authorization'] = f'token {token}'

        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 403:
                print(f" (Rate limit exceeded)")
            elif self.debug:
                print(f" (HTTP {response.status_code})")
        except Exception as e:
            if self.debug:
                print(f" (Error: {str(e)[:30]})")
        return None

    def get_node_versions(self) -> Dict[str, str]:
        """Get latest Node.js versions for major versions 12-25"""
        versions = {}

        try:
            # Node.js releases API
            url = "https://nodejs.org/dist/index.json"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                releases = response.json()

                # Track latest for each major version
                major_versions = {}

                for release in releases:
                    version = release.get('version', '').lstrip('v')
                    if version and not any(x in version for x in ['rc', 'nightly']):
                        major = version.split('.')[0]
                        if major.isdigit():
                            major_num = int(major)
                            if 12 <= major_num <= 26:
                                if major not in major_versions:
                                    major_versions[major] = version

                for major, version in major_versions.items():
                    versions[f'node_{major}'] = version

        except Exception as e:
            if self.debug:
                print(f"Error fetching Node versions: {e}")

        return versions

    def get_php_versions(self) -> Dict[str, str]:
        """Get latest PHP versions for 5.3-8.6"""
        versions = {}
        series = ['5.3', '5.4', '5.5', '5.6', '7.0', '7.1', '7.2', '7.3', '7.4',
                  '8.0', '8.1', '8.2', '8.3', '8.4', '8.5', '8.6']

        # Get multiple pages
        all_tags = []
        for page in range(1, 6):
            data = self.get_github_api(f"https://api.github.com/repos/php/php-src/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            # Group by series
            series_versions = {s: [] for s in series}

            for tag in all_tags:
                tag_name = tag.get('name', '').lstrip('php-')
                for serie in series:
                    if tag_name.startswith(serie + '.'):
                        series_versions[serie].append(tag_name)

            # Get latest for each series
            for serie, version_list in series_versions.items():
                if version_list:
                    # Filter based on series
                    if serie in ['8.6']:  # Dev versions
                        # Sort all versions including pre-releases
                        version_list.sort(key=lambda x: tuple(x.split('.')))
                        versions[f'php_{serie}'] = version_list[-1]
                    else:
                        # For stable branches, exclude pre-releases
                        stable_versions = [v for v in version_list
                                         if not any(x in v.lower() for x in ['alpha', 'beta', 'rc'])]
                        if stable_versions:
                            # Sort and get latest (with download verification)
                            stable_versions.sort(key=lambda x: tuple(map(int, x.split('.'))))
                            latest = self.select_latest_verified('php', serie, stable_versions)
                            if latest:
                                versions[f'php_{serie}'] = latest

        return versions

    def get_mariadb_versions(self) -> Dict[str, str]:
        """Get latest MariaDB versions for major series"""
        versions = {}
        series = ['10.4', '10.5', '10.6', '10.7', '10.8', '10.9', '10.10', '10.11', '11.0', '11.1', '11.2', '11.3', '11.4',
                  '11.5', '11.6', '11.7', '11.8', '12.0', '12.1', '12.2', '12.3']

        # 使用 tags 端点而非 releases（MariaDB 并非所有版本都会创建 GitHub Release）
        all_tags = []
        for page in range(1, 10):
            data = self.get_github_api(f"https://api.github.com/repos/MariaDB/server/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            series_versions = {s: [] for s in series}

            for tag in all_tags:
                tag_name = tag.get('name', '').replace('mariadb-', '')
                for serie in series:
                    if tag_name.startswith(serie + '.'):
                        # 只保留纯数字版本号，排除 rc/alpha/beta
                        version_part = tag_name[len(serie) + 1:]
                        if version_part.isdigit():
                            series_versions[serie].append(tag_name)
                        break

            for serie, version_list in series_versions.items():
                if version_list:
                    version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))
                    latest = self.select_latest_verified('mariadb', serie, version_list)
                    if latest:
                        versions[f'mariadb_{serie}'] = latest

        return versions

    def get_postgresql_versions(self) -> Dict[str, str]:
        """Get latest PostgreSQL versions for major versions"""
        versions = {}
        major_versions = list(range(10, 20))  # 10 to 19

        # Get multiple pages
        all_tags = []
        for page in range(1, 5):
            data = self.get_github_api(f"https://api.github.com/repos/postgres/postgres/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            # Group by major version
            major_version_tags = {major: [] for major in major_versions}

            for tag in all_tags:
                tag_name = tag.get('name', '')
                # PostgreSQL uses REL_XX_Y format
                if tag_name.startswith('REL_'):
                    version = tag_name.replace('REL_', '').replace('_', '.')
                    for major in major_versions:
                        if version.startswith(f'{major}.'):
                            major_version_tags[major].append(version)

            # Get latest for each major
            for major, version_list in major_version_tags.items():
                if version_list:
                    # Filter out RC/alpha; keep beta only for major=19 (PG 19 is in beta,
                    # no GA yet). Older majors had historical betas that must stay excluded.
                    stable_versions = []
                    for v in version_list:
                        exclude_tokens = ['RC', 'ALPHA']
                        if major != 19:
                            exclude_tokens.append('BETA')
                        if not any(x in v.upper() for x in exclude_tokens):
                            stable_versions.append(v)

                    if stable_versions:
                        # Sort versions and get latest (with download verification).
                        # Major 19 tags look like "19beta2" (mixed alpha-num); older
                        # majors are pure numeric. A split-on-non-digit key handles both.
                        def pg_key(v):
                            key = []
                            for p in re.split(r'(\D+)', v):
                                if p.isdigit():
                                    key.append((0, int(p)))
                                elif p:
                                    key.append((1, p))
                            return key
                        stable_versions.sort(key=pg_key)
                        latest = self.select_latest_verified('postgresql', str(major), stable_versions)
                        if latest:
                            # Keep the dotted form in packages.conf (e.g.
                            # "19.beta2") so upload_packages.py's strip_patch
                            # derives major=19. build_package strips the dot
                            # when constructing the FTP URL. Only lowercase
                            # the prerelease suffix to match the tarball name
                            # ("postgresql-19beta2.tar.bz2"). See commit
                            # 96dd915.
                            latest = re.sub(r'\.(BETA|RC|ALPHA)(\d+)',
                                            lambda m: '.' + m.group(1).lower() + m.group(2),
                                            latest)
                            versions[f'postgresql_{major}'] = latest

        return versions

    def get_mysql_versions(self) -> Dict[str, str]:
        """Get latest MySQL versions for major series"""
        versions = {}
        series = ['5.5', '5.6', '5.7', '8.0', '8.1', '8.2', '8.3', '8.4', '9.0', '9.1', '9.2', '9.3', '9.4', '9.5', '9.6', '9.7']

        # Get multiple pages (need at least 3 to skip cluster versions)
        all_tags = []
        for page in range(1, 6):
            data = self.get_github_api(f"https://api.github.com/repos/mysql/mysql-server/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            # Group by series and find latest
            series_versions = {s: [] for s in series}

            for tag in all_tags:
                tag_name = tag.get('name', '')
                # Skip cluster versions and special releases
                if 'cluster' in tag_name.lower() or '-labs' in tag_name or '-release' in tag_name:
                    continue

                # Check if it matches mysql-X.Y.Z format
                if tag_name.startswith('mysql-') and re.match(r'^mysql-\d+\.\d+\.\d+$', tag_name):
                    version = tag_name.replace('mysql-', '')
                    # Find which series this belongs to
                    for serie in series:
                        if version.startswith(serie + '.'):
                            series_versions[serie].append(version)
                            break

            # Get latest for each series (with download verification)
            for serie, version_list in series_versions.items():
                if version_list:
                    version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))
                    latest = self.select_latest_verified('mysql', serie, version_list)
                    if latest:
                        versions[f'mysql_{serie}'] = latest

        return versions

    def get_python_versions(self) -> Dict[str, str]:
        """Get latest Python versions for major.minor series"""
        versions = {}
        series = ['2.7', '3.5', '3.6', '3.7', '3.8', '3.9', '3.10', '3.11', '3.12', '3.13', '3.14', '3.15']

        # Get multiple pages
        all_tags = []
        for page in range(1, 5):
            data = self.get_github_api(f"https://api.github.com/repos/python/cpython/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            # Group by series
            series_versions = {s: [] for s in series}

            for tag in all_tags:
                tag_name = tag.get('name', '').lstrip('v')
                for serie in series:
                    if tag_name.startswith(serie + '.'):
                        # For 3.15, include alpha/beta/rc since it's pre-release.
                        if serie == '3.15':
                            series_versions[serie].append(tag_name)
                        elif not any(x in tag_name for x in ['a', 'b', 'rc']):
                            # Exclude pre-releases (alpha/beta/rc); 3.14 is GA now.
                            series_versions[serie].append(tag_name)

            # Get latest for each series
            for serie, version_list in series_versions.items():
                if version_list:
                    # Sort and get latest
                    # Handle versions with alpha/beta/rc suffixes
                    def version_key(v):
                        # Split into numeric and suffix parts
                        match = re.match(r'^([\d.]+)(.*?)$', v)
                        if match:
                            nums = tuple(map(int, match.group(1).split('.')))
                            suffix = match.group(2).lower()
                            if not suffix:
                                # Stable versions (no suffix) come after pre-releases
                                return nums + (999, 0)
                            # Pre-release: priority by token, sub-order by trailing digit
                            m = re.search(r'\d+', suffix)
                            sub = int(m.group()) if m else 0
                            for key, pri in [('rc', 3), ('b', 2), ('beta', 2), ('a', 1), ('alpha', 1)]:
                                if key in suffix:
                                    return nums + (pri, sub)
                            return nums + (999, 0)
                        return (0,)

                    version_list.sort(key=version_key)
                    latest = self.select_latest_verified('python', serie, version_list)
                    if latest:
                        versions[f'python_{serie}'] = latest

        return versions

    def get_ruby_versions(self) -> Dict[str, str]:
        """Get latest Ruby versions"""
        versions = {}
        series = ['2.4', '2.5', '2.6', '2.7', '3.0', '3.1', '3.2', '3.3', '3.4', '4.0']

        # Ruby tags on GitHub are vX_Y_Z (e.g., v3_2_2)
        # Need multiple pages
        all_tags = []
        for page in range(1, 6):
            data = self.get_github_api(f"https://api.github.com/repos/ruby/ruby/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            series_versions = {s: [] for s in series}

            for tag in all_tags:
                tag_name = tag.get('name', '')
                # Format: v3_2_2 or v2_7_8
                if tag_name.startswith('v'):
                    # Replace _ with .
                    version = tag_name[1:].replace('_', '.')
                    # Check if valid version
                    parts = version.split('.')
                    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                        std_version = f"{parts[0]}.{parts[1]}.{parts[2]}"

                        for serie in series:
                            if std_version.startswith(serie + '.'):
                                series_versions[serie].append(std_version)

            # Get latest for each series
            for serie, version_list in series_versions.items():
                if version_list:
                    version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))
                    latest = self.select_latest_verified('ruby', serie, version_list)
                    if latest:
                        versions[f'ruby_{serie}'] = latest

        return versions

    def get_redis_versions(self) -> Dict[str, str]:
        """Get latest Redis versions"""
        versions = {}
        series = ['7.4']

        # Redis releases
        all_tags = []
        for page in range(1, 5):
            data = self.get_github_api(f"https://api.github.com/repos/redis/redis/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            series_versions = {s: [] for s in series}

            for tag in all_tags:
                tag_name = tag.get('name', '')
                # Redis tags are usually just X.Y.Z
                if re.match(r'^\d+\.\d+\.\d+$', tag_name):
                    for serie in series:
                        if tag_name.startswith(serie + '.'):
                            series_versions[serie].append(tag_name)

            for serie, version_list in series_versions.items():
                if version_list:
                    version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))
                    latest = self.select_latest_verified('redis', serie, version_list)
                    if latest:
                        versions[f'redis_{serie}'] = latest

        return versions

    def get_composer_versions(self) -> Dict[str, str]:
        """Get latest Composer versions for 2.2 LTS and 2.x latest"""
        versions = {}

        data = self.get_github_api("https://api.github.com/repos/composer/composer/releases?per_page=30")
        if not data:
            return versions

        latest_2_2 = None
        latest_2 = None

        for release in data:
            if release.get('prerelease'):
                continue
            tag = release.get('tag_name', '')
            ver = self.clean_version_tag(tag)
            if not ver:
                continue

            # 只关注 2.x 系列
            if not ver.startswith('2.'):
                continue

            if ver.startswith('2.2.'):
                if latest_2_2 is None or self.compare_versions(ver, latest_2_2) > 0:
                    latest_2_2 = ver
            else:
                if latest_2 is None or self.compare_versions(ver, latest_2) > 0:
                    latest_2 = ver

        if latest_2_2:
            versions['composer_2.2'] = latest_2_2
        if latest_2:
            versions['composer_2'] = latest_2

        return versions

    def get_go_versions(self) -> Dict[str, str]:
        """Get latest Go versions"""
        versions = {}

        # Get multiple pages to ensure we have recent versions
        all_tags = []
        for page in range(1, 6):  # Get first 5 pages
            data = self.get_github_api(f"https://api.github.com/repos/golang/go/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            # Parse all versions
            version_list = []
            for tag in all_tags:
                tag_name = tag.get('name', '').replace('go', '')

                # Skip non-version tags
                if not tag_name or any(x in tag_name.lower() for x in ['beta', 'rc', 'weekly', 'release', 'tip']):
                    continue

                # Match version pattern
                if re.match(r'^\d+\.\d+(\.\d+)?$', tag_name):
                    version_list.append(tag_name)

            # Sort versions properly
            version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))

            # Group by major.minor series
            go_series_versions = {}
            for version in version_list:
                parts = version.split('.')
                if len(parts) >= 2:
                    major_minor = f"{parts[0]}.{parts[1]}"
                    if major_minor not in go_series_versions:
                        go_series_versions[major_minor] = []
                    go_series_versions[major_minor].append(version)

            # Get versions for recent Go releases (1.20+), with download verification
            for mm, ver_list in go_series_versions.items():
                try:
                    parts = mm.split('.')
                    mm_tuple = (int(parts[0]), int(parts[1]))
                    if mm_tuple >= (1, 20):
                        latest = self.select_latest_verified('go', mm, ver_list)
                        if latest:
                            versions[f'go_{mm}'] = latest
                except (ValueError, IndexError):
                    continue

        return versions

    def get_dotnet_versions(self) -> Dict[str, str]:
        """Get latest .NET SDK versions"""
        versions = {}

        try:
            url = "https://dotnetcli.azureedge.net/dotnet/release-metadata/releases-index.json"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                for release in data.get('releases-index', []):
                    channel = release.get('channel-version', '')
                    latest_sdk = release.get('latest-sdk', '')

                    if channel and latest_sdk:
                        # Map channel to version series
                        if channel.startswith('2.1'):
                            versions['dotnetsdk_2.1'] = latest_sdk
                        elif channel.startswith('2.2'):
                            versions['dotnetsdk_2.2'] = latest_sdk
                        elif channel.startswith('3.0'):
                            versions['dotnetsdk_3.0'] = latest_sdk
                        elif channel.startswith('3.1'):
                            versions['dotnetsdk_3.1'] = latest_sdk
                        elif channel.startswith('5.0'):
                            versions['dotnetsdk_5.0'] = latest_sdk
                        elif channel.startswith('6.0'):
                            versions['dotnetsdk_6.0'] = latest_sdk
                        elif channel.startswith('7.0'):
                            versions['dotnetsdk_7.0'] = latest_sdk
                        elif channel.startswith('8.0'):
                            versions['dotnetsdk_8.0'] = latest_sdk
                        elif channel.startswith('9.0'):
                            versions['dotnetsdk_9.0'] = latest_sdk
                        elif channel.startswith('10.0'):
                            versions['dotnetsdk_10.0'] = latest_sdk

        except Exception as e:
            if self.debug:
                print(f"Error fetching .NET versions: {e}")

        return versions

    def get_openjdk_versions(self) -> Dict[str, str]:
        """Get latest OpenJDK versions from Azul Zulu"""
        versions = {}
        java_versions = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26]

        for java_ver in java_versions:
            # Get both x64 and aarch64 versions
            for arch in ['x64', 'aarch64']:
                try:
                    # Always require tar.gz. Without this filter, Azul's API returns
                    # dmg/zip before tar.gz for JDK 25+, and downstream build_package
                    # cannot unpack .dmg — see runtime/build_package get_extension().
                    url = f"https://api.azul.com/metadata/v1/zulu/packages/?os=macos&arch={arch}&java_version={java_ver}&release_status=ga&java_package_type=jdk&availability_type=CA&javafx_bundled=false&archive_type=tar.gz&page_size=10"

                    response = requests.get(url, timeout=10)

                    pkg = None
                    if response.status_code == 200:
                        data = response.json()
                        # Find the first package without crac or fx in the name
                        for p in data:
                            name = p.get('name', '')
                            if 'crac' not in name and 'fx' not in name:
                                pkg = p
                                break

                    # If no tar.gz found (e.g., for beta versions), try zip
                    if not pkg:
                        url = url.replace('tar.gz', 'zip')
                        response = requests.get(url, timeout=10)

                        if response.status_code == 200:
                            data = response.json()
                            for p in data:
                                name = p.get('name', '')
                                if 'crac' not in name and 'fx' not in name:
                                    pkg = p
                                    # Replace .zip with .tar.gz but add a comment
                                    if not name.endswith('.tar.gz'):
                                        pkg['name'] = pkg.get('name', '').replace('.zip', '.tar.gz') + '  # WARN: FALLBACK FROM ZIP'
                                    break

                    if pkg:
                        # java_version is an array like [21, 0, 8]
                        java_version = pkg.get('java_version', [])
                        if java_version:
                            # Format the version correctly
                            if java_ver == 8:
                                # Java 8 format: 8.0.XXX
                                version_str = f"8.0.{java_version[2] if len(java_version) > 2 else 0}"
                            else:
                                # Java 11+ format: XX.Y.Z
                                version_str = '.'.join(map(str, java_version))

                            # Store version info
                            if arch == 'x64':
                                versions[f'openjdk_{java_ver}'] = {
                                    'version': version_str,
                                    'x64_filename': pkg.get('name', ''),
                                    'x64_distro_version': pkg.get('distro_version', [])
                                }
                            else:
                                # Add aarch64 info to existing entry
                                if f'openjdk_{java_ver}' in versions:
                                    versions[f'openjdk_{java_ver}']['aarch64_filename'] = pkg.get('name', '')
                                    versions[f'openjdk_{java_ver}']['aarch64_distro_version'] = pkg.get('distro_version', [])

                except Exception as e:
                    if self.debug:
                        print(f"Error fetching OpenJDK {java_ver} {arch}: {e}")

        # Convert to simple version format for compatibility
        simple_versions = {}
        for key, value in versions.items():
            if isinstance(value, dict):
                simple_versions[key] = value['version']
                # Store full info for later use in filename generation
                if not hasattr(self, 'openjdk_details'):
                    self.openjdk_details = {}
                self.openjdk_details[key] = value
            else:
                simple_versions[key] = value

        return simple_versions

    def get_httpd_versions(self) -> Dict[str, str]:
        """Get latest Apache HTTPD versions"""
        versions = {}

        # Apache uses different release structure
        try:
            # Check Apache download page for latest version
            url = "https://archive.apache.org/dist/httpd/"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                # Parse HTML to find version directories
                pattern = r'httpd-(\d+\.\d+\.\d+)\.tar\.gz'
                matches = re.findall(pattern, response.text)

                if matches:
                    # Get the latest 2.4.x version
                    versions_list = sorted(matches, key=lambda x: tuple(map(int, x.split('.'))))
                    for v in reversed(versions_list):
                        if v.startswith('2.4.'):
                            versions['httpd_2.4'] = v
                            break

        except Exception as e:
            if self.debug:
                print(f"Error fetching HTTPD versions: {e}")

        # Fallback to GitHub mirror if available
        if not versions:
            data = self.get_github_api("https://api.github.com/repos/apache/httpd/tags")
            if data:
                for tag in data:
                    tag_name = tag.get('name', '')
                    if re.match(r'^\d+\.\d+\.\d+$', tag_name):
                        if tag_name.startswith('2.4.'):
                            versions['httpd_2.4'] = tag_name
                            break

        return versions

    def get_mongodb_versions(self) -> Dict[str, str]:
        """Get latest MongoDB versions from downloads.mongodb.org"""
        versions = {}
        # 5.0 is intentionally excluded: it is EOL. Source releases keep coming
        # (current.json reports 5.0.34+) but macOS binaries are frozen at 5.0.31,
        # so auto-tracking 5.0 only yields phantom versions whose .tgz returns 403
        # and fails the build. 5.0 is pinned manually in packages.conf (commented,
        # x86_64 binary for both arches via Rosetta).
        series_map = ['6.0', '7.0', '8.0', '8.2', '8.3']

        try:
            url = "https://downloads.mongodb.org/current.json"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                found_versions = []

                for v in data.get('versions', []):
                    version = v.get('version', '')
                    # 仅保留纯数字版本号 (X.Y.Z)，排除 rc/alpha/beta
                    if version and re.match(r'^\d+\.\d+\.\d+$', version):
                        found_versions.append(version)

                # Sort versions
                found_versions.sort(key=lambda x: tuple(map(int, x.split('.'))), reverse=True)

                # Match to series
                for serie in series_map:
                    for version in found_versions:
                        if version.startswith(serie + '.'):
                            versions[f'mongodb_{serie}'] = version
                            break

        except Exception as e:
            if self.debug:
                print(f"Error fetching MongoDB versions: {e}")

        return versions

    def get_mongotools_versions(self) -> Dict[str, str]:
        """Get latest MongoDB Database Tools versions from archive"""
        versions = {}
        try:
            url = "https://www.mongodb.com/try/download/database-tools/releases/archive"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                # Find all 100.x.x versions
                matches = re.findall(r'100\.\d+\.\d+', response.text)
                found_versions = list(set(matches))

                if found_versions:
                    # Sort and get latest
                    found_versions.sort(key=lambda x: tuple(map(int, x.split('.'))), reverse=True)
                    versions['mongotools'] = found_versions[0]

        except Exception as e:
            if self.debug:
                print(f"Error fetching MongoTools versions: {e}")

        return versions

    def get_ngrok_version(self) -> Optional[str]:
        """Get latest ngrok version and download tokens from Homebrew Cask API.
        同时将下载 URL 中的 token 保存到 self.ngrok_tokens 供 generate_filename 使用。"""
        try:
            url = "https://formulae.brew.sh/api/cask/ngrok.json"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                version_raw = data.get('version', '')
                if not version_raw:
                    return None

                # brew cask version 字段可能包含逗号分隔的额外信息（如 token），
                # 例如 "3.36.0,a8i6aqunjBw,a"，实际版本号是第一段
                version = version_raw.split(',')[0]

                # 从下载 URL 中提取 token
                # URL 格式: https://bin.ngrok.com/a/<token>/ngrok-v3-...-darwin-<arch>.zip
                # 顶层 url 是 arm64，variations 里是 amd64 (x86_64)
                self.ngrok_tokens = {}

                # 顶层 url → arm64
                url_info = data.get('url', '')
                if url_info:
                    match = re.search(r'/a/([^/]+)/ngrok-v3-', url_info)
                    if match and 'arm64' in url_info:
                        self.ngrok_tokens['arm'] = match.group(1)
                    elif match and 'amd64' in url_info:
                        self.ngrok_tokens['x86'] = match.group(1)

                # variations → amd64 (x86_64)
                for key, var in data.get('variations', {}).items():
                    dl_url = var.get('url', '')
                    match = re.search(r'/a/([^/]+)/ngrok-v3-', dl_url)
                    if match:
                        if 'amd64' in dl_url and 'x86' not in self.ngrok_tokens:
                            self.ngrok_tokens['x86'] = match.group(1)
                        elif 'arm64' in dl_url and 'arm' not in self.ngrok_tokens:
                            self.ngrok_tokens['arm'] = match.group(1)

                if self.debug and self.ngrok_tokens:
                    print(f"    ngrok tokens: {self.ngrok_tokens}")

                return version

        except Exception as e:
            if self.debug:
                print(f"Error fetching ngrok version: {e}")
        return None

    def fetch_latest_versions(self) -> Dict[str, str]:
        """Fetch latest versions for all packages"""
        latest_versions = {}

        # Simple packages with single latest version
        simple_packages = {
            'caddy': ('caddyserver', 'caddy'),
            'cloudflared': ('cloudflare', 'cloudflared'),
            'frp': ('fatedier', 'frp'),
            'mailpit': ('axllent', 'mailpit'),
            'ollama': ('ollama', 'ollama'),
            'minio': ('minio', 'minio'),
            'meilisearch': ('meilisearch', 'meilisearch'),
            'typesense': ('typesense', 'typesense'),
            'bun': ('oven-sh', 'bun'),
            'deno': ('denoland', 'deno'),
            'rust': ('rust-lang', 'rust'),
            'git': ('git', 'git'),
            'memcached': ('memcached', 'memcached'),
            'nginx': ('nginx', 'nginx'),
            'maven': ('apache', 'maven'),
            'subversion': ('apache', 'subversion'),
            'dnsmasq': ('imp', 'dnsmasq'),
            'adminer': ('vrana', 'adminer'),
            'phpMyAdmin': ('phpmyadmin', 'phpmyadmin'),
            'mongosh': ('mongodb-js', 'mongosh'),
            'pinggy': ('Pinggy-io', 'cli-js'),
        }

        print("Fetching latest versions...")

        # Fetch simple packages
        for package, (owner, repo) in simple_packages.items():
            if package not in self.packages:
                continue

            print(f"  Checking {package}...", end=' ')
            sys.stdout.flush()

            # Try releases first
            cleaned = None
            data = self.get_github_api(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
            if data:
                tag = data.get('tag_name', '')
                cleaned = self.clean_version_tag(tag)

            if cleaned:
                latest_versions[package] = cleaned
                print(f"✓ {cleaned}")
            else:
                # Fallback to the releases list when the "latest" release is
                # missing or marked unstable (e.g. apache/maven exposing
                # maven-3.10.0-rc-1 as latest). Prefer non-prerelease entries,
                # then fall further back to tags.
                found = False
                releases = self.get_github_api(f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=30")
                if releases:
                    for r in releases:
                        if r.get('prerelease') or r.get('draft'):
                            continue
                        cleaned = self.clean_version_tag(r.get('tag_name', ''))
                        if cleaned:
                            latest_versions[package] = cleaned
                            print(f"✓ {cleaned}")
                            found = True
                            break
                if not found:
                    tags_data = self.get_github_api(f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=50")
                    if tags_data:
                        for tag in tags_data:
                            cleaned = self.clean_version_tag(tag.get('name', ''))
                            if cleaned:
                                latest_versions[package] = cleaned
                                print(f"✓ {cleaned}")
                                found = True
                                break
                if not found:
                    print("✗ No valid version found")

            time.sleep(self.api_delay)

        # Mongo Tools
        print("  Checking mongotools versions...", end=' ')
        mongotools_versions = self.get_mongotools_versions()
        if 'mongotools' in mongotools_versions:
            latest_versions['mongotools'] = mongotools_versions['mongotools']
            print(f"✓ {mongotools_versions['mongotools']}")
        else:
            print("✗ Failed")

        # ngrok
        print("  Checking ngrok version...", end=' ')
        ngrok_version = self.get_ngrok_version()
        if ngrok_version:
            latest_versions['ngrok'] = ngrok_version
            print(f"✓ {ngrok_version}")
        else:
            print("✗ Failed")

        # HTTPD
        print("  Checking httpd versions...", end=' ')
        httpd_versions = self.get_httpd_versions()
        if 'httpd_2.4' in httpd_versions:
            latest_versions['httpd'] = httpd_versions['httpd_2.4']
        print(f"✓ Latest: {httpd_versions.get('httpd_2.4', 'None')}")

        # Fetch multi-version packages
        print("\n  Fetching multi-version packages...")

        # Node.js
        print("  Checking node versions...", end=' ')
        node_versions = self.get_node_versions()
        print(f"✓ Found {len(node_versions)} series")

        # PHP
        print("  Checking php versions...", end=' ')
        php_versions = self.get_php_versions()
        print(f"✓ Found {len(php_versions)} series")

        # MariaDB
        print("  Checking mariadb versions...", end=' ')
        mariadb_versions = self.get_mariadb_versions()
        print(f"✓ Found {len(mariadb_versions)} series")

        # PostgreSQL
        print("  Checking postgresql versions...", end=' ')
        postgresql_versions = self.get_postgresql_versions()
        print(f"✓ Found {len(postgresql_versions)} series")

        # MySQL
        print("  Checking mysql versions...", end=' ')
        mysql_versions = self.get_mysql_versions()
        print(f"✓ Found {len(mysql_versions)} series")

        # Python
        print("  Checking python versions...", end=' ')
        python_versions = self.get_python_versions()
        print(f"✓ Found {len(python_versions)} series")

        # Go
        print("  Checking go versions...", end=' ')
        go_versions = self.get_go_versions()
        print(f"✓ Found {len(go_versions)} series")

        # .NET SDK
        print("  Checking dotnetsdk versions...", end=' ')
        dotnet_versions = self.get_dotnet_versions()
        print(f"✓ Found {len(dotnet_versions)} series")

        # OpenJDK
        print("  Checking openjdk versions...", end=' ')
        openjdk_versions = self.get_openjdk_versions()
        print(f"✓ Found {len(openjdk_versions)} series")

        # MongoDB
        print("  Checking mongodb versions...", end=' ')
        mongodb_versions = self.get_mongodb_versions()
        print(f"✓ Found {len(mongodb_versions)} series")

        # Ruby
        print("  Checking ruby versions...", end=' ')
        ruby_versions = self.get_ruby_versions()
        print(f"✓ Found {len(ruby_versions)} series")

        # Redis
        print("  Checking redis versions...", end=' ')
        redis_versions = self.get_redis_versions()
        print(f"✓ Found {len(redis_versions)} series")

        # Composer
        print("  Checking composer versions...", end=' ')
        composer_versions = self.get_composer_versions()
        print(f"✓ Found {len(composer_versions)} series")

        # Add multi-version results to latest_versions
        for versions_dict in [node_versions, php_versions, mariadb_versions, postgresql_versions,
                              mysql_versions, python_versions, go_versions, dotnet_versions,
                              openjdk_versions, mongodb_versions, ruby_versions, redis_versions,
                              composer_versions]:
            for key, version in versions_dict.items():
                package_name = key.split('_')[0]
                version_series = key.split('_')[1] if '_' in key else ''

                if package_name not in latest_versions:
                    latest_versions[package_name] = {}

                # If package already has a string version (simple), convert to dict to support multi
                if isinstance(latest_versions[package_name], str):
                    latest_versions[package_name] = {'_latest': latest_versions[package_name]}

                if isinstance(latest_versions[package_name], dict):
                    latest_versions[package_name][version_series] = version

        return latest_versions

    def clean_version_tag(self, tag: str) -> Optional[str]:
        """Clean version tag from various prefixes"""
        if not tag:
            return None

        # Remove common prefixes
        prefixes = ['v', 'release-', 'RELEASE.', 'RELEASE_', 'php-', 'mariadb-', 'bun-v',
                    'mysql-', 'node-v', 'python-', 'ruby-', 'maven-', 'r', 'rel_']

        for prefix in prefixes:
            if tag.lower().startswith(prefix.lower()):
                tag = tag[len(prefix):]

        # Filter out unstable versions
        unstable_keywords = ['milestone', 'test', 'rc', 'beta', 'alpha', 'preview', 'nightly', 'dev', 'cvs']
        if any(keyword in tag.lower() for keyword in unstable_keywords):
            return None

        # Handle special formats
        tag = tag.replace('_', '.')
        tag = tag.rstrip('.')

        # Validate - must contain at least one digit
        if not any(c.isdigit() for c in tag):
            return None

        # Must start with a digit
        if not tag[0].isdigit():
            return None

        return tag

    def check_zulu_file_exists(self, filename: str) -> bool:
        """Check if a Zulu file exists on CDN"""
        if not filename:
            return False

        url = f"https://cdn.azul.com/zulu/bin/{filename}"
        try:
            response = requests.head(url, timeout=5)
            return response.status_code == 200
        except:
            return False

    def compare_versions(self, v1: str, v2: str) -> int:
        """Compare two version strings"""
        def normalize(v):
            return [int(x) if x.isdigit() else x for x in re.split(r'[.-]', v)]
        try:
            n1, n2 = normalize(v1), normalize(v2)
            for i in range(max(len(n1), len(n2))):
                p1 = n1[i] if i < len(n1) else 0
                p2 = n2[i] if i < len(n2) else 0
                if isinstance(p1, int) and isinstance(p2, int):
                    if p1 > p2: return 1
                    if p1 < p2: return -1
                else:
                    if str(p1) > str(p2): return 1
                    if str(p1) < str(p2): return -1
            return 0
        except:
            return 0

    def version_to_tuple(self, version: str) -> tuple:
        """Convert version string to tuple for comparison.

        Split each part further into (numeric-prefix, suffix) so mixed forms
        like '19beta2' vs '17' are still orderable (numeric prefix sorts first,
        suffix breaks ties). Each element is (0, int) for pure digits and
        (1, int_prefix, str_suffix) otherwise, keeping all elements
        cross-comparable.
        """
        parts = []
        for part in re.split(r'[.-]', version):
            if part.isdigit():
                parts.append((0, int(part), ''))
            else:
                m = re.match(r'^(\d*)(.*)$', part)
                num_prefix = int(m.group(1)) if m.group(1) else 0
                suffix = m.group(2)
                parts.append((1, num_prefix, suffix))
        return tuple(parts)

    def generate_filename(self, package: str, version: str, arch: str, template: Optional[str] = None) -> str:
        """Generate filename for package version. If template is provided, try to use it."""

        # Special handling for OpenJDK (Zulu naming)
        if package == 'openjdk':
            # Try to get the actual filename from stored details
            if hasattr(self, 'openjdk_details'):
                for key, details in self.openjdk_details.items():
                    if details.get('version') == version:
                        if arch == 'x86' and 'x64_filename' in details:
                            return details['x64_filename']
                        elif arch == 'arm' and 'aarch64_filename' in details:
                            return details['aarch64_filename']

            # Fallback if we can't find the exact filename
            major = version.split('.')[0]
            if arch == 'x86':
                return f"zulu{major}.XX.XX-ca-jdk{version}-macosx_x64.tar.gz  # NEEDS VERIFICATION"
            else:
                return f"zulu{major}.XX.XX-ca-jdk{version}-macosx_aarch64.tar.gz  # NEEDS VERIFICATION"

        # Special handling for PHP
        if package == 'php':
            # dev 版本文件名是 git commit hash，沿用模板
            if '-dev-' in version and template:
                return template
            return f"php-{version}.tar.gz"

        # Special handling for PostgreSQL
        if package == 'postgresql':
            return f"postgresql-{version}.tar.bz2"

        # Special handling for Composer
        if package == 'composer':
            return f"composer.phar"

        # Special handling for Cloudflared
        if package == 'cloudflared':
            if arch == 'x86':
                return f"cloudflared-darwin-amd64.tgz"
            else:
                return f"cloudflared-darwin-arm64.tgz"

        # Special handling for Mailpit
        if package == 'mailpit':
            if arch == 'x86':
                return f"mailpit-darwin-amd64.tar.gz"
            else:
                return f"mailpit-darwin-arm64.tar.gz"

        # Special handling for Deno
        if package == 'deno':
            if arch == 'x86':
                return f"deno-x86_64-apple-darwin.zip"
            else:
                return f"deno-aarch64-apple-darwin.zip"

        # Special handling for meilisearch
        if package == 'meilisearch':
            if arch == 'x86':
                return f"meilisearch-macos-amd64"
            else:
                return f"meilisearch-macos-apple-silicon"

        # Special handling for MongoDB
        if package == 'mongodb':
            if arch == 'x86':
                return f"mongodb-macos-x86_64-{version}.tgz"
            else:
                return f"mongodb-macos-arm64-{version}.tgz"

        # Special handling for Python (capital P)
        if package == 'python':
            return f"Python-{version}.tgz"

        # Special handling for Node.js
        if package == 'node':
            if arch == 'x86':
                return f"node-v{version}-darwin-x64.tar.gz"
            else:
                return f"node-v{version}-darwin-arm64.tar.gz"

        # Special handling for Bun
        if package == 'bun':
            if arch == 'x86':
                return f"bun-darwin-x64.zip"
            else:
                return f"bun-darwin-aarch64.zip"

        # Special handling for Rust
        if package == 'rust':
            if arch == 'x86':
                return f"rust-{version}-x86_64-apple-darwin.tar.gz"
            else:
                return f"rust-{version}-aarch64-apple-darwin.tar.gz"

        # Special handling for .NET SDK
        if package == 'dotnetsdk':
            if arch == 'x86':
                return f"dotnet-sdk-{version}-osx-x64.tar.gz"
            else:
                try:
                    major = int(version.split('.')[0])
                    if major >= 6:
                        return f"dotnet-sdk-{version}-osx-arm64.tar.gz"
                except:
                    pass
                return f"dotnet-sdk-{version}-osx-x64.tar.gz"

        if package == 'adminer':
            return f"adminer-{version}.php"

        if package == 'phpMyAdmin':
            return f"phpMyAdmin-{version}-all-languages.tar.gz"

        if package == 'mongosh':
            if arch == 'x86':
                return f"mongosh-{version}-darwin-x64.zip"
            else:
                return f"mongosh-{version}-darwin-arm64.zip"

        if package == 'mongotools':
            if arch == 'x86':
                return f"mongodb-database-tools-macos-x86_64-{version}.zip"
            else:
                return f"mongodb-database-tools-macos-arm64-{version}.zip"

        # Pinggy
        if package == 'pinggy':
            if arch == 'x86':
                return 'pinggy-macos-x64'
            else:
                return 'pinggy-macos-arm64'

        # ngrok 文件名是下载 token，优先使用从 brew API 获取的最新 token
        if package == 'ngrok':
            if hasattr(self, 'ngrok_tokens') and self.ngrok_tokens:
                token = self.ngrok_tokens.get(arch)
                if token:
                    return token
            # 兜底：沿用已有模板中的 token
            if template:
                return template
            return version

        # Look at existing patterns or template
        if template:
            # Try to replace known old versions in template
            if package in self.packages:
                for v in self.packages[package]:
                    old_ver = v['version']
                    if old_ver and old_ver in template:
                        return template.replace(old_ver, version)

        if package in self.packages and self.packages[package]:
            recent = self.packages[package][-1]
            if arch == 'x86':
                template_to_use = recent['x86_file']
            else:
                template_to_use = recent['arm_file']

            # Try to replace version in template
            for v in self.packages[package]:
                old_ver = v['version']
                if old_ver in template_to_use:
                    return template_to_use.replace(old_ver, version)

        # Default patterns
        return f'{package}-{version}.tar.gz'

    def update_conf_file(self, latest_versions: Dict[str, any]):
        """Update conf file with new versions."""
        if self.dry_run:
            print("\n📋 DRY RUN - No changes will be made")

        updates_made = []
        self.updated_records = []
        self.updated_pkgs = set()

        # Process updates
        # Helper to get versions for a package
        def get_package_updates(pkg):
            updates = []
            if pkg in latest_versions:
                v_data = latest_versions[pkg]
                if isinstance(v_data, dict):
                    for series, ver in v_data.items():
                        updates.append({'series': series, 'latest': ver})
                else:
                    updates.append({'latest': v_data})
            return updates

        # 1. Calculate updates for display
        for package, versions_data in latest_versions.items():
            if isinstance(versions_data, dict):
                # Multi-version package
                for series, version in versions_data.items():
                    # Check if version already exists
                    has_version = any(v['version'] == version for v in self.packages.get(package, []))
                    if not has_version:
                        updates_made.append({
                            'package': package,
                            'series': series,
                            'latest': version
                        })
                        self.record_update(package, version)
            else:
                # Single version package
                version = versions_data
                has_version = any(v['version'] == version for v in self.packages.get(package, []))

                if not has_version:
                    try:
                        newest_existing = max([v['version'] for v in self.packages.get(package, [])],
                                             key=lambda x: self.version_to_tuple(x))

                        if self.compare_versions(version, newest_existing) > 0:
                            updates_made.append({
                                'package': package,
                                'current': newest_existing,
                                'latest': version
                            })
                            self.record_update(package, version)
                    except:
                        pass

        if not updates_made and not self.dry_run:
            print("\n✅ All packages are up to date (version-wise)!")

        # Display updates
        if updates_made:
            print(f"\n📦 Found {len(updates_made)} package update(s):\n")
            for update in updates_made:
                if 'series' in update:
                    print(f"  • {update['package']} {update['series']}: → {update['latest']}")
                elif 'current' in update:
                    print(f"  • {update['package']}: {update['current']} → {update['latest']}")
                else:
                    print(f"  • {update['package']}: → {update['latest']}")
        else:
            print("\n✅ All packages are up to date (no new versions found)")

        if self.dry_run:
            return

        # 2. Apply updates with Block Buffering logic
        with open(self.conf_file, 'r') as f:
            lines = f.readlines()

        new_lines = []

        # Buffer for current package block
        current_package = None
        current_block = []  # List of (line_content, parsed_info)

        def flush_block():
            nonlocal current_package, current_block
            if not current_block:
                return

            # If it's a package block, process and sort it
            if current_package and current_package in latest_versions:
                updates = get_package_updates(current_package)
                # Map series -> update
                updates_map = {}
                for up in updates:
                    series = up.get('series', 'SINGLE')
                    updates_map[series] = up

                # Parse existing lines
                parsed_entries = []
                for line, info in current_block:
                    if info and info['version']:
                        parsed_entries.append({'ver': info['version'], 'line': line, 'info': info})
                    else:
                        parsed_entries.append({'ver': None, 'line': line, 'info': None})

                final_items = []
                processed_series = set()

                def get_series_key(ver):
                    for s_key in updates_map:
                        if s_key == 'SINGLE': return 'SINGLE'
                        if ver == s_key or ver.startswith(s_key + '.') or ver.startswith(s_key + '-'):
                            return s_key
                    return None

                # 1. Handle Updates (Active Lines)
                for s_key, up in updates_map.items():
                    latest = up['latest']

                    # Find matching entries
                    matching_entries = [e for e in parsed_entries if e['ver'] and get_series_key(e['ver']) == s_key]

                    # Check for exact match
                    exact_match = next((e for e in matching_entries if e['ver'] == latest), None)

                    if exact_match:
                        # 版本无更新 → 已编译过，确保注释
                        line = exact_match['line']
                        if not line.strip().startswith('#'):
                            line = "# " + line
                        final_items.append((self.version_to_tuple(latest), line))
                        processed_series.add(s_key)
                    else:
                        # Update needed
                        template_entry = matching_entries[-1] if matching_entries else None
                        if not template_entry and parsed_entries:
                            # Use last package entry as template
                            template_entry = next((e for e in reversed(parsed_entries) if e['info']), None)

                        template_info = template_entry['info'] if template_entry else {}

                        new_ver = latest
                        x86_file = self.generate_filename(current_package, new_ver, 'x86', template_info.get('x86_file'))
                        arm_file = self.generate_filename(current_package, new_ver, 'arm', template_info.get('arm_file'))

                        # 有新版本 → 取消注释（需要被构建脚本遍历和编译）
                        line_str = f"{current_package}\t{new_ver}\t{x86_file}\t{arm_file}"
                        final_items.append((self.version_to_tuple(new_ver), line_str + "\n"))
                        processed_series.add(s_key)

                        # Register Update
                        self.record_update(current_package, new_ver)

                # 2. Handle Leftovers (Orphaned / Unmatched)
                for e in parsed_entries:
                    if not e['info']:
                        # Header/Comment
                        final_items.append(((0,), e['line']))
                        continue

                    s_key = get_series_key(e['ver'])
                    if s_key and s_key in processed_series:
                        # Already processed (either matched or updated/pruned)
                        continue

                    # Orphaned: we have no upstream latest for this series
                    # (e.g. a manually-maintained prerelease like
                    # 'postgresql 19.beta2', or a series we don't track).
                    # Preserve the line verbatim — auto-commenting an active
                    # row here would silently disable a package the human
                    # explicitly enabled.
                    final_items.append((self.version_to_tuple(e['ver']), e['line']))

                # Sort
                final_items.sort(key=lambda x: x[0])

                for _, line_str in final_items:
                    new_lines.append(line_str)

            else:
                # Just write lines as is if no updates or not a tracked package
                for line, _ in current_block:
                    new_lines.append(line)

            current_package = None
            current_block = []

        for line in lines:
            # Analyze line
            stripped = line.strip()

            is_package_line = False
            parsed_info = None
            pkg_name = None

            if stripped and '\t' in stripped:
                # Parse to see if it's a package
                # Remove leading # and spaces for parsing
                clean_content = stripped.lstrip('#').strip()
                parts = clean_content.split('\t')

                if len(parts) >= 3:
                    name = parts[0].strip()
                    if name in self.packages or name in latest_versions:
                        is_package_line = True
                        pkg_name = name
                        parsed_info = {
                            'name': name,
                            'version': parts[1].strip(),
                            'x86_file': parts[2].strip(),
                            'arm_file': parts[3].strip() if len(parts) > 3 else '',
                        }

            if is_package_line:
                # If we were building a block for a DIFFERENT package, flush it
                if current_package and current_package != pkg_name:
                    flush_block()

                current_package = pkg_name
                current_block.append((line, parsed_info))

            else:
                # Not a package line (comment, empty, or partition header)
                # Flush current block if any
                flush_block()

                # Append this line exactly as is
                new_lines.append(line)

        # Flush final block
        flush_block()

        # Write back
        with open(self.conf_file, 'w') as f:
            f.writelines(new_lines)

        print(f"✅ Config file updated: {self.conf_file}")
        print("📝 Old versions have been commented out. New versions are now active.")

        # Print build commands if updates found
        if self.updated_pkgs:
            pkg_list = ",".join(sorted(list(self.updated_pkgs)))
            print("\n🚀 To build updated packages, run:")
            print(f"./build_package -a x86_64 -p {pkg_list}")
            print(f"./build_package -a arm64 -p {pkg_list}")

    def run(self, emit_json=False, emit_json_file=None, all_packages=False):
        """Main execution"""
        print(f"📂 Loading packages from {self.conf_file}...")
        self.load_packages()
        print(f"📊 Found {len(self.packages)} unique package(s)\n")

        if not self.packages:
            print("❌ No packages found in conf file")
            return

        latest_versions = self.fetch_latest_versions()

        print(f"\n🔍 Processed package updates")

        self.update_conf_file(latest_versions)

        records = self.active_package_records() if all_packages else self.updated_records
        if emit_json or emit_json_file:
            self.emit_matrix(records, output_file=emit_json_file, emit_stdout=emit_json)


def main():
    parser = argparse.ArgumentParser(description='Update ServBay packages versions')
    parser.add_argument('--conf', default='packages.conf',
                       help='Path to packages.conf file')
    parser.add_argument('--dry-run', action='store_true',
                       help='Check for updates without modifying files')
    parser.add_argument('--github-token', help='GitHub API token for higher rate limits')
    parser.add_argument('--debug', action='store_true',
                       help='Enable debug output')
    parser.add_argument('--emit-json', action='store_true',
                       help='Print build matrix JSON after checking updates')
    parser.add_argument('--emit-json-file',
                       help='Write build matrix JSON to the given file')
    parser.add_argument('--all', action='store_true',
                       help='Emit a full rebuild matrix from active config entries')

    args = parser.parse_args()

    if args.github_token:
        os.environ['GITHUB_TOKEN'] = args.github_token

    updater = PackageUpdater(conf_file=args.conf, dry_run=args.dry_run, debug=args.debug)
    updater.run(emit_json=args.emit_json, emit_json_file=args.emit_json_file, all_packages=args.all)


if __name__ == "__main__":
    main()
