# GUI 自动化测试框架

## 项目介绍

技术选型：`pywinauto` + `backend="uia"`（Microsoft UI Automation）。测试人员**只写 JSON 流程文件**，
「怎么找控件、怎么操作、怎么断言、失败怎么留证」全部由框架承担。

```
定位主键   Qt objectName → UIA AutomationId
降级链路   可见文本 name/name_re → type + index → parents 祖先链 → click_offset 相对坐标
图像兜底   本期只留空入口（接口与配置就位，调用抛 VisionNotImplementedError）
用例形态   test_flow_file/*.json 声明式流程，框架解释执行
报告产出   单文件 HTML（截图 base64 内嵌）+ result.json + 失败截图 + 失败控件树快照
```

---

## 快速开始

### 1. 环境准备

需要 Windows 交互式桌面会话（不能是服务/无头会话）、Python 3.11+。

```powershell
# 创建虚拟环境（已有 .venv 可跳过）
python -m venv .venv

# 安装依赖
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 探测控件树（第一步一定要做这个）

写用例之前必须先看清 App在 UIA 下长什么样：

```powershell
.venv\Scripts\python.exe auto_test.py --inspect --execute=xxx/app.exe --wait=30 --keep-open
```

`--wait=30` 给出 30 秒手工登录并切到目标界面的时间，`--keep-open` 让应用导出后不关闭。
产物在 `reports/inspect/<时间戳>/`：

| 文件 | 内容 |
|---|---|
| `summary.txt` | 控件总数、带 AutomationId 数、可交互控件覆盖率、门禁判定 |
| `control_tree.txt` | 完整控件树，每个控件都附带可直接抄进 JSON 的 `child_window(...)` 配方 |
| `automation_ids.txt` | 已设置 AutomationId 的控件清单 + **缺少 AutomationId 的可交互控件清单** |
| `windows.txt` | 进程内所有顶层窗口的标题，用于收窄 `main_window.title_re` |

### 3. 跑冒烟套件

```powershell
.venv\Scripts\python.exe auto_test.py --execute=xxx/app.exe
```

不带 `--suite` 时使用 `config/default.yaml` 里的 `default_suite`（默认 `suite_smoke.json`）。
运行结束后打开 `reports/latest.html` 查看报告。

> 首次运行前请按 [docs/spike_report.md](./docs/spike_report.md) 确认 `main_window.title_re`
> 与用例里的 `auto_id` 已换成真实值 —— 仓库中现有的 `login_*` / `main_*` 是按命名规范推定的占位值。

---

## 命令行

`--execute=X` 与 `--execute X` 两种写法等价。

```powershell
# 主用法：跑默认冒烟套件
python auto_test.py --execute=E:/xxx/xx/app.exe

# 指定套件 / 单个流程 / 用例 / 标签
python auto_test.py --execute=... --suite=test_flow_file/suite_smoke.json
python auto_test.py --execute=... --flow=tc001_launch
python auto_test.py --execute=... --case=TC_LOGIN_001 --tags=smoke,login

# 辅助命令（不跑用例）
python auto_test.py --inspect --execute=...      # 导出控件树到 reports/inspect/
python auto_test.py --validate                   # 仅校验所有 JSON，错误精确到字段路径
python auto_test.py --list                       # 列出套件 / 流程 / 用例 / 标签
python auto_test.py --list-actions               # 列出全部可用动作，按分类分组

# 运行控制
python auto_test.py --execute=... --repeat=3 --continue-on-error `
                    --log-level=DEBUG --timeout-scale=1.5 --report-dir=reports
```

### 常用参数

| 参数 | 说明 |
|---|---|
| `--execute PATH` | 被测应用可执行文件。也可设环境变量 `app_EXE` 免敲长路径 |
| `--suite NAME` | 套件文件，缺省用配置里的 `default_suite` |
| `--flow NAME` | 直接跑指定流程（可重复），此时忽略 `--suite` |
| `--case ID` | 只跑指定用例 id（可重复，也可逗号分隔） |
| `--tags LIST` | 只跑带指定标签的用例，逗号分隔，多个标签之间是「或」 |
| `--repeat N` | 整套流程重复 N 轮，用于稳定性验证 |
| `--continue-on-error` | 单条用例失败后继续跑后续用例 |
| `--timeout-scale X` | 全局超时放大系数，慢机器填 1.5，不必逐个改 JSON |
| `--var K=V` | 注入全局变量（可重复），流程中用 `${K}` 引用 |
| `--attach [--pid PID]` | 连接已运行的实例，不启动也不关闭应用（调试用例时很有用） |
| `--validate` | 仅校验流程 JSON |
| `--list` / `--list-actions` | 清单查询 |
| `--inspect` | 导出控件树；配 `--wait SEC` `--keep-open` `--depth N` `--inspect-window RE` |
| `--report-dir DIR` | 报告输出根目录，缺省 `reports` |
| `--no-html` / `--no-json` / `--no-embed` | 控制报告产出；`--no-embed` 让截图不内嵌，HTML 更小 |
| `--log-level LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--allow-script` | 把 `run.allow_script` 置 true，为 `run_py` 这类逃生口动作预留的开关（**该动作本期未实现**，写了会报「未知动作」） |

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 全部通过 |
| `1` | 存在未通过的用例 |
| `2` | 框架或配置错误（未指定应用、JSON 校验失败、应用启动失败等） |

退出码已规范化，后续接 CI 时可直接作为构建结果判定。

---

## 目录结构

```
AutoTestapp/
├── auto_test.py                  # 唯一入口（先设 DPI 感知，再转发 cli.main）
├── requirements.txt
├── config/
│   └── default.yaml              # 全局默认配置
├── app_at/                       # 框架包
│   ├── cli.py                    # argparse：run / validate / inspect / list
│   ├── config.py                 # Settings，yaml + 套件 + CLI + env 多层合并
│   ├── log.py                    # rich 控制台 handler + 滚动文件 handler
│   ├── errors.py                 # 异常体系
│   ├── compat.py                 # pywinauto / rich 的版本兼容垫片
│   ├── context.py                # RunContext：run_id、产物目录、窗口别名表、变量栈
│   ├── app.py                    # AppSession：启动/连接/健康检查/清理
│   ├── locator.py                # LocatorSpec -> pywinauto WindowSpecification
│   ├── element.py                # Element：等待/重试/操作/取值 的统一封装
│   ├── actions.py                # 动作注册表 + @action 装饰器
│   ├── asserts.py                # 断言类动作
│   ├── flow.py                   # Suite / Flow / Step 数据模型
│   ├── flow_loader.py            # JSON 读取、schema 校验、语义检查
│   ├── variables.py              # ${var} 模板解析与作用域栈
│   ├── runner.py                 # FlowRunner / SuiteRunner / StepExecutor
│   ├── artifacts.py              # 截图 + 控件树导出
│   ├── report.py                 # 结果模型 -> JSON + HTML
│   ├── inspect_tool.py           # 控件树探测（--inspect）
│   ├── vision.py                 # 【空入口】图像定位占位
│   └── templates/report.html.j2
├── test_flow_file/
│   ├── flow.schema.json          # JSON Schema，编辑器可直接引用做补全
│   ├── suite_smoke.json
│   ├── common_login.json         # 可复用子流程（被 include，不要列进套件的 flows）
│   ├── tc001_launch.json
│   └── tc002_main_ui.json
├── resources/images/             # 预留给 vision
├── docs/
│   ├── spike_report.md           # 控件可识别性结论与门禁判定
│   ├── objectname_requirements.md# 给 app 开发的 objectName 补充清单
│   └── flow_authoring.md         # 流程编写指南（写用例前必读）
└── reports/                      # 运行产物，已 gitignore
```

---

## 核心概念

### 套件 / 流程 / 步骤

- **套件**（含 `flows` 字段的 JSON）聚合多个流程，并声明本次运行共享的 `vars` 与 `app` / `settings` 覆盖。
- **流程**（一个 JSON 文件 = 一条用例）由 `setup` → `steps` → `teardown` 三段组成。
  `teardown` 无论前面成败都必然执行，其中的失败只记警告、不改变用例结论。
- **步骤**是一次动作或一次断言，`action` 必填，需要定位控件的动作还要给 `target`。

`data_set` 非空时，整条用例按行数重复执行（数据驱动）。

### 定位

`target` 里多个字段同时给出是 **AND 组合**，框架按 `parents → window → 其余条件` 顺序构造 `child_window()`。

| 字段 | 说明 |
|---|---|
| `window` | 窗口别名，默认 `main`；由 `switch_window` 动作动态注册 |
| `auto_id` | **首选**，对应 UIA AutomationId（Qt objectName） |
| `name` / `name_re` | 可见文本，精确 / 正则 |
| `type` | UIA `control_type`，如 `Button` / `Edit` / `ListItem` |
| `class` / `class_re` | `class_name`，Qt 下区分度低，仅作辅助 |
| `index` | 多候选时消歧，**从 1 开始计数** |
| `parents` | 祖先链数组，**自上而下**。强烈建议提供：Qt 控件树常有十几层，不加祖先链会触发全树深搜 |
| `depth` | 从父容器向下的最大搜索层数 |
| `image` | 图像模板路径 —— **schema 保留字段，本期抛未实现** |

字段集是封闭的：拼错字段名不会报错但会静默改变匹配行为，因此 `--validate` 严格校验。

### 变量

优先级（高 → 低）：

```
内置变量 > data_set 当前行 > flow.vars > suite.vars > config.vars > ${env:NAME}
```

内置变量：`__run_id__` `__case_id__` `__step_id__` `__iteration__` `__now__` `__date__` `__time__` `__timestamp__`。
`set_var` 动作可在运行中写入当前用例作用域；给 `target` 不给 `value` 时，把控件文本写进变量。

### 等待与重试

三层，全部超时统一乘以 `timeout_scale`：

1. **定位等待**：`timeout` 内按 `interval` 轮询，超时抛 `ElementNotFoundError`
2. **就绪等待**：操作前默认 `wait('exists enabled visible')`
3. **步骤重试**：`retries` 次，每次重试**重新定位**（避免复用失效的 COM 引用）

### 失败处置

| `on_fail` | 行为 |
|---|---|
| `abort` | 终止当前用例剩余步骤（默认） |
| `continue` | 记录失败，继续下一步 |
| `skip_flow` | 跳过剩余步骤直接 teardown，且不计失败 |

`optional: true` 的步骤失败只记 warning，不判定用例失败。套件级 `continue_on_error` 控制用例之间是否继续。

### 失败自动留证

每次步骤失败落盘三件套，这是本框架降低维护成本的关键：

```
reports/<run_id>/screenshots/<case>_<step>.png   全屏截图
reports/<run_id>/trees/<case>_<step>.txt         控件树快照，直接对着它改 target
reports/<run_id>/run.log                         完整异常栈 + 该步骤解析后的实际参数
```

排查定位失败的正确姿势：打开 `trees/*.txt`，找到目标控件那一行，把它后面括号里的
`child_window(...)` 配方抄进 JSON 的 `target`。

---

## 报告

- **JSON**：`reports/<run_id>/result.json`，结构 `run → suites → cases → steps`，每层含状态、耗时、错误、产物路径。它是唯一事实来源，HTML 由它渲染。
- **HTML**：Jinja2 单文件（内联 CSS + base64 截图），含汇总卡片、环境信息、按流程分组的可折叠步骤表、失败步骤的截图与控件树。
- `reports/latest.html` 与 `reports/latest.txt` 指向最近一次运行。

---

## 配置

`config/default.yaml` 是全局默认值，优先级：

```
CLI 参数 > 套件 JSON 的 app/settings 段 > config/default.yaml > 代码内置默认值
```

几个需要留意的项：

| 配置 | 为什么重要 |
|---|---|
| `app.env.QT_ACCESSIBILITY: "1"` | 缺了它 Qt 的 UIA 桥可能不激活，控件树只有一个空 Pane，表现为「所有控件都找不到」，极易误判成用例写错 |
| `app.main_window.title_re` | Qt 窗口类名形如 `Qt643QWindowIcon`，随 Qt 小版本变化不可依赖，**只能按标题识别** |
| `run.click_by: mouse` | Qt 的 UIA Invoke Pattern 支持不完整，必须用真实鼠标 `click_input()` |
| `run.foreground_check: true` | GUI 自动化独占前台。运行期间人为切窗口会让点击落到别处且**表现为随机失败**；检测到焦点丢失会等待重试，仍失败则在报告中标注 `FOCUS_LOST` 以便与真实缺陷区分 |
| `run.dump_tree_on_fail: true` | 对着控件树快照改 `target` 是最高效的排查方式，建议常开 |

**运行自动化的机器必须处于交互式桌面会话**，运行期间不要人为操作鼠标键盘。建议在专用虚拟机或独立用户会话中运行。

---

## 扩展

框架的两个扩展点都做到「零侵入」：

- **新增一个动作**：在 `app_at/actions.py` 加一个 `@action("名字")` 函数，签名统一为
  `fn(ctx, step, element) -> ActionResult`。不需要改注册表、schema 或其他任何文件。
- **新增一条用例**：在 `test_flow_file/` 加一个 JSON 文件，再把它列进套件的 `flows`。

`image` 定位与 `assert_image` 是预留的图像兜底入口。启用时需要：安装 `opencv-python` / `numpy`
（见 `requirements.txt` 末尾注释），实现 `app_at/vision.py` 里抛 `VisionNotImplementedError` 的函数体，
并把 `config/default.yaml` 的 `vision.enabled` 打开。其余模块无需改动。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/flow_authoring.md](./docs/flow_authoring.md) | **流程编写指南**，写用例前必读：54 个动作清单、target 字段语义、变量、条件与循环、常见坑 |
| [docs/spike_report.md](./docs/spike_report.md) | 控件可识别性探测结论、门禁判定标准、工具链自检记录 |
| [docs/objectname_requirements.md](./docs/objectname_requirements.md) | 给 app 开发的 objectName 命名规范与补充清单 |

## 已知前置条件

1. 需要一个可用的 `app.exe` 才能完成 Phase 0 Spike（控件可识别性门禁）。
2. 需要 app 的主要功能清单与日常操作路径（登录方式、核心业务动作、典型数据），才能编写有意义的冒烟流程。
3. 需与 app 开发确认可按规范补充 `objectName`，并明确这是**测试契约**、变更需同步通知测试侧。
