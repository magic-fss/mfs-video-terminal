# MFS 影视终端

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()

命令行影视搜索与播放客户端，基于 **苹果CMS V10 API** 规范构建。支持多API源聚合搜索、自定义源生命周期管理、浏览器/MPV/VLC 多后端播放，以及完整的终端交互式 TUI（Text User Interface）。

---

## 功能特性

| 模块 | 能力描述 |
|:---|:---|
| **聚合搜索** | 并发检索所有启用源，结果自动合并，支持分页导航 |
| **源管理** | 添加、删除、启用/禁用 API 源；默认源受保护，防止误删 |
| **播放解析** | 自动解析苹果CMS多源分组格式（`$$$` / `#` / `$`），按播放源扁平化展示 |
| **多后端播放** | 浏览器（内嵌 HLS.js 自动降级）、MPV、VLC 三种后端 |
| **终端封面** | 基于 PowerShell `ConvertTo-Sixel` 的原生终端图像渲染（可选） |
| **历史记录** | JSON 持久化，同一影片同一集自动去重，保留最近 200 条 |
| **异常分级** | `NetworkError` / `APIError` / `ParseError` / `PlayerError` 独立捕获与日志追踪 |
| **配置热载** | 本地 JSON 配置，脚本目录自包含，便于迁移与版本控制 |
| **指数退避** | 网络层自动重试，退避间隔 1.5ⁿ 秒，上限可配置 |

---

## 安装

### 环境要求
- **Python** ≥ 3.9
- （可选）**PowerShell 7.4+** + 模块 `Microsoft.PowerShell.ConsoleGuiTools`，用于终端封面显示
- （可选）**MPV** 或 **VLC** 可执行文件位于系统 `PATH`

### 依赖安装
```bash
# 方式一：直接安装依赖
pip install -r requirements.txt

# 方式二：从源码构建（推荐）
pip install .
```

---

## 快速开始

### 交互式 TUI（推荐）
直接运行进入菜单驱动界面：
```bash
python mfs_cli.py
```

### CLI 命令模式
```bash
# 1. 聚合搜索（所有启用源同时检索）
python mfs_cli.py search "流浪地球" --page 1

# 2. 查看影片详情（建议指定 --source 提高命中率）
python mfs_cli.py detail 12345 --source "默认源"

# 3. 播放指定集数（--episode 序号从 1 开始）
python mfs_cli.py play 12345 --episode 1 --player browser --source "默认源"

# 4. 查看播放历史
python mfs_cli.py history

# 5. 配置管理
python mfs_cli.py config
python mfs_cli.py config --set default_player mpv --set timeout 20
```

---

## 源管理

项目内置默认源，您可通过以下命令扩展私有或公开源：

```bash
# 添加新源（URL 需以 /provide/vod/ 结尾）
python mfs_cli.py add-source "4K源" "https://example.com/api.php/provide/vod/"

# 列出所有源及其启用状态
python mfs_cli.py list-sources

# 启用 / 禁用指定源
python mfs_cli.py toggle-source "4K源"

# 删除源（默认源受保护，仅可禁用）
python mfs_cli.py remove-source "4K源"
```

> **提示**：若持有批量源资源，可直接编辑 `config/config.json` 中的 `api_sources` 数组实现批量导入。

---

## 项目结构

```
.
├── mfs_cli.py              # 主程序（单文件全功能，含 CLI + TUI）
├── pyproject.toml          # PEP 621 现代 Python 打包配置
├── requirements.txt        # 运行时依赖清单
├── README.md               # 项目说明文档
└── config/                 # 运行时自动生成（与脚本同目录，便于迁移）
    ├── config.json         # 用户配置与 API 源列表
    ├── history.json        # 播放历史持久化
    └── app.log             # 运行日志（按周轮转，保留一月）
```

---

## 配置说明

首次运行时自动在脚本同级目录创建 `config/` 文件夹。关键配置项说明如下：

| 键 | 默认值 | 说明 |
|:---|:---|:---|
| `api_sources` | `[默认源]` | API 源对象数组，每个源含 `name`、`url`、`enabled` |
| `default_player` | `browser` | 默认播放器：`browser` / `mpv` / `vlc` |
| `timeout` | `15` | 单次 HTTP 请求超时秒数 |
| `max_retries` | `3` | 网络异常时的指数退避重试次数 |
| `page_size` | `20` | 每页返回条数（受 API 源限制） |
| `user_agent` | Chrome 125 | 请求 User-Agent，可根据需要覆盖 |

---

## 异常体系

项目采用分级异常设计，便于上层精准捕获与日志归因：

| 异常类型 | 触发场景 | 用户侧表现 |
|:---|:---|:---|
| `NetworkError` | 超时、DNS 失败、连接重置、超过最大重试 | 红色提示，建议检查网络 |
| `APIError` | 非 200 状态码、JSON 解析失败、业务码错误、所有源均失败 | 红色提示，附带源名称与状态码 |
| `ParseError` | 字段缺失、播放地址格式不符 | 黄色提示，通常为源数据不规范 |
| `PlayerError` | 播放器未安装、进程启动失败、不支持的播放器名称 | 红色提示，附带环境检查建议 |

---

## 终端封面显示（可选）

若终端支持 Sixel（Windows Terminal 1.22+ / WezTerm / iTerm2），可启用高清封面内嵌显示：

```powershell
# 1. 安装 PowerShell 7.4+
winget install Microsoft.PowerShell

# 2. 安装 Sixel 模块
Install-Module Microsoft.PowerShell.ConsoleGuiTools -Force -Scope CurrentUser
```

不满足前提时，项目自动跳过封面渲染，不影响任何核心功能。

---

## 开源协议

[MIT License](LICENSE)

---

> **声明**：本项目仅供学习交流使用，请严格遵守当地法律法规及目标站点的服务条款。项目作者不对用户的使用行为承担任何责任。
