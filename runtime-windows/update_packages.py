#!/usr/bin/env python3
"""
Windows package version update script for ServBay.
Detects latest versions from GitHub API / official APIs and updates windows-packages.txt.

Based on the macOS runtime-v2/scripts/update_packages.py, adapted for:
- 2-3 column config format: name\tversion[\tfilename]
- Windows-specific packages: simple-acme, git, openssl, apache
- Windows-specific filenames: PHP (VC builds), OpenJDK (win_x64), PostgreSQL (EDB binaries)
"""

import re
import requests
from typing import Dict, Optional, List
import sys
import time
import argparse
import os
import json


class WindowsPackageUpdater:
    # GitHub tags 可能包含从未正式发布的内部开发标签（幽灵标签）。
    # 以下 URL 模板用于通过 HTTP HEAD 请求验证版本是否真实存在于官方下载源。
    # 仅用于通过 GitHub tags 检测版本的软件包，使用官方 API 的软件包无需验证。
    DOWNLOAD_VERIFY_URLS = {
        'mysql': [
            'https://cdn.mysql.com/Downloads/MySQL-{series}/mysql-{version}.tar.gz',
            'https://downloads.mysql.com/archives/get/p/23/file/mysql-{version}.tar.gz',
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

    def __init__(self, conf_file: Optional[str] = None, dry_run: bool = False, debug: bool = False):
        if conf_file:
            self.conf_file = conf_file
        else:
            # Default to windows-packages.txt in the same directory as this script
            self.conf_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'windows-packages.txt')

        self.packages = {}
        self.api_delay = 0.3  # Delay between API calls
        self.dry_run = dry_run
        self.debug = debug
        self.updated_pkgs = set()  # Track updated packages for build command output
        self.updated_records = []  # Structured records for workflow matrix output.
        self.openjdk_details = {}  # Store OpenJDK details for filename generation
        self.ngrok_tokens = {}  # Store ngrok download tokens

    def record_update(self, name: str, version: str):
        """Record a package update once, preserving insertion order."""
        record = {"name": name, "version": version}
        if record not in self.updated_records:
            self.updated_records.append(record)
        self.updated_pkgs.add(f"{name}-{version}")

    def active_package_records(self):
        """Return active package records from windows-packages.txt for full rebuilds."""
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

    @staticmethod
    def _php_version_type(version: str) -> str:
        """Map a PHP version string to PHP-Windows-Portable's build.yml type.

        alpha/beta/RC -> testing (compiled from the php-src pre-release tag),
        a *dev* snapshot -> development, everything else -> stable. build-windows.yml
        turns this into the right dispatch arg (testing_tag / dev_version /
        stable_version)."""
        v = version.lower()
        if any(x in v for x in ('alpha', 'beta', 'rc')):
            return 'testing'
        if 'dev' in v:
            return 'development'
        return 'stable'

    def build_matrix(self, records):
        """Build GitHub Actions matrix JSON for Windows package builds."""
        include = []
        seen = set()
        for record in records:
            name = record["name"]
            version = record["version"]
            key = ("windows", "x64", name, version)
            if key in seen:
                continue
            seen.add(key)
            include.append({
                "os": "windows",
                "arch": "x64",
                "name": name,
                "version": version,
                # Only PHP consumes version_type (build-windows.yml). Emit it for
                # every entry so the workflow matrix schema stays uniform; non-PHP
                # packages get a harmless 'stable' that their branch ignores.
                "version_type": self._php_version_type(version) if name == "php" else "stable",
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
        # 已在配置中的版本无需验证（已经过验证）
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
        """Load current packages from windows-packages.txt (including commented ones).
        Format: name\tversion[\tfilename]
        """
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
                    clean_line = original_line[1:].strip()
                else:
                    clean_line = original_line

                parts = clean_line.split('\t')
                if len(parts) >= 2:
                    package = parts[0].strip()
                    version = parts[1].strip()
                    filename = parts[2].strip() if len(parts) > 2 else ''

                    # Skip header comments like "Package	Version	Filename"
                    if package.lower() == 'package':
                        continue

                    # Skip lines with "NEW VERSION" marker
                    if 'NEW VERSION' in clean_line:
                        continue

                    if package not in self.packages:
                        self.packages[package] = []
                    self.packages[package].append({
                        'version': version,
                        'filename': filename,
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
            # 依次查找: 脚本同目录 → 项目根目录 → runtime-v2/scripts → runtime
            script_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(script_dir)
            candidate_dirs = [
                script_dir,
                project_root,
                os.path.join(project_root, 'runtime-v2', 'scripts'),
                os.path.join(project_root, 'runtime'),
            ]
            for candidate_dir in candidate_dirs:
                env_file = os.path.join(candidate_dir, '.env')
                if os.path.exists(env_file):
                    with open(env_file, 'r') as f:
                        for line in f:
                            if line.startswith('GITHUB_TOKEN='):
                                token = line.split('=', 1)[1].strip()
                                break
                    if token:
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

    # ---------------------------------------------------------------
    # Version detection methods (shared logic with macOS)
    # ---------------------------------------------------------------

    def get_node_versions(self) -> Dict[str, str]:
        """Get latest Node.js versions for major versions 12-25"""
        versions = {}
        try:
            url = "https://nodejs.org/dist/index.json"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                releases = response.json()
                major_versions = {}
                for release in releases:
                    version = release.get('version', '').lstrip('v')
                    if version and not any(x in version for x in ['rc', 'nightly']):
                        major = version.split('.')[0]
                        if major.isdigit():
                            major_num = int(major)
                            if 12 <= major_num <= 25:
                                if major not in major_versions:
                                    major_versions[major] = version

                for major, version in major_versions.items():
                    versions[f'node_{major}'] = version

        except Exception as e:
            if self.debug:
                print(f"Error fetching Node versions: {e}")
        return versions

    def get_php_versions(self) -> Dict[str, str]:
        """Get latest PHP versions for 8.1-8.6 from the official php/php-src tags.

        Mirrors the macOS updater (runtime/update_packages.py:get_php_versions):
        detect the newest UPSTREAM version per series here, and build-windows.yml
        then dispatches our own ServBay/PHP-Windows-Portable compile for it (the
        same detect-upstream -> trigger-own-repo flow python/ollama already use).
        The previous implementation read PHP-Windows-Portable's own releases, so it
        could only ever "discover" versions we had already built and never picked up
        a fresh upstream release.

        Stable series take the newest non-prerelease tag; 8.6 (pre-GA) keeps the
        newest tag including alpha/beta/RC, so a new 8.6.0alphaN is detected and
        compiled as a `testing` build (see _php_version_type)."""
        versions = {}
        series = ['8.1', '8.2', '8.3', '8.4', '8.5', '8.6']

        all_tags = []
        for page in range(1, 6):
            data = self.get_github_api(
                f"https://api.github.com/repos/php/php-src/tags?per_page=100&page={page}"
            )
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            series_versions = {s: [] for s in series}
            for tag in all_tags:
                tag_name = tag.get('name', '')
                # php-src tags are like `php-8.6.0alpha1`; strip the `php-` prefix.
                if tag_name.startswith('php-'):
                    tag_name = tag_name[len('php-'):]
                for serie in series:
                    if tag_name.startswith(serie + '.'):
                        series_versions[serie].append(tag_name)
                        break

            for serie, version_list in series_versions.items():
                if not version_list:
                    continue
                if serie == '8.6':  # pre-GA: keep alpha/beta/RC so new ones compile
                    version_list.sort(key=lambda x: tuple(x.split('.')))
                    versions[f'php_{serie}'] = version_list[-1]
                else:
                    stable_versions = [v for v in version_list
                                       if not any(x in v.lower() for x in ['alpha', 'beta', 'rc'])]
                    if stable_versions:
                        stable_versions.sort(key=lambda x: tuple(map(int, x.split('.'))))
                        versions[f'php_{serie}'] = stable_versions[-1]

        return versions

    def get_mariadb_versions(self) -> Dict[str, str]:
        """Get latest MariaDB versions for major series"""
        versions = {}
        series = ['10.4', '10.5', '10.6', '10.7', '10.8', '10.9', '10.10', '10.11',
                  '11.0', '11.1', '11.2', '11.3', '11.4', '11.5', '11.6', '11.7', '11.8',
                  '12.0', '12.1']

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
        major_versions = list(range(10, 19))  # 10 to 18

        all_tags = []
        for page in range(1, 5):
            data = self.get_github_api(f"https://api.github.com/repos/postgres/postgres/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            major_version_tags = {major: [] for major in major_versions}
            for tag in all_tags:
                tag_name = tag.get('name', '')
                if tag_name.startswith('REL_'):
                    version = tag_name.replace('REL_', '').replace('_', '.')
                    for major in major_versions:
                        if version.startswith(f'{major}.'):
                            major_version_tags[major].append(version)

            for major, version_list in major_version_tags.items():
                if version_list:
                    stable_versions = []
                    for v in version_list:
                        if not any(x in v.upper() for x in ['RC', 'BETA', 'ALPHA']):
                            parts = v.split('.')
                            if all(p.isdigit() for p in parts):
                                stable_versions.append(v)

                    if stable_versions:
                        stable_versions.sort(key=lambda x: tuple(map(int, x.split('.'))))
                        latest = self.select_latest_verified('postgresql', str(major), stable_versions)
                        if latest:
                            versions[f'postgresql_{major}'] = latest

        return versions

    def get_mysql_versions(self) -> Dict[str, str]:
        """Get latest MySQL versions for major series"""
        versions = {}
        series = ['5.5', '5.6', '5.7', '8.0', '8.1', '8.2', '8.3', '8.4',
                  '9.0', '9.1', '9.2', '9.3', '9.4']

        all_tags = []
        for page in range(1, 6):
            data = self.get_github_api(f"https://api.github.com/repos/mysql/mysql-server/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            series_versions = {s: [] for s in series}
            for tag in all_tags:
                tag_name = tag.get('name', '')
                if 'cluster' in tag_name.lower() or '-labs' in tag_name or '-release' in tag_name:
                    continue
                if tag_name.startswith('mysql-') and re.match(r'^mysql-\d+\.\d+\.\d+$', tag_name):
                    version = tag_name.replace('mysql-', '')
                    for serie in series:
                        if version.startswith(serie + '.'):
                            series_versions[serie].append(version)
                            break

            for serie, version_list in series_versions.items():
                if version_list:
                    version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))
                    latest = self.select_latest_verified('mysql', serie, version_list)
                    if latest:
                        versions[f'mysql_{serie}'] = latest

        return versions

    def verify_python_windows_installer(self, version: str) -> bool:
        """验证 Python 版本是否有 Windows amd64 安装包（EOL 版本可能只发布源代码）"""
        url = f"https://www.python.org/ftp/python/{version}/python-{version}-amd64.exe"
        try:
            response = requests.head(url, timeout=10, allow_redirects=True)
            if response.status_code < 400:
                return True
            if self.debug:
                print(f"    Python {version} no Windows installer (HTTP {response.status_code})")
        except requests.exceptions.RequestException:
            pass
        return False

    def get_python_versions(self) -> Dict[str, str]:
        """Get latest Python versions for major.minor series (3.10+, Windows only)"""
        versions = {}
        series = ['3.10', '3.11', '3.12', '3.13', '3.14']

        all_tags = []
        for page in range(1, 5):
            data = self.get_github_api(f"https://api.github.com/repos/python/cpython/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            series_versions = {s: [] for s in series}
            for tag in all_tags:
                tag_name = tag.get('name', '').lstrip('v')
                for serie in series:
                    if tag_name.startswith(serie + '.'):
                        if not any(x in tag_name for x in ['a', 'b', 'rc']):
                            series_versions[serie].append(tag_name)

            for serie, version_list in series_versions.items():
                if version_list:
                    version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))
                    # 从最新版本开始，找第一个有 Windows 安装包的版本
                    for version in reversed(version_list):
                        if self.verify_python_windows_installer(version):
                            versions[f'python_{serie}'] = version
                            break
                        else:
                            print(f"    ⚠ Python {version} has no Windows installer, trying earlier...")

        return versions

    def get_ruby_versions(self) -> Dict[str, str]:
        """Get latest Ruby versions"""
        versions = {}
        series = ['2.4', '2.5', '2.6', '2.7', '3.0', '3.1', '3.2', '3.3', '3.4', '4.0']

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
                if tag_name.startswith('v'):
                    version = tag_name[1:].replace('_', '.')
                    parts = version.split('.')
                    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
                        std_version = f"{parts[0]}.{parts[1]}.{parts[2]}"
                        for serie in series:
                            if std_version.startswith(serie + '.'):
                                series_versions[serie].append(std_version)

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

        all_tags = []
        for page in range(1, 6):
            data = self.get_github_api(f"https://api.github.com/repos/golang/go/tags?per_page=100&page={page}")
            if data:
                all_tags.extend(data)
            else:
                break

        if all_tags:
            version_list = []
            for tag in all_tags:
                tag_name = tag.get('name', '').replace('go', '')
                if not tag_name or any(x in tag_name.lower() for x in ['beta', 'rc', 'weekly', 'release', 'tip']):
                    continue
                if re.match(r'^\d+\.\d+(\.\d+)?$', tag_name):
                    version_list.append(tag_name)

            version_list.sort(key=lambda x: tuple(map(int, x.split('.'))))

            go_series_versions = {}
            for version in version_list:
                parts = version.split('.')
                if len(parts) >= 2:
                    major_minor = f"{parts[0]}.{parts[1]}"
                    if major_minor not in go_series_versions:
                        go_series_versions[major_minor] = []
                    go_series_versions[major_minor].append(version)

            for mm, ver_list in go_series_versions.items():
                try:
                    # 用整数元组比较，避免 float('1.2') == float('1.20') 的问题
                    mm_parts = tuple(int(x) for x in mm.split('.'))
                    if mm_parts >= (1, 20):
                        latest = self.select_latest_verified('go', mm, ver_list)
                        if latest:
                            versions[f'go_{mm}'] = latest
                except ValueError:
                    continue

        return versions

    def get_openjdk_versions(self) -> Dict[str, str]:
        """Get latest OpenJDK versions from Azul Zulu (Windows x64 only)"""
        versions = {}
        java_versions = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25]

        for java_ver in java_versions:
            try:
                url = f"https://api.azul.com/metadata/v1/zulu/packages/?os=windows&arch=x64&java_version={java_ver}&release_status=ga&java_package_type=jdk&availability_type=CA&javafx_bundled=false&archive_type=zip&page_size=10"

                if java_ver >= 25:
                    url = f"https://api.azul.com/metadata/v1/zulu/packages/?os=windows&arch=x64&java_version={java_ver}&java_package_type=jdk&javafx_bundled=false&archive_type=zip&page_size=10"

                response = requests.get(url, timeout=10)

                pkg = None
                if response.status_code == 200:
                    data = response.json()
                    for p in data:
                        name = p.get('name', '')
                        if 'crac' not in name and 'fx' not in name:
                            pkg = p
                            break

                if pkg:
                    java_version = pkg.get('java_version', [])
                    if java_version:
                        if java_ver == 8:
                            version_str = f"8.0.{java_version[2] if len(java_version) > 2 else 0}"
                        else:
                            version_str = '.'.join(map(str, java_version))

                        versions[f'openjdk_{java_ver}'] = version_str
                        self.openjdk_details[f'openjdk_{java_ver}'] = {
                            'version': version_str,
                            'filename': pkg.get('name', ''),
                            'distro_version': pkg.get('distro_version', [])
                        }

            except Exception as e:
                if self.debug:
                    print(f"Error fetching OpenJDK {java_ver}: {e}")

        return versions

    def get_mongodb_versions(self) -> Dict[str, str]:
        """Get latest MongoDB versions from downloads.mongodb.org"""
        versions = {}
        series_map = ['4.4', '5.0', '6.0', '7.0', '8.0', '8.2']

        try:
            url = "https://downloads.mongodb.org/current.json"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()
                found_versions = []
                for v in data.get('versions', []):
                    version = v.get('version', '')
                    if version:
                        # 仅保留纯数字版本号 (X.Y.Z)，排除 rc/alpha/beta
                        if re.match(r'^\d+\.\d+\.\d+$', version):
                            found_versions.append(version)

                found_versions.sort(key=lambda x: tuple(map(int, x.split('.'))), reverse=True)

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
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                matches = re.findall(r'100\.\d+\.\d+', response.text)
                found_versions = list(set(matches))
                if found_versions:
                    found_versions.sort(key=lambda x: tuple(map(int, x.split('.'))), reverse=True)
                    versions['mongotools'] = found_versions[0]

        except Exception as e:
            if self.debug:
                print(f"Error fetching MongoTools versions: {e}")

        return versions

    def get_ngrok_version(self) -> Optional[str]:
        """Get latest ngrok version and Windows download token from ngrok archive page."""
        try:
            url = "https://ngrok.com/download/archive/ngrok/ngrok-v3/stable/ngrok_archive"
            response = requests.get(url, timeout=15)

            if response.status_code == 200:
                html = response.text

                # 提取 Windows amd64 zip 的下载链接和版本号
                # 格式: /a/<token>/ngrok-v3-<version>-windows-amd64.zip
                match = re.search(
                    r'/a/([^/]+)/ngrok-v3-([\d.]+)-windows-amd64\.zip',
                    html
                )
                if match:
                    token = match.group(1)
                    version = match.group(2)
                    self.ngrok_tokens = {'default': token}

                    if self.debug:
                        print(f"    ngrok Windows amd64 token: {token}")

                    return version

        except Exception as e:
            if self.debug:
                print(f"Error fetching ngrok version: {e}")
        return None

    def get_apache_versions(self) -> Dict[str, str]:
        """Get latest Apache HTTPD versions (Windows uses 'apache' package name)"""
        versions = {}
        try:
            url = "https://archive.apache.org/dist/httpd/"
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                pattern = r'httpd-(\d+\.\d+\.\d+)\.tar\.gz'
                matches = re.findall(pattern, response.text)
                if matches:
                    versions_list = sorted(matches, key=lambda x: tuple(map(int, x.split('.'))))
                    for v in reversed(versions_list):
                        if v.startswith('2.4.'):
                            versions['apache_2.4'] = v
                            break

        except Exception as e:
            if self.debug:
                print(f"Error fetching Apache versions: {e}")

        if not versions:
            data = self.get_github_api("https://api.github.com/repos/apache/httpd/tags")
            if data:
                for tag in data:
                    tag_name = tag.get('name', '')
                    if re.match(r'^\d+\.\d+\.\d+$', tag_name):
                        if tag_name.startswith('2.4.'):
                            versions['apache_2.4'] = tag_name
                            break

        return versions

    # ---------------------------------------------------------------
    # Windows-specific version detection methods
    # ---------------------------------------------------------------

    def get_simple_acme_version(self) -> Optional[str]:
        """Get latest simple-acme (win-acme) version from GitHub releases"""
        data = self.get_github_api("https://api.github.com/repos/simple-acme/simple-acme/releases/latest")
        if data:
            tag = data.get('tag_name', '')
            cleaned = self.clean_version_tag(tag)
            return cleaned
        return None

    def get_git_version(self) -> Optional[str]:
        """Get latest Git for Windows version from GitHub releases"""
        data = self.get_github_api("https://api.github.com/repos/git-for-windows/git/releases/latest")
        if data:
            tag = data.get('tag_name', '')
            # Git for Windows tags: v2.49.0.windows.1
            match = re.match(r'v?(\d+\.\d+\.\d+)', tag)
            if match:
                return match.group(1)
        return None

    def get_openssl_version(self) -> Optional[str]:
        """Get latest OpenSSL version from GitHub tags"""
        data = self.get_github_api("https://api.github.com/repos/openssl/openssl/tags?per_page=30")
        if data:
            for tag in data:
                tag_name = tag.get('name', '')
                # OpenSSL tags: openssl-3.5.0, openssl-3.4.1
                match = re.match(r'openssl-(\d+\.\d+\.\d+)$', tag_name)
                if match:
                    version = match.group(1)
                    # Only return stable versions (no alpha/beta/rc)
                    return version
        return None

    # ---------------------------------------------------------------
    # Helper methods
    # ---------------------------------------------------------------

    def clean_version_tag(self, tag: str) -> Optional[str]:
        """Clean version tag from various prefixes"""
        if not tag:
            return None

        prefixes = ['v', 'release-', 'RELEASE.', 'RELEASE_', 'php-', 'mariadb-', 'bun-v',
                     'mysql-', 'node-v', 'python-', 'ruby-', 'maven-', 'r', 'rel_']

        for prefix in prefixes:
            if tag.lower().startswith(prefix.lower()):
                tag = tag[len(prefix):]

        unstable_keywords = ['milestone', 'test', 'rc', 'beta', 'alpha', 'preview', 'nightly', 'dev', 'cvs']
        if any(keyword in tag.lower() for keyword in unstable_keywords):
            return None

        tag = tag.replace('_', '.')
        tag = tag.rstrip('.')

        if not any(c.isdigit() for c in tag):
            return None

        if not tag[0].isdigit():
            return None

        return tag

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
        """Convert version string to tuple for comparison"""
        parts = []
        for part in re.split(r'[.-]', version):
            if part.isdigit():
                parts.append(int(part))
            else:
                parts.append(part)
        return tuple(parts)

    def generate_filename(self, package: str, version: str) -> Optional[str]:
        """Generate filename for packages that need it (PHP, OpenJDK, PostgreSQL, ngrok).
        Returns None for packages where build_windows auto-generates the filename."""

        # PHP: ServBay 自编译 Windows 包，统一命名为 php-{version}-windows-x64.zip
        if package == 'php':
            return f"php-{version}-windows-x64.zip"

        # OpenJDK: use the exact filename from Azul API
        if package == 'openjdk':
            for key, details in self.openjdk_details.items():
                if details.get('version') == version:
                    return details.get('filename', None)
            # Fallback
            major = version.split('.')[0]
            return f"zulu{major}.XX.XX-ca-jdk{version}-win_x64.zip  # NEEDS VERIFICATION"

        # PostgreSQL: EDB binaries follow a fixed pattern
        if package == 'postgresql':
            return f"postgresql-{version}-1-windows-x64-binaries.zip"

        # ngrok: download token
        if package == 'ngrok':
            token = self.ngrok_tokens.get('default', '')
            if token:
                return token
            # Fallback: check existing config for token
            if package in self.packages:
                for v in self.packages[package]:
                    if v['filename']:
                        return v['filename']
            return None

        # All other packages: no filename needed (build_windows auto-generates)
        return None

    # ---------------------------------------------------------------
    # Fetch all versions
    # ---------------------------------------------------------------

    def fetch_latest_versions(self) -> Dict[str, any]:
        """Fetch latest versions for all packages"""
        latest_versions = {}

        # Simple packages with single latest version (via GitHub releases/tags)
        simple_packages = {
            'caddy': ('caddyserver', 'caddy'),
            'cloudflared': ('cloudflare', 'cloudflared'),
            'frp': ('fatedier', 'frp'),
            'mailpit': ('axllent', 'mailpit'),
            'ollama': ('ollama', 'ollama'),
            'meilisearch': ('meilisearch', 'meilisearch'),
            'bun': ('oven-sh', 'bun'),
            'deno': ('denoland', 'deno'),
            'rust': ('rust-lang', 'rust'),
            # memcached: latest version comes from upstream memcached/memcached
            # tags; the Windows binary is built on demand by our fork
            # ServBay/Memcached-Windows-Portable (see build-windows.yml Stage 1).
            'memcached': ('memcached', 'memcached'),
            'nginx': ('nginx', 'nginx'),
            'adminer': ('vrana', 'adminer'),
            'phpmyadmin': ('phpmyadmin', 'phpmyadmin'),
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

            data = self.get_github_api(f"https://api.github.com/repos/{owner}/{repo}/releases/latest")
            if data:
                tag = data.get('tag_name', '')
                cleaned = self.clean_version_tag(tag)
                if cleaned:
                    latest_versions[package] = cleaned
                    print(f"✓ {cleaned}")
                else:
                    print("✗ Invalid version")
            else:
                tags_data = self.get_github_api(f"https://api.github.com/repos/{owner}/{repo}/tags?per_page=20")
                if tags_data:
                    found = False
                    for tag in tags_data:
                        tag_name = tag.get('name', '')
                        cleaned = self.clean_version_tag(tag_name)
                        if cleaned:
                            latest_versions[package] = cleaned
                            print(f"✓ {cleaned}")
                            found = True
                            break
                    if not found:
                        print("✗ No valid version found")
                else:
                    print("✗ Failed")

            time.sleep(self.api_delay)

        # MinIO: tag 格式 RELEASE.2025-10-15T17-29-55Z → 转为 2025.10.15
        if 'minio' in self.packages:
            print("  Checking minio...", end=' ')
            sys.stdout.flush()
            data = self.get_github_api("https://api.github.com/repos/minio/minio/releases/latest")
            if data:
                tag = data.get('tag_name', '')  # RELEASE.2025-10-15T17-29-55Z
                match = re.match(r'RELEASE\.(\d{4})-(\d{2})-(\d{2})T', tag)
                if match:
                    ver = f"{match.group(1)}.{match.group(2)}.{match.group(3)}"
                    latest_versions['minio'] = ver
                    print(f"✓ {ver}")
                else:
                    print("✗ Invalid tag format")
            else:
                print("✗ Failed")
            time.sleep(self.api_delay)

        # Windows-specific packages
        # simple-acme
        if 'simple-acme' in self.packages:
            print("  Checking simple-acme...", end=' ')
            sys.stdout.flush()
            ver = self.get_simple_acme_version()
            if ver:
                latest_versions['simple-acme'] = ver
                print(f"✓ {ver}")
            else:
                print("✗ Failed")
            time.sleep(self.api_delay)

        # git
        if 'git' in self.packages:
            print("  Checking git...", end=' ')
            sys.stdout.flush()
            ver = self.get_git_version()
            if ver:
                latest_versions['git'] = ver
                print(f"✓ {ver}")
            else:
                print("✗ Failed")
            time.sleep(self.api_delay)

        # openssl: 基础依赖库，不参与版本自动刷新，需保持稳定

        # Mongo Tools
        if 'mongotools' in self.packages:
            print("  Checking mongotools versions...", end=' ')
            sys.stdout.flush()
            mongotools_versions = self.get_mongotools_versions()
            if 'mongotools' in mongotools_versions:
                latest_versions['mongotools'] = mongotools_versions['mongotools']
                print(f"✓ {mongotools_versions['mongotools']}")
            else:
                print("✗ Failed")

        # ngrok
        if 'ngrok' in self.packages:
            print("  Checking ngrok version...", end=' ')
            sys.stdout.flush()
            ngrok_version = self.get_ngrok_version()
            if ngrok_version:
                latest_versions['ngrok'] = ngrok_version
                print(f"✓ {ngrok_version}")
            else:
                print("✗ Failed")

        # Apache (Windows uses 'apache' instead of macOS 'httpd')
        if 'apache' in self.packages:
            print("  Checking apache versions...", end=' ')
            sys.stdout.flush()
            apache_versions = self.get_apache_versions()
            if 'apache_2.4' in apache_versions:
                latest_versions['apache'] = apache_versions['apache_2.4']
                print(f"✓ {apache_versions['apache_2.4']}")
            else:
                print("✗ Failed")

        # Fetch multi-version packages
        print("\n  Fetching multi-version packages...")

        print("  Checking node versions...", end=' ')
        sys.stdout.flush()
        node_versions = self.get_node_versions()
        print(f"✓ Found {len(node_versions)} series")

        print("  Checking php versions...", end=' ')
        sys.stdout.flush()
        php_versions = self.get_php_versions()
        print(f"✓ Found {len(php_versions)} series")

        print("  Checking mariadb versions...", end=' ')
        sys.stdout.flush()
        mariadb_versions = self.get_mariadb_versions()
        print(f"✓ Found {len(mariadb_versions)} series")

        print("  Checking postgresql versions...", end=' ')
        sys.stdout.flush()
        postgresql_versions = self.get_postgresql_versions()
        print(f"✓ Found {len(postgresql_versions)} series")

        print("  Checking mysql versions...", end=' ')
        sys.stdout.flush()
        mysql_versions = self.get_mysql_versions()
        print(f"✓ Found {len(mysql_versions)} series")

        print("  Checking python versions...", end=' ')
        sys.stdout.flush()
        python_versions = self.get_python_versions()
        print(f"✓ Found {len(python_versions)} series")

        print("  Checking go versions...", end=' ')
        sys.stdout.flush()
        go_versions = self.get_go_versions()
        print(f"✓ Found {len(go_versions)} series")

        print("  Checking openjdk versions...", end=' ')
        sys.stdout.flush()
        openjdk_versions = self.get_openjdk_versions()
        print(f"✓ Found {len(openjdk_versions)} series")

        print("  Checking mongodb versions...", end=' ')
        sys.stdout.flush()
        mongodb_versions = self.get_mongodb_versions()
        print(f"✓ Found {len(mongodb_versions)} series")

        print("  Checking ruby versions...", end=' ')
        sys.stdout.flush()
        ruby_versions = self.get_ruby_versions()
        print(f"✓ Found {len(ruby_versions)} series")

        print("  Checking redis versions...", end=' ')
        sys.stdout.flush()
        redis_versions = self.get_redis_versions()
        print(f"✓ Found {len(redis_versions)} series")

        print("  Checking composer versions...", end=' ')
        sys.stdout.flush()
        composer_versions = self.get_composer_versions()
        print(f"✓ Found {len(composer_versions)} series")

        # Add multi-version results to latest_versions
        for versions_dict in [node_versions, php_versions, mariadb_versions, postgresql_versions,
                              mysql_versions, python_versions, go_versions,
                              openjdk_versions, mongodb_versions, ruby_versions, redis_versions,
                              composer_versions]:
            for key, version in versions_dict.items():
                package_name = key.split('_')[0]
                version_series = key.split('_')[1] if '_' in key else ''

                if package_name not in latest_versions:
                    latest_versions[package_name] = {}

                if isinstance(latest_versions[package_name], str):
                    latest_versions[package_name] = {'_latest': latest_versions[package_name]}

                if isinstance(latest_versions[package_name], dict):
                    latest_versions[package_name][version_series] = version

        return latest_versions

    # ---------------------------------------------------------------
    # Config file update logic
    # ---------------------------------------------------------------

    def update_conf_file(self, latest_versions: Dict[str, any]):
        """Update windows-packages.txt with new versions using block buffering."""
        if self.dry_run:
            print("\n📋 DRY RUN - No changes will be made")

        updates_made = []
        self.updated_records = []
        self.updated_pkgs = set()

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
                for series, version in versions_data.items():
                    has_version = any(v['version'] == version for v in self.packages.get(package, []))
                    if not has_version:
                        updates_made.append({
                            'package': package,
                            'series': series,
                            'latest': version
                        })
                        self.record_update(package, version)
            else:
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
        current_package = None
        current_block = []  # List of (line_content, parsed_info)

        def flush_block():
            nonlocal current_package, current_block
            if not current_block:
                return

            if current_package and current_package in latest_versions:
                updates = get_package_updates(current_package)
                updates_map = {}
                for up in updates:
                    series = up.get('series', 'SINGLE')
                    updates_map[series] = up

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
                        if s_key == 'SINGLE':
                            return 'SINGLE'
                        if ver == s_key or ver.startswith(s_key + '.') or ver.startswith(s_key + '-'):
                            return s_key
                    return None

                # 1. Handle Updates (Active Lines)
                for s_key, up in updates_map.items():
                    latest = up['latest']

                    matching_entries = [e for e in parsed_entries if e['ver'] and get_series_key(e['ver']) == s_key]
                    exact_match = next((e for e in matching_entries if e['ver'] == latest), None)

                    if exact_match:
                        # 版本无更新 → 已编译过，确保注释
                        line = exact_match['line']
                        if not line.strip().startswith('#'):
                            line = "# " + line
                        final_items.append((self.version_to_tuple(latest), line))
                        processed_series.add(s_key)
                    else:
                        # Update needed - generate new line
                        new_ver = latest
                        filename = self.generate_filename(current_package, new_ver)

                        if filename:
                            line_str = f"{current_package}\t{new_ver}\t{filename}"
                        else:
                            line_str = f"{current_package}\t{new_ver}"

                        # 有新版本 → 取消注释（需要被构建脚本遍历和编译）
                        final_items.append((self.version_to_tuple(new_ver), line_str + "\n"))
                        processed_series.add(s_key)
                        self.record_update(current_package, new_ver)

                # 2. Handle Leftovers (Orphaned / Unmatched)
                for e in parsed_entries:
                    if not e['info']:
                        final_items.append(((0,), e['line']))
                        continue

                    s_key = get_series_key(e['ver'])
                    if s_key and s_key in processed_series:
                        continue

                    line = e['line']
                    if not line.strip().startswith('#'):
                        line = "# " + line.lstrip('#').strip() + "\n"
                    final_items.append((self.version_to_tuple(e['ver']), line))

                # Sort
                final_items.sort(key=lambda x: x[0])

                for _, line_str in final_items:
                    new_lines.append(line_str)

            else:
                for line, _ in current_block:
                    new_lines.append(line)

            current_package = None
            current_block = []

        for line in lines:
            stripped = line.strip()

            is_package_line = False
            parsed_info = None
            pkg_name = None

            if stripped and '\t' in stripped:
                clean_content = stripped.lstrip('#').strip()
                parts = clean_content.split('\t')

                if len(parts) >= 2:
                    name = parts[0].strip()
                    # Skip header line
                    if name.lower() == 'package':
                        new_lines.append(line)
                        continue

                    if name in self.packages or name in latest_versions:
                        is_package_line = True
                        pkg_name = name
                        parsed_info = {
                            'name': name,
                            'version': parts[1].strip(),
                            'filename': parts[2].strip() if len(parts) > 2 else '',
                        }

            if is_package_line:
                if current_package and current_package != pkg_name:
                    flush_block()

                current_package = pkg_name
                current_block.append((line, parsed_info))

            else:
                flush_block()
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
            print(f"./build_windows x64 -p {pkg_list}")

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
    parser = argparse.ArgumentParser(description='Update ServBay Windows package versions')
    parser.add_argument('--conf', help='Path to windows-packages.txt file')
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

    updater = WindowsPackageUpdater(conf_file=args.conf, dry_run=args.dry_run, debug=args.debug)
    updater.run(emit_json=args.emit_json, emit_json_file=args.emit_json_file, all_packages=args.all)


if __name__ == "__main__":
    main()
