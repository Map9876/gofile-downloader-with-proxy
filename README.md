# GoFile Downloader CLI

基于 [GoFileDownloader](https://github.com/Lysagxra/GoFileDownloader) 重构的精简命令行版本，移除了 Rich 富文本依赖，新增 Cloudflare Workers 代理支持。

## 与原项目的区别

| 特性 | 原项目 | 本项目 |
|------|--------|--------|
| 依赖 | requests + rich | 仅 requests |
| UI | Rich 富文本实时面板 | 简洁命令行输出 |
| 代理 | 不支持 | `--proxy` 支持 Cloudflare Workers |
| 文件结构 | 多模块（8+ 文件） | 单包 |
| 批量下载 | URLs.txt 文件 | 暂不支持 |
| 进度条 | Rich 复杂面板 | 终端 `█░` 进度条 |
| 安装方式 | 克隆仓库 | pip install |

## 安装

### 通过 pip 安装（推荐）

```bash
pip install gofile-dl
```

安装后即可全局使用 `gofile-dl` 命令：

```bash
gofile-dl https://gofile.io/d/xxxxx
```

### 通过 pipx 安装（隔离环境）

```bash
pipx install gofile-dl
```

### 从源码安装

```bash
git clone https://github.com/Lysagxra/GoFileDownloader.git
cd GoFileDownloader
pip install .
```

### 直接运行（无需安装）

```bash
pip install requests
python3 gofile_dl/cli.py https://gofile.io/d/xxxxx
```

或：

```bash
python -m gofile_dl https://gofile.io/d/xxxxx
```

## 使用方法

### 基本下载

```bash
gofile-dl https://gofile.io/d/xxxxx
```

### 带密码的相册

```bash
gofile-dl https://gofile.io/d/xxxxx --password MyPass
```

### 使用 Cloudflare Workers 代理（国内必备）

```bash
gofile-dl https://gofile.io/d/xxxxx --proxy https://c.map987.dpdns.org/
```

### 自定义保存目录

```bash
gofile-dl https://gofile.io/d/xxxxx -o /path/to/save
```

### 自定义并发线程数

```bash
gofile-dl https://gofile.io/d/xxxxx -w 8
```

### 顺序下载（默认并发3线程）

```bash
gofile-dl https://gofile.io/d/xxxxx --sequential
```

### 查看版本

```bash
gofile-dl --version
```

### 组合使用

```bash
gofile-dl https://gofile.io/d/xxxxx --proxy https://c.map987.dpdns.org/ -o ./output --password MyPass -w 16
```

## 全部参数

```
positional arguments:
  url                     GoFile URL (e.g. https://gofile.io/d/xxxxx)

options:
  -h, --help              show help message and exit
  --password PASSWORD, -p PASSWORD
                          密码保护的相册密码
  --output OUTPUT, -o OUTPUT
                          保存目录 (默认: ./Downloads)
  --proxy PROXY           Cloudflare Workers 代理 URL 前缀
  --workers WORKERS, -w WORKERS
                          并发下载线程数 (默认: 3)
  --sequential            顺序下载（默认并发下载）
  --version, -V           显示版本号
```

## 代理使用说明

GoFile 在国内时常无法连接，可通过 Cloudflare Workers 反代解决。

`--proxy` 参数会将代理 URL 前缀拼接到所有 GoFile 请求前：

```
原始请求:  https://api.gofile.io/accounts
代理请求:  https://c.map987.dpdns.org/https://api.gofile.io/accounts
```

代理会应用于所有三个阶段的请求（见下方下载流程）。

## 断点续传

如果下载中断，再次运行相同命令即可自动续传。脚本会：
- 检测已下载的部分文件大小
- 使用 HTTP Range 请求从断点继续下载
- 如果下载链接过期，自动刷新后重试
- 最多重试 20 次

## 下载流程

本项目全程走 GoFile API，无 HTML 页面抓取。流程如下：

### 第1步：创建访客账户

```
POST https://api.gofile.io/accounts
→ 获取 accountToken
```

无需注册，GoFile 会自动创建一个临时访客账户，返回的 token 用于后续所有请求的认证。

### 第2步：生成 X-Website-Token

```
基于 accountToken + 当前时间窗口，通过 SHA-256 计算生成
→ 获取 X-Website-Token（动态校验令牌）
```

这是 GoFile 的反爬机制，每 4 小时（14400 秒）变化一次。

### 第3步：获取文件列表

```
GET https://api.gofile.io/contents/{contentId}?cache=true&sortField=createTime&sortDirection=1
Headers:
  Authorization: Bearer {accountToken}
  X-Website-Token: {websiteToken}
  Cookie: accountToken={accountToken}
→ 获取文件名、下载链接、文件夹结构
```

- `contentId` 从 URL 中提取，如 `https://gofile.io/d/5tkZZi` → `5tkZZi`
- 支持递归遍历子文件夹
- 密码保护的相册需传入 SHA-256 哈希后的密码

### 第4步：下载文件

```
GET {download_link}（如 https://store1.gofile.io/download/...）
Headers:
  Cookie: accountToken={accountToken}
  Referer: {下载链接的源站}
→ 流式写入本地文件
```

- 默认 3 线程并发下载（可通过 `-w` 调整）
- 已存在的完整文件自动跳过
- 支持断点续传（HTTP Range）
- 连接失败自动刷新下载链接并重试

### 代理在流程中的应用

当使用 `--proxy https://c.map987.dpdns.org/` 时：

| 步骤 | 原始 URL | 代理后 URL |
|------|----------|------------|
| 创建账户 | `https://api.gofile.io/accounts` | `https://c.map987.dpdns.org/https://api.gofile.io/accounts` |
| 获取文件列表 | `https://api.gofile.io/contents/xxx` | `https://c.map987.dpdns.org/https://api.gofile.io/contents/xxx` |
| 下载文件 | `https://store1.gofile.io/download/...` | `https://c.map987.dpdns.org/https://store1.gofile.io/download/...` |

## PyPI 发布

本项目使用 GitHub Actions 自动发布到 PyPI，通过 API Token 认证（无需绑定 Trusted Publisher）。

### 首次配置

1. **获取 PyPI API Token**
   - 注册 [PyPI](https://pypi.org) 账号
   - 进入 Account settings → API tokens → Add API token
   - Scope 选择整个账号（首次发布时项目还不存在，无法选单个项目）
   - 复制生成的 token（以 `pypi-` 开头，只显示一次）

2. **获取 TestPyPI API Token**（可选）
   - 同样在 [TestPyPI](https://test.pypi.org) 获取

3. **配置 GitHub Secrets**
   - 进入仓库 Settings → Secrets and variables → Actions
   - 添加 secret `PYPI_API_TOKEN`，值为第1步获取的 token
   - （可选）添加 secret `TEST_PYPI_API_TOKEN`

### 发布新版本

1. 更新 `pyproject.toml` 和 `gofile_dl/__init__.py` 中的版本号
2. 提交并打 tag：
   ```bash
   git tag v1.0.1
   git push origin v1.0.1
   ```
3. 在 GitHub Releases 页面基于 tag 创建新 Release
4. GitHub Actions 自动构建并发布

### 手动发布（不用 GitHub Actions）

如果不想用 Actions，也可以本地直接发布：

```bash
pip install build twine
python -m build
twine upload dist/*
```

按提示输入 PyPI 用户名和密码（或 API Token，用户名填 `__token__`，密码填 token）。

## 致谢

- 原项目：[Lysagxra/GoFileDownloader](https://github.com/Lysagxra/GoFileDownloader)
