# 巢记 ChaoJi

免安装、单文件的富文本笔记应用 —— 一个 HTML 文件， anywhere 运行，数据完全属于你。

> 适用场景：公司管控桌面 / 无软件安装权限的内网机 / 只能以文件方式交付环境。

## 特性

- **单 HTML 文件**：`chaoji.html` 拷到哪用到哪，无安装、无依赖、无联网
- **OneNote 三栏结构**：笔记本 → 分区 → 页面，含增删与全文搜索（标题+正文）
- **富文本编辑**：加粗/斜体/下划线/删除线、H1/H2、列表、引用、高亮，Tab 缩进（Shift+Tab 反缩进）
- **图片**：粘贴截图或文件插入，自动压缩至 1280px，**点击出现蓝框拖拽等比缩放**
- **静默写回保存**：首次 Ctrl+S 授权 .nbk 文件后，以后每 30 秒/按 Ctrl+S 都**无对话框直接写回同一文件**（File System Access API，Chromium 内核浏览器）
- **多机同步**：数据保存在 `.nbk`（JSON）文件，拷到另一台机器「打开笔记本」即可继续
- **数据安全**：关闭未保存标签页弹确认拦截；意外关闭自动留草稿横幅；保存失败自动降级为下载备份
- **状态可见**：状态栏 + 标题旁小徽章，实时显示上次保存时间与未写回标记

## 数据模型

`.nbk` 文件是唯一持久真相源（浏览器存储在企业环境下不可靠）：

```json
{ "app": "chaoji", "ver": 1,
  "notebooks": [ { "id": "...", "name": "笔记本",
    "sections": [ { "id": "...", "name": "分区",
      "pages": [ { "id": "...", "title": "...", "html": "...", "created": 0, "updated": 0 } ] } ] } ] }
```

浏览器 IndexedDB 仅作会话内崩溃恢复镜像，**不作为数据真相源**。

## 使用

1. 把 `chaoji.html` 放到本地任意可写目录（如 `~/Desktop`）
2. 双击用 Chromium 系浏览器打开（360 企业安全浏览器 v13、Chrome、Edge 均可）
3. 首次 Ctrl+S → 另存为 `我的笔记本.nbk` → 之后自动静默写回
4. 下次打开：点「打开笔记本」选中 .nbk，此后顶部绿色横幅可**一键恢复**免选

## 版本

| 版本 | 说明 |
|---|---|
| v0.5 (chaoji-v0.5.html + check.py) | **导出本页为 Word**（.doc，mso 格式带样式，data URI 图片内嵌，文件名自动清洗非法字符）；新增 `check.py` 环境自检（应对缺标准库/无 root 环境，逐项 PASS/FAIL） |
| v0.4 | 右键菜单（笔记本/分区/页面/编辑器四处定制菜单）、重命名、树形层级缩进、页栏头部＋号直接新建页、横幅可关闭、Ctrl+N/Ctrl+Shift+S、server 启动自动开浏览器（--no-browser 可禁） |
| v0.3 (server.py) | 服务器模式：`python3 server.py` 起本地服务，保存免授权直写盘（原子写+防路径穿越），HTML 双模式（http://127.0.0.1:8765 有服务走 API，file:// 打开自动退回 FS Access 模式） |
| v0.2 | Tab 缩进 / 图片拖拽缩放 / 句柄一键恢复 / 标题旁保存时间戳（已移入本地 archive/） |
| v0.1 | MVP：三栏结构、富文本、图片粘贴、搜索、静默写回、草稿恢复（已移入本地 archive/） |

## 缺标准库 / 无 root 环境的应对

**先跑自检**（Linux 端）：

```bash
python3 check.py
```

全部 PASS 直接 `python3 server.py`。遇到 FAIL 的含义和处理：

| FAIL 项 | 含义 | 应对 |
|---|---|---|
| http.server / json / argparse 等标准库缺失 | Python 被精简（如精简容器镜像、只装了 python-minimal） | **不需要服务器模式也能用**：双击 `chaoji-v0.5.html` 走 file:// 浏览器文件模式，功能完全一样，只是保存要授权 FS Access |
| webbrowser 缺失 | 仅影响"自动开浏览器" | 加 `--no-browser` 启动，手动开浏览器输入地址 |
| 家目录不可写 | 放在只读盘 | 换可写路径：`python3 server.py --dir /tmp/chaoji` |
| 无法绑端口 | 极少见 | `--port` 换一个，1024 以上端口不需要 root |

**核心理解**：服务器模式是增强、不是依赖——前端 HTML 本身是**纯浏览器**可用的（file:// 双击即用，保存用浏览器 FS Access API），Python 只是把"保存"从「授权句柄写回」升级为「更省心的 API 直写」。**即使 Python 完全不可用，巢记的核心功能也 100% 可用。**

## 归档

本地 `archive/` 目录存放历史版本 HTML（gitignore，不上传仓库）。历史版本需要时用 git 历史回溯。

## 服务器模式（推荐，Linux 端用法）

无需 root、无第三方依赖（纯标准库）：

```bash
cd ~/notes                       # server.py 和 chaoji-v0.3.html 放同一目录
python3 server.py                # 默认 http://127.0.0.1:8765/，数据目录 = 同目录
python3 server.py --port 8899 --dir ~/mydata   # 可选：自定义端口/数据目录
```

浏览器打开地址即用，保存走 `POST /api/save`（每 30s 自动 + Ctrl+S），**无任何授权弹窗**。

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/ping` | GET | 服务探测（前端据此自动切到服务器模式） |
| `/api/list` | GET | 列数据目录下所有 .nbk（含修改时间） |
| `/api/load/<file>` | GET | 读取指定 .nbk |
| `/api/save` | POST | `{file, state}` 原子写盘（临时文件+rename），文件名白名单校验防路径穿越 |

服务只绑定 127.0.0.1，不暴露局域网。停止：终端 Ctrl+C。
以后续版本照旧文件名带版本号交付。

## 已知约束

- 主保存通道依赖 Chromium 内核 ≥ 86 的 File System Access API（360 企业安全浏览器 v13 / Chrome / Edge 均满足）；Firefox 无此 API，自动降级为「导出备份」下载方式
- `navigator.storage.persist()` 在 file:// 下被拒，故 IndexedDB 只作会话内缓存使用
- 富文本序列化为浏览器 contenteditable 原生 HTML（非 schema 化），不同浏览器打开可能有微小格式差异

## License

MIT
