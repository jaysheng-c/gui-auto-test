# Phase 0 技术验证报告（Spike Report）

> 本文档是方案 Phase 0 的交付物，作用是**在投入框架开发之前，用证据回答一个问题**：
> pywinauto + UIA 能不能稳定看到并操作 app 的控件？
>
> **当前状态：工具链已验证可用；app 本体的探测尚未执行（缺可用的 `app.exe`）。**
> 拿到 exe 后请按 [§4 待执行](#4-待执行拿到-dmasexe-后的操作清单) 补齐 §3 的表格，并据此给出正式门禁结论。

---

## 1. 验证目标

| # | 要确认的事 | 为什么关键 |
|---|---|---|
| 1 | `pywinauto` 在 Python 3.11.5 下可正常导入 | `comtypes` 的 DLL 加载问题是 UIA 后端的常见拦路虎 |
| 2 | 主窗口的 `title` / `title_re` | Qt 窗口 `class_name` 形如 `Qt643QWindowIcon`，随 Qt 小版本变化，**不可依赖，只能靠标题** |
| 3 | Qt 的 UIA 桥是否被激活 | 未激活时控件树只有一个空 Pane，表现为「所有控件都找不到」，极易误判成用例写错 |
| 4 | 关键交互控件是否有非空 `AutomationId` | `AutomationId` ← Qt `objectName`，是唯一稳定的定位主键 |
| 5 | `click_input()` 与 `click()` 哪个生效 | Qt 的 UIA Pattern 支持不完整，默认必须以真实鼠标 `click_input()` 为准 |
| 6 | 自绘控件（图表 / 画布 / 自定义表格）在树中的表现 | UIA 看不到内部结构时，只能按父容器 `rectangle` 做 `click_offset` 相对坐标点击 |

---

## 2. 门禁判定标准

以 `--inspect` 产出的 `summary.txt` 中「**可交互控件覆盖率**」（可交互且带 AutomationId 的控件数 ÷ 可交互控件总数）为准：

| 覆盖率 | 判定 | 后续动作 |
|---|---|---|
| **≥ 70%** | 通过 | 按方案 Phase 1 起继续，以 `auto_id` 为定位主键编写用例 |
| **30% ~ 70%** | 部分通过 | 继续，同时把 `click_offset` 提前实现（本框架已内置），核心控件用 `auto_id`、自绘区域用相对坐标 |
| **< 30%** | 不通过 | 先排查 `QT_ACCESSIBILITY`；仍无改善则暂停，重新评估图像识别方案 —— 此时 `app_at/vision.py` 需从「空入口」升级为实际实现，工期另估 |

覆盖率不是唯一指标。即使总覆盖率达标，也要逐个确认**主流程上的关键控件**（登录框、主菜单、工具栏、主表格）是否都有 `AutomationId`；
关键控件缺失比长尾控件缺失严重得多。

---

## 3. 探测结论

### 3.1 app 本体 —— 待填

> 下列表格在拿到真实 `app.exe` 并执行 `--inspect` 后填写。

| 项 | 结论 |
|---|---|
| 依赖导入（pywinauto / comtypes） | ✅ 已确认，见 §3.2 |
| 主窗口标题 | ⬜ 待填（取 `reports/inspect/*/windows.txt`） |
| 建议的 `main_window.title_re` | ⬜ 待填（当前配置里的 `^app` 是按命名习惯推定的占位值） |
| 主窗口 `class_name` | ⬜ 待填（**仅记录，不作为识别条件**） |
| Qt UIA 桥是否激活 | ⬜ 待填（控件树非空即视为激活；若为空，先确认 `QT_ACCESSIBILITY=1` 已注入且未被外部设为 `0`） |
| 控件总数 / 带 AutomationId 数 | ⬜ 待填 |
| 可交互控件覆盖率 | ⬜ 待填 → 对照 §2 给出判定 |
| 登录框 `AutomationId` | ⬜ 待填 |
| 主菜单 / 工具栏 `AutomationId` | ⬜ 待填 |
| 主表格 `AutomationId` | ⬜ 待填 |
| `click_input()` vs `invoke()` | ⬜ 待填（框架默认 `run.click_by: mouse`，即用 `click_input()`） |
| 自绘控件表现 | ⬜ 待填（记录其父容器 `rectangle`，为 `click_offset` 做准备） |

### 3.2 工具链自检 —— 已完成

由于 app.exe 暂不可用，用 Windows 自带的 **字符映射表 `C:\Windows\System32\charmap.exe`**
（Win32/MFC 应用，控件类型齐全：Button / Edit / ComboBox / CheckBox / Pane / ScrollBar / StatusBar / MenuBar / Text）
对**框架本身**做了端到端验证。这不构成对 app 的门禁结论，但证明了工具链、执行引擎与报告链路都是通的。

**`--inspect` 探测结果**

```
控件总数:                26
带 AutomationId:         19
可交互控件数:            23
可交互且带 AutomationId:  17
可交互控件覆盖率:        73.9%   → 高于 70% 门禁线
控件树导出:              5399 字节，每个控件附带可直接抄用的 child_window(...) 配方
```

**端到端运行结果**（临时自检套件，3 条用例，其中 1 条为故意失败用例，用于验证留证与失败处置）

> 这批自检流程写在 `.selftest/` 下（`_` 开头 = 草稿，不被 `--validate` / `--list` 扰进来），
> 验证完成后已整目录清理，仅保留结论。它们针对的是 charmap 的控件，对 app 无用。

```
用例 ST_LAUNCH_001    PASSED (4.00s)   启动 → 主窗口 → 控件可见 → click_offset → 正常退出
用例 ST_FAIL_003      FAILED (5.49s)   【预期失败】验证截图 + 控件树快照 + on_fail 语义
用例 ST_INTERACT_002  PASSED (9.72s)   include / if / repeat(count 与 over) / set_var / 文本写入 / 勾选
通过率 66.67%   退出码 1（正确：存在失败用例）

只跑应通过的用例（--tags=ok）：2/2 通过，100%，退出码 0（正确）
```

**逐项对照方案第十二节验收标准**

| # | 验收标准 | 状态 | 证据 |
|---|---|---|---|
| 1 | 跑完套件生成 HTML + JSON，退出码正确 | ✅ | `report.html`(1.4 MB，截图内嵌) + `result.json`(45 KB)；有失败 → 1，全通过 → 0 |
| 2 | `--validate` 报出具体文件与字段路径 | ✅ | 正向：4 个冒烟文件 + 5 个自检文件，0 错误 0 警告；负向：构造含 8 类错误的文件，5 错 3 警**全部带字段路径**，EXIT=2 |
| 3 | `--inspect` 输出完整控件树到文件 | ✅ | `control_tree.txt` 5399 字节 |
| 4 | 定位失败自动产出截图 + 控件树快照 | ✅ | 3 组 `.png` + `.txt`（控件树 5.7~5.9 KB） |
| 5 | `continue_on_error` 生效 | ✅ | ST_FAIL_003 失败后 ST_INTERACT_002 仍执行并通过 |
| 6 | teardown 后无残留进程 | ✅ | 运行结束扫描 charmap 进程数 = 0 |
| 7 | 连续 20 次通过率 ≥ 95% | ⬜ | **需真实 app.exe**，且需在目标机器上跑 |
| 8 | 新增动作只改 `actions.py`、新增用例只加 JSON | ✅ | 54 个动作全部通过 `@action` 装饰器注册 |

**过程中暴露并修掉的真实缺陷**（这些是自检的主要价值 —— 它们全都会在第一次跑 app 时命中）

| 缺陷 | 影响 | 修法 |
|---|---|---|
| `Application.window()` 会自动注入 `app=self`，与显式传入的 `process` 冲突 | **每条用例的主窗口等待 100% 失败**，整个链路一步都跑不了 | `app.py`：`wait_main_window` 不传 `process`；在唯一入口 `AppSession.window()` 统一剔除 `process`/`app`/`backend` |
| `print_control_identifiers` 只存在于 `WindowSpecification`，wrapper 上没有 | **控件树导出全空**，`--inspect` 与失败留证双双失效 | `artifacts.control_tree_text()`：用句柄临时构造 spec 取到该方法；失败时退回自行遍历 `children()` 的简化树 |
| `print_control_identifiers` 的 `max_width` 参数在 pywinauto 0.6.9 已被去掉 | 导出抛 `TypeError` | 新增 `compat.supported_kwargs()`，按当前安装版本的实际签名过滤关键字参数 |
| 中文 Windows 控制台默认 GBK，编不出 `✗` 等字符 | rich 写失败在 **logging 内部**抛 `UnicodeEncodeError`，刷出几十行 `--- Logging error ---` 把真实结果完全淹没 | `log._harden_stdio()` 把 stdout/stderr 的错误策略放宽为 `replace`；`cli.py` 的 `✗` 换成 GBK 可编码的 `×` |
| `Step.bind()` 递归绑定了 `steps` / `then` / `else` 子步骤 | **`repeat` 与 `if` 完全不可用** —— 子步骤用外层 resolver 解析，`${item}` / `${index}` 当场报「变量未定义」 | 子步骤改为由 `StepExecutor.run_steps` 在推入块级作用域之后逐个绑定（延迟绑定） |
| 控件读回的文本带行尾符（Edit 写入 `A` 读回 `A\r`） | `set_text` 的 verify 与 `assert_text` 的 eq 双双**假失败**；`set_var` 取到带 `\r` 的变量会污染后续所有比较 | 在 `Element.text()` 这个唯一出口统一行尾并 strip 首尾（内部空白仍严格比较）；需要原始值时用 `assert_property` |
| `include` 只按全局 `flow_dir` 解析子流程 | 把流程按模块分子目录存放时，**所有子流程都找不到** | `StepExecutor` 增加 `base_dir`（= 当前流程所在目录），调用方目录优先、再回退 `flow_dir`；`--validate` 同步加静态存在性检查 |
| `set_var` 的「从控件取值」分支不可达 | 只给 `target` 不给 `value` 的写法直接报错 | 调整判定顺序：先取控件值，再报缺参数 |
| `Settings._set_dotted` 用 `hasattr`/`getattr` 遍历中间节点 | 套件 `app.env.*` 覆盖直接抛 `ConfigError: 配置项不存在` → **`suite_smoke.json` 这个默认套件 EXIT=2，一步都跑不了**（`QT_ACCESSIBILITY` 恰好是激活 Qt UIA 桥的关键项） | `config.py`：中间节点是 `MutableMapping` 时按 key 走，缺层自动补空 dict（dataclass 仍严格报错）；叶子 dict **逐 key 合并**而非整体替换，避免套件只声明一个变量就把 `default.yaml` 里其余的全冲掉 |
| `--validate` 对顶层 `oneOf(套件\|流程)` 直接校验 | 所有子错误被压成一条「不是恰好一个 oneOf 分支成立」，且 `absolute_path` 停在根上 → **一个含 3 处错误的文件只报 1 条、还没有字段路径**，直接违反验收标准 2 | `flow_loader.validate_data`：先用 `_select_branch` 判定这份数据是套件还是流程，再对该分支单独 `iter_errors(data, _schema=branch)`；`_deepest_error` 让 message 与路径成对地从同一个叶子错误上取。修后同一文件报出 5 错 3 警，全部带精确路径 |

> 假失败比漏测更快地摧毁团队对套件的信任 —— 上面第 5、6 两条尤其值得留意，
> 它们的表现形式都是「用例明明是对的却一直红」。
>
> 最后两条则是**交付前最后一轮验证**（跑 README 主用法、而不是跑自检套件）才暴露出来的。
> 教训很直接：自检套件是自己写的，`app` 段刻意保持简单，恰好绕过了真实套件一定会用的
> `app.env` / `app.main_window` 覆盖路径。**自检用例应当尽量长得像用户真正会写的东西。**

### 3.3 最终验证 —— 已完成

对照第四节 CLI 设计逐条跑过，全部符合预期：

```
compileall app_at/                  → 0 错误
--validate                            → 4 个文件 0 错误 0 警告，EXIT=0
--validate（含 8 类错误的负向文件）    → 5 错误 3 警告，全部带字段路径，EXIT=2
--version                             → app_at 0.1.0
--list                                → 套件/流程/用例/标签清单，EXIT=0
--list-actions                        → 54 个动作，8 个分类
--help                                → 参数说明完整
无 --execute                          → 提示缺少测试目标，EXIT=2（框架错误，正确）
--execute=charmap.exe（默认套件）      → 配置加载通过，EXIT=1（用例未通过，正确）
```

最后一条的结果符合预期：`suite_smoke.json` 里 `main_window.title_re` 是 `^app`，
而被测目标是 `charmap.exe`（窗口标题「字符映射表」），所以两条用例都停在基础设施伪步骤
`wait_main_window` 上。关键在于**错误信息直接列出了当前可见的顶层窗口标题**，
照着改 `title_re` 即可，不需要再去猜。这也是「把排查从猜变成看」这条设计原则在框架自身上的体现。

---

## 4. 待执行：拿到 app.exe 后的操作清单

```powershell
# 1) 导出主窗口控件树（--wait 给出手工登录并切到目标界面的时间）
.venv\Scripts\python.exe auto_test.py --inspect --execute=E:/app/app.exe --wait=30 --keep-open

# 2) 对每个关键业务界面重复一次（登录后主页、设备列表、详情面板、各类弹窗）
#    每换一次界面就跑一次 --inspect，产物按时间戳分开存放

# 3) 看 summary.txt 的覆盖率，对照 §2 给出门禁判定，回填 §3.1 的表格

# 4) 把 automation_ids.txt 里「缺少 AutomationId 的可交互控件」部分
#    整理进 docs/objectname_requirements.md，提交给 app 开发

# 5) 用 windows.txt 里的真实标题收窄 config/default.yaml 的 app.main_window.title_re

# 6) 把 test_flow_file/*.json 里占位的 auto_id（login_* / main_*）换成真实值，
#    然后 --validate 复检
.venv\Scripts\python.exe auto_test.py --validate

# 7) 稳定性验证（验收标准 7）：连续 20 轮
.venv\Scripts\python.exe auto_test.py --execute=E:/app/app.exe --repeat=20 --continue-on-error
```

第 7 步跑的时候注意：**机器必须处于交互式桌面会话，运行期间不要人为操作鼠标键盘**。
若报告里出现 `FOCUS_LOST` 标记，那是焦点被抢占导致的失败，不是被测应用的缺陷。

---

## 5. 环境记录

| 项 | 值 |
|---|---|
| 操作系统 | Windows 11（build 26200） |
| Python | 3.11.5（项目 `.venv`） |
| pywinauto | 0.6.9 |
| 后端 | `uia` |
| 自检用的替代应用 | `C:\Windows\System32\charmap.exe`（字符映射表） |

依赖清单见 [requirements.txt](../requirements.txt)。

> 升级 pywinauto / rich 时留意：这两个库都在小版本间改过公开签名。
> 框架已通过 `app_at/compat.py` 按运行时实际签名过滤关键字参数，
> 但**新增调用点时仍应走 `compat.supported_kwargs()`**，不要直接透传可选参数。
