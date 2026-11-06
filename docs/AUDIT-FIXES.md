# 审计与修复报告（enhancement 分支）

本文件记录 2026-11 对「巢记 ChaoJi」代码库的全量审计结论、已实施的修复，以及测试方式。
审计由 4 个并行智能体分工完成（前端 UI/交互、数据/Markdown 核心、Python 服务器、E2E 基建），
所有修复均在真实浏览器（Playwright + 系统 Chrome）与真实 HTTP 服务下回归通过。

> 快照基线：`chaoji-v2.0.html` 5788 行 / `server.py` 508 行（enhancement 分支起点）。
> 修复后：`chaoji-v2.0.html` 约 6000 行 / `server.py` 约 630 行。

---

## 一、Python 服务端（server.py）

| 编号 | 级别 | 问题 | 修复 |
| --- | --- | --- | --- |
| S-01 | **P0** | 并发保存同一 `.nbk` 时共用固定 `.tmp` 文件，可能把目标文件替换成空文件（数据损坏） | `atomic_write` 改用唯一临时名 `*.tmp.<pid>.<seq>`；同一文件名加锁串行；Windows 下 `os.replace` 失败自动重试 |
| S-02 | **P0** | 任意网页可向 `127.0.0.1:8765` 发 POST，覆盖/删除用户笔记（CSRF）；DNS rebinding 可绕过同源限制 | `safe_origin()` 校验 `Host`/`Origin`：只接受 `127.0.0.1`/`localhost` + 本服务端口；`Origin: null` 与跨源一律 403；无 Origin 的脚本/测试放行 |
| S-03 | **P0** | UTF-8 BOM 或非 JSON 内容的 `.nbk` 会被静默当成空笔记本（看似"数据丢失"） | 读取时剥离 BOM；空文件返回空状态；非 JSON 对象返回 400 明确报错 |
| S-04 | **P1** | `api_load`/`api_delete` 对非法文件名返回 404，与"文件不存在"混淆 | 非法文件名统一返回 400；不存在才返回 404 |
| S-05 | **P1** | Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9，含扩展名）无法创建文件 | `valid_nbk_name` 拒绝保留名（`_RESERVED_STEMS`） |
| S-06 | **P2** | 连接数无上限，本地脚本/恶意页面可耗尽线程与句柄 | `MAX_CONNECTIONS=64` 信号量限流，超出返回 503 |
| S-07 | **P2** | 手写 HTTP 响应缺少 403/503 reason phrase | 补齐状态码文案 |

## 二、前端数据与持久化（chaoji-v2.0.html）

| 编号 | 级别 | 问题 | 修复 |
| --- | --- | --- | --- |
| C-01 | **P0** | 打开/合并/拖入/会话恢复任意 `.nbk` 时，`page.html` 未净化直接 `innerHTML` 渲染 → 存储型 XSS（可窃取全部笔记并外带） | 新增 `cleanPageHtml()`（DOMParser 惰性文档清洗：移除 script/iframe/object/embed/style/svg 等，删除全部 `on*` 属性，拦截 `javascript:`/`vbscript:`/`data:text/html`）与 `normalizeState()`（结构规范化）；所有载入路径统一经 `adoptState` 校验 |
| C-02 | **P1** | `saveToServer` 在写盘期间继续输入仍把 `dirty` 清为 false → 这段改动不进脏检查/关闭提示（丢改动） | 保存前后快照比较：仅当 `JSON.stringify(state)` 未变化才清 dirty，否则重新排程保存；新增 `saveInFlight` 互斥避免并发写 |
| C-03 | **P1** | 服务器模式启动时完全忽略 IndexedDB 草稿：保存失败/强制关闭后的未写回内容无法恢复 | 启动时若检测到 `dirty` 草稿，显示「草稿恢复」横幅（`offerDraftRestore`），可合并恢复 |
| C-04 | **P1** | 智能粘贴 Markdown 后点「撤销」会二次 `execCommand("insertText")`，把正文打乱并重复 | 撤销只恢复粘贴前 HTML 并重建复选框状态，不再二次插入 |
| C-05 | **P1** | 拖入非法 `.nbk` 时先解绑 `fileHandle` 再校验 → 原文件句柄静默丢失，Ctrl+S 变成另存为 | 先用 `normalizeState` 校验，通过后才解绑 |
| C-06 | **P1** | 源码/分屏模式输入后 120ms 内触发全量渲染会把 textarea 回滚旧内容，随后输入覆盖新内容 | `renderEditor` 重置 textarea 前判断 `document.activeElement`；置顶/移动/复制等操作前统一 `flushMdSourceIfNeeded()` |
| C-07 | **P1** | 服务器文件选择器删除"当前打开"的文件后 `serverFile` 不清理，自动保存会把刚删文件重建 | 删除当前文件时清空 `serverFile` 并更新状态/会话 |
| C-08 | **P2** | 服务器会话恢复时文件已不存在，会静默重建同名空笔记本 | `serverLoadFile(name, allowCreate=false)`：恢复场景明确提示"笔记本已不存在" |
| C-09 | **P2** | 删除笔记本/分区后「撤销」，`sel.pg` 可能指向其他分区页面 → 编辑区空白 | 撤销时在恢复的结构中重新定位页面，修正选中状态 |
| C-10 | **P2** | 右键「重命名标题」未同步 `sel.pg`，跨分区操作后编辑区空白 | 同步设置 `sel.pg` |
| C-11 | **P2** | 焦点离开编辑器后代码块快捷栏残留，误点会操作上一个代码块 | 点击编辑器/快捷栏之外区域时收起浮动快捷栏；`updateToolbarState` 早退分支同步隐藏 |
| C-12 | **P2** | Callout/表格/链接弹窗中 Enter 落在「取消」按钮上仍会执行插入 | Enter 仅在焦点不在弹窗底部按钮、且不在 `<select>` 上时触发插入 |
| C-13 | **P2** | 移动端 ≤440px 时导航抽屉右缘被裁切；桌面「折叠侧栏」状态残留导致移动端 ☰ 打不开 | CSS 改用 `min(210px, 56vw)` + `right:0`；窄屏 `toggleSidebar` 清理 `sidebar-collapsed` |
| C-14 | **P2** | 源码模式富文本工具栏仍启用，点击会把内容静默写进隐藏编辑器 | 源码模式下禁用格式按钮（保留模式切换/复制 MD 按钮可用） |
| C-15 | **P2** | Markdown 链接/图片未拦截 `javascript:`、`data:text/html` 等危险协议 | 新增 `safeMdUrl()` 白名单，应用于链接与图片 |
| C-16 | **P2** | `mergeStates` 遇到缺少 `sections`/`pages` 的脏数据会抛异常 | 增加数组/空值防御 |
| C-17 | **P3** | `htmlToMd` 代码围栏固定 3 个反引号，代码内含 ``` 时导出后提前闭合 | 按代码中最长反引号 run 计算围栏长度（至少 3） |
| C-18 | **P3** | 导出下载后立即 `revokeObjectURL` 在部分浏览器会取消下载 | 延迟 1.5s 释放 |
| C-19 | **P3** | 打印/另存 PDF 后 `#printArea` 长期驻留；源码模式导出可能拿到旧内容 | 打印后清理，导出前先 flush 源码 |
| C-20 | **P3** | 搜索筛选状态下编辑页面，卡片就地更新会覆盖搜索高亮 | 就地更新复用 `highlightKeyword()` |
| C-21 | **P3** | “＋”按钮为 `span` 不可键盘操作；基础无障碍缺口 | 加 `role="button" tabindex="0" aria-label`，并增加 Enter/Space 全局委托 |
| C-22 | **P3** | Markdown「按章节拆分」会丢弃"有标题但暂无正文"的空章节 | 标题节点即使正文为空也生成页面 |

> 另：审计确认三态 Markdown 往返（h1/strong/em/u/s/mark/code/a/ul/ol/blockquote/pre/table/todo/callout/img/hr）、
> 搜索、图片 overlay、导入导出、输入法组合态等 12 项交互原本正常，未做改动。

---

## 三、测试

### 一键运行

```bash
python run_all_tests.py          # unit → integration → E2E，汇总表 + 失败详情 + 退出码
```

| 套件 | 文件 | 用例数 | 说明 |
| --- | --- | --- | --- |
| 服务器纯函数单测 | `test_server_unit.py` | 47 断言 | 文件名白名单、保留名、percent_decode、json_escape、safe_origin、atomic_write 并发 |
| 服务器 HTTP 集成测试 | `test_server_integration.py` | 15 | 真实起服务发 HTTP 请求：API 正常/错误流、BOM、非法 JSON、CSRF/Host、路径穿越、并发保存、rename/delete、旧协议 |
| 真实浏览器 E2E | `test_e2e_ui.py` | 17 | Playwright + 系统 Chrome：文件模式 5 例 + 服务器模式 12 例（新建/自动保存/刷新恢复、Markdown 三态、导入导出、搜索/置顶/移动/删除撤销、待办/表格/代码块/Callout/图片） |

E2E 需要 Playwright（`pip install -r requirements-test.txt`），使用系统已安装的 Chrome/Edge，无需
`playwright install`、无需 Node.js。缺少 Playwright 时一键入口将 E2E 判为 SKIP（不算失败）。

当前结果：**unit 47 / integration 15 / E2E 17，全部通过**。

---

## 四、已知遗留（非阻塞）

1. 浏览器 File System Access 授权流程（`showOpenFilePicker`/`showSaveFilePicker` 真实用户授权）无法在 headless 中完全覆盖，建议人工抽测一次。
2. 大文档性能未做量化压测（`syncEditorToPage` 每键触发 `pageText` 全量解析，已有 500 条缓存）。
3. 可访问性仍有提升空间（模态框焦点陷阱、更多 `aria-*`）。
