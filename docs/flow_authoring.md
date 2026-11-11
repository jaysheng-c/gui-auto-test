# 流程编写指南

本文档面向**测试人员**：如何在 `test_flow_file/` 下用 JSON 编写 app 的自动化用例。

阅读顺序建议：先看 [§1 最小可用用例](#1-最小可用用例) 跑通一条，再按需查 [§6 动作清单](#6-动作清单)。
遇到定位失败请直接跳 [§10 调试工作流](#10-调试工作流)。

---

## 目录

1. [最小可用用例](#1-最小可用用例)
2. [两种 JSON 文件：套件与流程](#2-两种-json-文件套件与流程)
3. [流程文件结构](#3-流程文件结构)
4. [步骤的通用字段](#4-步骤的通用字段)
5. [target：控件定位](#5-target控件定位)
6. [动作清单](#6-动作清单)
7. [变量](#7-变量)
8. [流程控制：include / if / repeat](#8-流程控制include--if--repeat)
9. [失败处置与自动留证](#9-失败处置与自动留证)
10. [调试工作流](#10-调试工作流)
11. [常见坑](#11-常见坑)
12. [编写检查清单](#12-编写检查清单)

---

## 1. 最小可用用例

`test_flow_file/tc_demo.json`：

```json
{
  "id": "TC_DEMO_001",
  "name": "启动后主窗口出现",
  "tags": ["smoke"],
  "priority": "P0",
  "timeout": 120,
  "steps": [
    {
      "id": "s1",
      "desc": "确认进程还活着",
      "action": "assert_process_alive"
    },
    {
      "id": "s2",
      "desc": "主窗口标题符合预期",
      "action": "assert_window_title",
      "target": { "window": "main" },
      "expect": "app",
      "op": "contains"
    }
  ]
}
```

校验并运行：

```powershell
.venv\Scripts\python.exe auto_test.py --validate              # 校验 JSON，错误精确到字段路径
.venv\Scripts\python.exe auto_test.py --execute=E:/app/app.exe --flow=tc_demo
```

> **框架会为每条用例单独启动一次应用**：清理残留进程 → 启动 → 等主窗口 → 注册别名 `main`；
> 用例结束后执行 `teardown`，再关闭应用并清理残留进程。所以用例之间互不污染，
> 但也意味着**不要在用例里假设上一条用例留下的界面状态**。
> 调试时想复用已开的实例，加 `--attach`（此时框架不启动也不关闭应用）。

---

## 2. 两种 JSON 文件：套件与流程

`flow.schema.json` 顶层用 `oneOf` 区分二者：

| | 判定依据 | 作用 |
|---|---|---|
| **套件** | 含 `flows` 字段 | 聚合多个流程，声明共享 `vars` 与 `app` / `settings` 覆盖 |
| **流程** | 含 `id` / `steps` / `setup` / `tags` 之一 | 一条用例 |

### 套件示例

```json
{
  "suite": "smoke",
  "name": "app 冒烟套件",
  "vars": { "username": "tester", "password": "${env:app_PWD}" },
  "app": {
    "args": [],
    "startup_timeout": 60,
    "main_window": { "title_re": "^app.*", "timeout": 60 }
  },
  "settings": { "continue_on_error": true },
  "flows": ["tc001_launch.json", "tc002_main_ui.json"]
}
```

- `flows` 按顺序执行。可写文件名、相对路径，`.json` 后缀可省略。
- `app` / `settings` 段覆盖 `config/default.yaml`，**只影响本套件**。
- **被 `include` 引入的子流程不要列进 `flows`** —— 否则它会被当成独立用例跑两遍。
  `--list` 会把这类文件单独列在「可复用子流程」一节，`--validate` 也会给出提示。

### 文件发现规则

`test_flow_file/` 下：

- **子目录会递归扫描**，所以流程可以按模块分目录存放（如 `flows/device/tc001.json`）。
- **`_` 开头的文件名被跳过** —— 给写到一半的草稿留个位置，不会被 `--validate` / `--list` 扰进来。
  反过来：**正式用例千万不要以 `_` 开头**，否则会被静默忽略，表现为「写了个用例但从来没跑过」。
- `flow.schema.json` 自身不算流程文件。

### 在编辑器里启用补全

在 JSON 文件顶部加一行（VS Code / PyCharm 均支持）：

```json
{ "$schema": "./flow.schema.json", ... }
```

之后 `action` / `target` 的字段名、枚举值都有补全与即时校验。
注意 `$schema` 不参与运行时校验，框架用的是 `flow.schema.json` 本体。

---

## 3. 流程文件结构

```json
{
  "id":          "TC_MAIN_002",
  "name":        "登录后主界面关键控件可见",
  "description": "用例意图、前置条件、预期结果的补充说明",
  "tags":        ["smoke", "main"],
  "priority":    "P0",
  "timeout":     120,
  "enabled":     true,
  "skip_reason": null,
  "vars":        { "device_name": "DEV_001" },
  "data_set":    [],
  "setup":       [ /* 步骤 */ ],
  "steps":       [ /* 步骤 */ ],
  "teardown":    [ /* 步骤 */ ]
}
```

| 字段 | 说明 |
|---|---|
| `id` | 用例唯一标识，用于 `--case` 过滤与报告分组。缺省取文件名，**建议显式声明** |
| `name` | 中文名，出现在报告里 |
| `tags` | 标签数组，用于 `--tags` 过滤。建议至少有一个 `smoke` 之类的分级标签 |
| `priority` | `P0` / `P1` / `P2` / `P3`。P0 为核心冒烟，必须每次全绿 |
| `timeout` | 整条用例的超时（秒）。超时判定失败并进入 `teardown` |
| `enabled` | `false` 时跳过该用例，报告记为 SKIP |
| `skip_reason` | 跳过原因，展示在报告中 |
| `vars` | 用例级变量，优先级高于套件级 |
| `data_set` | 数据驱动：每个对象是一轮迭代的变量，**用例整体按行数重复执行** |
| `setup` | 前置步骤。失败会导致用例直接判失败，但仍执行 `teardown` |
| `steps` | 主体步骤，按顺序执行 |
| `teardown` | 清理步骤。**无论前面成败都必然执行**，其中的失败只记警告、不改变用例结论 |

### 数据驱动

```json
{
  "id": "TC_LOGIN_DATA",
  "data_set": [
    { "user": "admin",  "pwd": "a1", "expect_ok": true  },
    { "user": "guest",  "pwd": "x",  "expect_ok": false }
  ],
  "steps": [
    { "action": "set_text", "target": { "auto_id": "login_leUserName" }, "value": "${user}" },
    { "action": "set_text", "target": { "auto_id": "login_lePassword" }, "value": "${pwd}"   },
    { "action": "click",    "target": { "auto_id": "login_btnSubmit" } },
    {
      "action": "if",
      "cond": { "var": "expect_ok", "is_true": true },
      "then": [ { "action": "assert_exists", "target": { "auto_id": "main_tblDeviceList" } } ],
      "else": [ { "action": "assert_exists", "target": { "auto_id": "login_lblError" } } ]
    }
  ]
}
```

报告里每轮迭代会独立成一条结果，`__iteration__` 内置变量给出轮次号。

---

## 4. 步骤的通用字段

```json
{
  "id": "s1",
  "desc": "点击设备列表中的目标设备",
  "action": "click",
  "target": { "window": "main", "auto_id": "main_tblDeviceList" },

  "value": null,
  "expect": null,

  "timeout": 15,
  "interval": 0.5,
  "retries": 1,
  "optional": false,
  "on_fail": "abort",
  "screenshot": "on_fail",
  "dump_tree": true
}
```

| 字段 | 说明 |
|---|---|
| `action` | **必填**，动作名。拼写错误在 `--validate` 与运行时都会被检出 |
| `id` | 步骤标识，用于日志、报告与产物文件命名。缺省用序号 —— **建议手写**，否则产物文件名会是 `s3` 这种看不出含义的东西 |
| `desc` | 这一步在做什么，用中文写清楚，报告里直接展示 |
| `target` | 控件定位条件。对需要定位控件的动作必填 |
| `window` | 窗口别名，等价于 `target.window` 的简写 |
| `value` | 写入类动作的值：`set_text` 的文本、`select_item` 的选项、`set_var` 的变量值 |
| `expect` | 断言类动作的期望值。**整串为单个 `${var}` 时保留原始类型**（数字仍按数字比较） |
| `timeout` | 本步骤定位/等待超时（秒），会再乘以全局 `timeout_scale`。缺省用 `run.default_timeout` |
| `interval` | 轮询间隔（秒），缺省用 `run.poll_interval` |
| `retries` | 失败重试次数。**每次重试都会重新定位控件**，绝不复用已失效的 COM 引用 |
| `optional` | `true` 时本步骤失败只记警告，不判定用例失败。用于非关键的可选交互 |
| `on_fail` | `abort`（默认）/ `continue` / `skip_flow`，见 [§9](#9-失败处置与自动留证) |
| `screenshot` | `always` / `on_fail` / `never`，覆盖全局 `run.screenshot` |
| `dump_tree` | 失败时是否导出控件树快照，覆盖全局 `run.dump_tree_on_fail` |

除上述字段外，**其余键都会作为动作专属参数传入**（如 `click` 的 `button`/`double`、
`click_offset` 的 `dx`/`dy`、`type_keys` 的 `keys`）。schema 对这部分不做封闭约束，
这样新增动作时不需要同步改 schema —— 代价是**动作参数拼错不会被校验出来**，
只会表现为「参数没生效」。写完先用 `--validate` + 一次实跑确认。

> **断言不参与重试。** `AssertFailed` 属于不可重试异常：控件已经找到了、值确实不对，
> 重试三次只会让失败的用例多花三倍时间。`retries` 只对定位失败与操作异常有效。

---

## 5. target：控件定位

多个字段同时给出是 **AND 组合**。框架按 `parents → window → 其余条件` 的顺序构造 `child_window()`。

| 字段 | 说明 |
|---|---|
| `window` | 从哪个窗口开始查找，默认 `main`。别名由 `switch_window` 动作注册 |
| `auto_id` | **【首选】** UIA AutomationId ← Qt objectName。改界面文案不会失效 |
| `name` | 控件可见文本，精确匹配（UIA `Name` 属性） |
| `name_re` | 可见文本正则匹配（`re.search` 语义） |
| `type` | UIA 控件类型：`Button` / `Edit` / `List` / `ListItem` / `CheckBox` / `TabItem` / `Text` / `Pane` / `Window` / `MenuItem` / `Table` … |
| `class` / `class_re` | 控件类名。Qt 下区分度低（`QWidget` / `QLabel`），仅作辅助 |
| `index` | 匹配到多个时取第几个，**从 1 开始计数**（框架内部转成 pywinauto 的 0 基 `found_index`） |
| `parents` | 祖先链数组，**自上而下书写** |
| `depth` | 从父容器向下的最大搜索层数 |
| `best_match` | pywinauto 的模糊文本匹配。容错高但可能命中意外控件，**仅建议调试期使用** |
| `control_id` | Win32 控件 ID，Qt 下通常无效 |
| `desc` | 对这个定位条件的说明，仅用于日志与报告可读性 |
| `image` | **【预留，当前版本未实现】** 图像模板相对路径。本期使用会抛 `VisionNotImplementedError` |

`target` 的字段集是**封闭的**：拼错字段名（如把 `auto_id` 写成 `automation_id`）不会报「未知字段」，
但会静默改变匹配行为，所以 schema 对它严格校验，`--validate` 能直接抓到。

### 降级链路

`auto_id` 不可用时，按这个顺序降级：

```
auto_id  →  name / name_re  →  type + index  →  parents 祖先链收窄  →  click_offset 相对坐标
```

越往后越脆弱。用文本定位时注意 Qt 的快捷键标注：界面上显示「复制(C)」，
UIA 的 `Name` 可能是 `复制(C)` 也可能是 `复制`，**建议用 `name_re` 而不是 `name`**。

### 一定要用 parents

Qt 主窗口的控件树常有十几层，不加祖先链会触发全树深搜，单步可能耗时数秒甚至超时。

```json
{
  "target": {
    "window": "main",
    "auto_id": "main_tblDeviceList",
    "parents": [{ "auto_id": "main_pnlLeft" }]
  }
}
```

`parents` 是数组，**自上而下**书写（最外层祖先在前）。每一项本身也是一个 `target`，
所以可以嵌套 `parents`，但一般一到两层就够了。

### 自绘控件用 click_offset

图表、画布、自定义表格在 UIA 树里通常只表现为一个不透明 Pane。
此时定位到**父容器**，按相对它左上角的像素偏移点击：

```json
{
  "action": "click_offset",
  "target": { "window": "main", "auto_id": "main_chartOverview" },
  "dx": 120,
  "dy": 80
}
```

父容器的 `rect` 可以在 `--inspect` 产出的 `control_tree.txt` / `automation_ids.txt` 里查到。
**前提是父容器有 objectName** —— 见 [objectname_requirements.md](./objectname_requirements.md) 的 P3 项。

---

## 6. 动作清单

共 **54 个**动作。随时可用 `python auto_test.py --list-actions` 查看最新清单与说明。

约定：下表中「值」列的 `value` 指步骤的 `value` 字段，多数动作也接受同义的动作参数（已列出）。

### 窗口（window）

| 动作 | 参数 | 说明 |
|---|---|---|
| `activate_window` | `target` | 把窗口激活并置于前台 |
| `maximize` | `target` | 最大化 |
| `restore` | `target` | 还原 |
| `minimize` | `target` | 最小化 |
| `close_window` | `target`，`force`，`wait` | 关闭窗口；`force=true` 直接结束进程；`wait` 是等待关闭的秒数（默认 10） |
| `switch_window` | **`alias`**，`target`，`states` | 注册窗口别名，后续步骤可用 `target.window` 引用 |
| `wait_window` | `target`，`states` | 等待窗口达到指定状态 |

`states` 是空格分隔的状态集合，可用 `exists` `enabled` `visible` `ready`（= exists+enabled+visible），
默认 `exists`。

`switch_window` 用于弹窗、子窗口。注册后 `main` 依然指向主窗口：

```json
{ "action": "switch_window", "alias": "export", "target": { "name_re": "^导出" }, "timeout": 20 }
```

之后 `{ "target": { "window": "export", "auto_id": "..." } }` 即可。

窗口的「标题」在 `target` 里写 `name` / `name_re`（框架内部会映射到 UIA 的 `title` / `title_re`）。
`switch_window` 只从 `target` 取识别条件，**不读步骤层的 `title_re` 之类的键**；
一个条件都不给会报错。框架会自动把当前被测进程的 pid 加进条件，避免匹配到同名的其他应用窗口。

### 鼠标（mouse）

| 动作 | 参数 | 说明 |
|---|---|---|
| `click` | `target`，`by`，`button`，`double` | 点击。`by=mouse` 走真实鼠标（**Qt 推荐**），`by=invoke` 走 UIA Invoke Pattern |
| `double_click` | `target`，`by`，`button` | 双击 |
| `right_click` | `target` | 右键点击 |
| `click_offset` | `target`，**`dx`**，**`dy`**，`button`，`double` | 按控件内相对坐标点击，用于 UIA 看不到的自绘控件 |
| `drag_drop` | `target`，**`to`**，`button` | 拖拽。`to` 可以是另一个 `target` 对象，或 `[x, y]` 绝对坐标 |
| `scroll` | `target`，`direction`，`amount`，`count` | 滚动。`direction`: `up`/`down`/`left`/`right`；`amount`: `line`/`page`/`document`；`count` 默认 3 |

> **默认用 `mouse` 而不是 `invoke`。** Qt 的 UIA Pattern 支持不完整，`invoke()` 经常
> 「调用成功但界面没反应」。全局默认已在 `config/default.yaml` 设为 `run.click_by: mouse`，
> 单个步骤可用 `by` 覆盖。

### 键盘 / 文本（keyboard）

| 动作 | 参数 | 说明 |
|---|---|---|
| `set_text` | `target`，`value`（或 `text`），`by`，`verify` | 写入文本（先清空）。默认走 ValuePattern，**不受输入法影响**。`verify=true` 会回读校验 |
| `type_keys` | `target`，`value`（或 `keys`），`literal`，`pause` | 发送按键序列。`literal=true` 时按纯文本转义输入 |
| `clear_text` | `target` | 清空文本框 |
| `hotkey` | `target`，`value`（或 `keys`） | 组合键，如 `^s` 保存、`%{F4}` 关闭 |
| `press_key` | `target`，`value`（或 `key`） | 单个按键，如 `ENTER` / `TAB` / `F5` |

按键语法（pywinauto/sendkeys）：`^` = Ctrl，`%` = Alt，`+` = Shift，`{ENTER}` `{TAB}` `{F5}` `{DEL}`。
**要输入的字面文本里含这些符号时，必须用 `type_keys` + `literal: true`**，否则会被当成组合键。

优先用 `set_text` 而不是 `type_keys` 填输入框：前者直接赋值，不受输入法、焦点、
按键时序影响，快且稳。`type_keys` 留给「必须模拟按键」的场景（快捷键、搜索框的即时过滤、回车提交）。

### 控件状态（control）

| 动作 | 参数 | 说明 |
|---|---|---|
| `select_item` | `target`，`value`（或 `item`） | 选择下拉项 / 列表项 / 标签页 |
| `check` | `target` | 勾选。**已勾选则不动，幂等** |
| `uncheck` | `target` | 取消勾选，幂等 |
| `toggle` | `target` | 切换一次勾选状态（非幂等，慎用） |
| `expand` | `target` | 展开树节点 / 下拉框 |
| `collapse` | `target` | 折叠 |
| `set_value` | `target`，`value` | 设置数值（滑块等） |
| `focus` | `target` | 把焦点设到控件上 |
| `menu_select` | `target`，**`path`**，`exact` | 按路径选菜单，如 `"文件->保存"`。**Qt 自绘菜单可能不支持**，此时改用逐级 `click` |

`check` / `uncheck` 是幂等的，`toggle` 不是 —— 重跑用例时 `toggle` 会导致状态不确定，**优先用前两者**。

### 等待（wait）

| 动作 | 参数 | 说明 |
|---|---|---|
| `sleep` | `value`（或 `seconds`，默认 1.0） | 固定等待。**能不用就不用** |
| `wait_exists` | `target`，`states` | 等待控件出现 |
| `wait_gone` | `target` | 等待控件消失，用于等进度条 / 遮罩结束 |
| `wait_enabled` | `target` | 等待控件变为可用 |
| `wait_text` | `target`，`expect`（或 `value`），`match` | 轮询文本直到匹配。`match`: `contains`（默认）/ `equals` / `regex` |

> `sleep` 是随机失败的头号来源：等短了偶发失败，等长了拖慢整个套件。
> **`wait_text` / `wait_exists` / `wait_gone` 匹配即刻返回**，既快又稳，永远优先于 `sleep`。
> `sleep` 的秒数同样会乘 `timeout_scale`。

### 断言（assert）

| 动作 | 参数 | 说明 |
|---|---|---|
| `assert_exists` | `target`，`expect`（默认 `true`） | 断言控件存在；`expect=false` 时反向断言 |
| `assert_not_exists` | `target` | 断言控件不存在（消失） |
| `assert_text` | `target`，**`expect`**，`op`（默认 `eq`） | 断言控件文本 |
| `assert_text_contains` | `target`，**`expect`** | 断言文本包含子串 |
| `assert_text_matches` | `target`，**`expect`** | 断言文本匹配正则 |
| `assert_enabled` | `target`，`expect`（默认 `true`） | 断言可用；`expect=false` 断言不可用 |
| `assert_visible` | `target`，`expect`（默认 `true`） | 断言可见 |
| `assert_checked` | `target`，`expect`（默认 `true`） | 断言勾选状态 |
| `assert_selected` | `target`，`expect` | 断言列表项 / 标签页处于选中状态 |
| `assert_expanded` | `target`，`expect` | 断言树节点 / 下拉框展开状态 |
| `assert_count` | `target`，**`expect`**，`op`（默认 `eq`） | 断言匹配到的控件数量 |
| `assert_item_count` | `target`，**`expect`**，`op` | 断言列表 / 下拉 / 表格的条目数 |
| `assert_window_title` | `target`，**`expect`**，`op`（默认 `contains`） | 断言窗口标题 |
| `assert_property` | `target`，**`property`**，`expect`，`op` | 断言任意 UIA 属性值，用于框架未内置的校验点 |
| `assert_process_alive` | 无 | 断言被测进程仍在运行，用于捕捉崩溃 |

`op` 取值：`eq` / `ne` / `gt` / `ge` / `lt` / `le` / `contains` / `not_contains` / `matches`
（`gt`~`le` 会转成数字比较；`matches` 是正则）。

> **控件文本已被规范化**：框架在读取控件文本时统一行尾（`\r\n` / `\r` → `\n`）并去掉首尾空白，
> **内部空白仍严格比较**。这是因为实测发现不少控件读回的文本带尾随 `\r`，
> 严格相等会造成大量假失败。
>
> 确实需要原始值（含行尾符）时，用 `assert_property` 直接读 UIA 属性：
> ```json
> { "action": "assert_property", "target": { "auto_id": "x" }, "property": "Value", "expect": "A\r" }
> ```

`assert_process_alive` 建议放在每条用例的开头和结尾，它是应用崩溃 / 卡死的看门狗 ——
否则崩溃会表现为后续每一步都「控件找不到」，掩盖真实原因。

### 系统对话框（dialog）

| 动作 | 参数 | 说明 |
|---|---|---|
| `handle_dialog` | `target`（对话框），`button`，`confirm` | 定位对话框并点击指定按钮 |
| `set_file_path` | `target`（对话框），`value`（或 `path`），`confirm` | 在打开 / 保存对话框中填入路径并确认 |

`handle_dialog` 不给 `button` 时按 `confirm` 正则匹配按钮文本，
默认覆盖中英文系统：`确定|确认|是|OK|Ok|Yes|Open|Save|打开|保存`。

`set_file_path` 处理 Windows 原生文件对话框（`class_name` 为 `#32770`）。
Qt6 在 Windows 上默认使用原生对话框，其结构随系统语言变化，因此文件名输入框按
「对话框内第一个 Edit」定位，确认按钮按文本正则匹配，匹配不到时退回回车提交。

```json
{
  "action": "set_file_path",
  "target": { "window": "main", "class": "#32770" },
  "value": "D:/export/result.csv",
  "timeout": 30
}
```

### 流程控制（flow）

见 [§8](#8-流程控制include--if--repeat)。

| 动作 | 参数 | 说明 |
|---|---|---|
| `include` | **`flow`**，`vars` | 执行子流程，共享当前变量作用域 |
| `repeat` | `count` 或 `over`，`steps`，`item_var`，`index_var` | 重复执行子步骤 |
| `if` | **`cond`**，`then`，`else` | 条件分支 |
| `set_var` | **`name`**，`value` 或 `target` | 写入当前用例作用域的变量 |
| `log` | `message`（或 `value`），`level` | 向日志输出一条信息，`level`: `debug`/`info`/`warning`/`error` |

### 预留（当前版本未实现）

`assert_image` / `ocr_text` / `wait_cpu_idle` / `run_py` 本期**没有注册为动作**，写了会在
`--validate` 与运行时报「未知动作」（并给出拼写相近的候选）。`run_py` 是受限逃生口，
即使实现也需要 `--allow-script` 显式开启。

图像定位不同：`target.image` 是 schema 里的**正式保留字段**，接口、路由与配置项都已就位，
调用时抛 `VisionNotImplementedError`。实现位置在 `app_at/vision.py`，其余模块无需改动；
启用前需先安装 `opencv-python` / `numpy`（见 `requirements.txt` 末尾注释）并把 `vision.enabled` 打开。

> 动作名不受 schema 约束（schema 只校验 `action` 是非空字符串），真正的校验来自
> `ACTION_REGISTRY`，`--validate` 会对着它检查每个步骤的 `action`。

---

## 7. 变量

### 语法

```
${name}          引用变量
${env:NAME}      读环境变量（唯一读环境变量的方式，不会与普通变量混淆）
```

字符串里可以嵌多个：`"报告 ${__case_id__} 于 ${__now__} 生成"`。
**整串只有一个 `${var}` 时保留原始类型**（数字仍是数字，布尔仍是布尔），
这样 `assert_count` 的 `expect: "${limit}"` 能按数字比较而不是字符串比较。

### 优先级（高 → 低）

```
内置变量  >  data_set 当前行  >  flow.vars  >  suite.vars  >  config.vars  >  ${env:NAME}
```

### 内置变量

| 变量 | 含义 |
|---|---|
| `__run_id__` | 本次运行的时间戳 ID，也是 `reports/<run_id>/` 目录名 |
| `__case_id__` | 当前用例 id |
| `__step_id__` | 当前步骤 id |
| `__iteration__` | `data_set` / `--repeat` 的当前轮次 |
| `__now__` | `2026-11-06 12:04:54` |
| `__date__` | `2026-11-06` |
| `__time__` | `120454` |
| `__timestamp__` | Unix 秒 |

时间类变量是**惰性求值**的：每次引用都拿当下时刻，不是用例开始时刻。

### 作用域与写入

- **读取**走作用域栈，按上面的优先级。
- **写入**（`set_var`）固定落在**用例作用域**：`setup` 里写的变量，`steps` 和 `teardown` 都能读到；
  但不会泄漏到下一条用例。
- `include` 的 `vars` 参数只在子流程内可见。
- 变量的值本身可以引用别的变量（`${a}` 的值是 `${b}`），框架会链式解析，**上限 8 层**，
  超了按循环引用报错。

```json
{ "action": "set_var", "name": "version", "target": { "auto_id": "main_lblVersion" } }
```

给了 `target` 不给 `value`，就是把控件文本写进变量 —— 用于把界面上的动态数据
（版本号、设备名、生成的单号）传给后续断言。

### 密码等敏感值

**不要写进 JSON**。用环境变量注入：

```powershell
$env:app_PWD = "xxx"
.venv\Scripts\python.exe auto_test.py --execute=...
```

```json
"vars": { "password": "${env:app_PWD}" }
```

变量解析发生在**步骤执行前**（惰性），因此未被任何步骤引用的变量即使取值失败也不影响运行。

---

## 8. 流程控制：include / if / repeat

### include —— 复用子流程

```json
{ "action": "include", "flow": "common_login.json", "vars": { "username": "admin" } }
```

- 子流程**共享调用方的变量作用域**：子流程里 `set_var` 写的变量，调用方后续步骤能读到。
- `vars` 传入的额外变量只在子流程内可见。
- **相对路径以调用方所在目录为基准**解析，找不到才回退到全局 `flow_dir` 与项目根。
  所以流程可以按模块分子目录存放（`flows/device/tc001.json` 引用同目录的 `common.json` 直接写文件名即可）。
  嵌套 include 时，基准目录会跟着切到子流程自己的目录。
- `--validate` 会静态检查子流程是否存在，拼错文件名在校验期就报警告，不用等跑到那一步。
- 递归深度有上限，防止两个子流程互相 include。

被 include 的子流程仍然是合法的流程文件，`--list` 会把它单独列在「可复用子流程」一节。

### if —— 条件分支

```json
{
  "action": "if",
  "timeout": 10,
  "cond": { "exists": { "window": "main", "auto_id": "login_leUserName" } },
  "then": [ { "action": "include", "flow": "common_login.json" } ],
  "else": [ { "action": "log", "level": "info", "message": "已登录，跳过登录流程" } ]
}
```

`cond` 支持的写法：

| 写法 | 含义 |
|---|---|
| `{ "var": "mode", "equals": "admin" }` | 变量相等（宽松比较，`"3"` 能匹配 `3`） |
| `{ "var": "n", "not_equals": "0" }` | 不等 |
| `{ "var": "s", "contains": "OK" }` | 变量值包含子串 |
| `{ "var": "n", "gt": 5 }` / `"lt"` | 数值比较 |
| `{ "var": "flag", "is_true": true }` / `"is_false"` | 真值判断（`""` `0` `false` `no` `none` `null` 视为假） |
| `{ "exists": { <target> } }` | 控件存在 |
| `{ "not_exists": { <target> } }` | 控件不存在 |
| `{ "all": [c1, c2] }` / `{ "any": [...] }` / `{ "not": c }` | 逻辑组合 |

要点：

- `cond` 里**同样支持 `${var}`**，如 `{ "var": "mode", "equals": "${expect_mode}" }`。
- 判「存在」会给一个短等待（默认 3 秒，可用步骤的 `timeout` 覆盖），因为界面可能正在弹出；
  判「不存在」则**立即采样**，否则每个 `not_exists` 分支都要白等一个完整超时。
- `then` / `else` 为空时该步骤记为 skipped，不算失败。

### repeat —— 循环

两种形态：

```json
{
  "action": "repeat",
  "count": 3,
  "index_var": "i",
  "steps": [
    { "action": "log", "message": "第 ${i} 轮" },
    { "action": "scroll", "target": { "auto_id": "main_tblDeviceList" }, "direction": "down", "count": 1 }
  ]
}
```

```json
{
  "action": "repeat",
  "over": ["DEV_001", "DEV_002", "DEV_003"],
  "item_var": "device",
  "index_var": "idx",
  "steps": [
    { "action": "click", "target": { "name_re": "${device}" } },
    { "action": "assert_text", "target": { "auto_id": "main_lblDeviceName" }, "expect": "${device}" }
  ]
}
```

| 参数 | 说明 |
|---|---|
| `count` | 重复次数；与 `over` 二选一 |
| `over` | 要遍历的数组；也接受 `"a,b,c"` 这种逗号分隔字符串 |
| `item_var` | 元素变量名，默认 `item` |
| `index_var` | 下标变量名，默认 `index` |

要点：

- 子步骤里的 `${item}` / `${index}`（或自定义名）**在每轮开始时才解析**，
  所以能正确拿到当轮的值 —— 包括写在 `target` 里的。
- `repeat` 可以嵌套，内层能读到外层的循环变量。
- 子步骤失败按各自的 `on_fail` 处理，默认会中止整个 repeat。
  想「遍历一批、失败的跳过继续」，给子步骤加 `"on_fail": "continue"`。
- 报告里每轮会带上 `repeat[0]` `repeat[1]` 这样的标签。

> 注意 `count` 这个名字被 `repeat` 与 `scroll` 共用但含义不同：
> 在 `repeat` 步骤上是「循环几次」，在 `scroll` 步骤上是「滚动几格」。
> 两者都是 Step 的已知字段，写在同一层不会冲突（`scroll` 通常作为 `repeat` 的子步骤出现）。

---

## 9. 失败处置与自动留证

### on_fail

| 值 | 行为 |
|---|---|
| `abort` | **默认**。终止当前用例剩余步骤，直接进 `teardown`，用例判失败 |
| `continue` | 记录这一步失败，继续下一步。用例最终仍判失败 |
| `skip_flow` | 跳过剩余步骤直接 `teardown`，**且不计失败**（用于「前置条件不满足就整条跳过」） |

`optional: true` 是更轻量的选择：这一步失败**只记 warning，不影响用例结论**。
适合非关键的可选交互（比如「如果有引导弹窗就关掉」）。

套件级 `continue_on_error`（或 CLI 的 `--continue-on-error`）控制**用例之间**是否继续，
与步骤级的 `on_fail` 是两个层次。

### teardown 的语义

- 无论 `setup` / `steps` 成功失败，`teardown` **必然执行**。
- `teardown` 里的步骤失败**只记警告**，不会把已通过的用例改成失败。
  报告里会打 `TEARDOWN_WARN` 标记 —— 看到这个标记要留意，
  它通常意味着窗口没关干净，可能污染下一条用例（不过框架每条用例都会重启应用并清理残留进程，
  所以实际影响有限）。

### 报告里那几个没写过的步骤

每条用例的结果里会出现 `launch_app` / `wait_main_window`（在 `setup` 最前）与
`close_app`（在 `teardown` 最后），但 JSON 里并没有它们 ——
这是运行器自动注入的**基础设施步骤**：

| 步骤 | 做什么 | 失败意味着 |
|---|---|---|
| `launch_app` | 清理残留进程 → 启动应用 | 用例直接判 ERROR，后面一步也不会跑。先查 `--execute` 路径 |
| `wait_main_window` | 等主窗口出现，并把别名 `main` 注册到上下文 | 窗口识别条件不对。用 `--inspect` 的 `windows.txt` 看真实标题，收窄 `main_window.title_re` |
| `close_app` | 关闭应用 → 扫描并 kill 残留进程 | 不影响用例结论，但会在报告里留下警告 |

它们**不是注册的动作**，在 JSON 里写 `{"action": "launch_app"}` 会报「未知动作」。

`--attach` 模式下：`launch_app` 变成「连接已运行的实例」（不启动、不清残留），
`wait_main_window` 仍然会跑，**`close_app` 被跳过**（应用保留，方便反复调试）。

> 主窗口别名用的是**配置里的识别条件**而不是刚取到的窗口标题：
> app 登录后标题可能变化，写死精确标题会让后续步骤全部定位失败。

用例自己的 `teardown` 应该写「关窗口 / 还原状态」这类业务清理（如 `close_window`），
而不是试图关进程 —— 那是 `close_app` 的职责。

### 自动留证

每次步骤失败落盘三件套：

```
reports/<run_id>/screenshots/<case>_<step>.png    全屏截图
reports/<run_id>/trees/<case>_<step>.txt          控件树快照
reports/<run_id>/run.log                          完整异常栈 + 该步骤解析后的实际参数
```

文件名用的是步骤的 `id`，所以**给步骤写有意义的 `id`** 直接决定了留证好不好找。

留证发生在 `on_fail` 处置**之前**，所以即使 `on_fail: "abort"` 也一定有截图和控件树。

`FOCUS_LOST` 标记：`run.foreground_check` 打开时，框架在操作前会校验被测应用是否在前台。
检测到焦点被抢占会等待重试，仍失败则在报告中标注 `FOCUS_LOST` ——
**这类失败不是被测应用的缺陷**，是运行环境被干扰了。

---

## 10. 调试工作流

### 第一步：永远先 --inspect

```powershell
.venv\Scripts\python.exe auto_test.py --inspect --execute=E:/app/app.exe --wait=30 --keep-open
```

`--wait=30` 期间手工登录并导航到目标界面，`--keep-open` 让应用导出后不关闭。
产物在 `reports/inspect/<时间戳>/`：

| 文件 | 怎么用 |
|---|---|
| `control_tree.txt` | **主力**。每个控件后面都附带一条 `child_window(...)` 配方，可以直接抄成 `target` |
| `automation_ids.txt` | 已有 objectName 的控件清单 + 缺失清单（后者提交给开发） |
| `summary.txt` | 覆盖率与门禁判定 |
| `windows.txt` | 进程内所有顶层窗口标题，用于收窄 `main_window.title_re` |

`control_tree.txt` 里的一行长这样：

```
   - [Pane] '字符网格'    child_window(title="字符网格", auto_id="108", control_type="Pane")
```

对应的 `target`：

```json
{ "window": "main", "auto_id": "108", "type": "Pane", "desc": "字符网格" }
```

### 第二步：只跑一条用例

```powershell
.venv\Scripts\python.exe auto_test.py --execute=... --flow=tc002_main_ui --log-level=DEBUG
.venv\Scripts\python.exe auto_test.py --execute=... --case=TC_MAIN_002
```

### 第三步：复用已开的实例

```powershell
.venv\Scripts\python.exe auto_test.py --attach --flow=tc002_main_ui --log-level=DEBUG
```

`--attach` 不启动也不关闭应用，改一次 JSON 就能立刻重跑，迭代最快。
**调完记得用正常模式（不带 `--attach`）跑一遍**，确认启动 / 清理链路也没问题。
注意 `--attach` 前得自己把应用开好并停在主窗口（或登录页），
否则 `wait_main_window` 会失败。

### 第四步：定位失败怎么办

1. 打开 `reports/<run_id>/trees/<case>_<step>.txt`
2. 搜目标控件的可见文本或类型
3. **找到了** → 把它后面的 `child_window(...)` 配方抄进 `target`，通常是 `auto_id` 写错或该控件根本没有 objectName
4. **没找到** → 三种可能：
   - 控件在别的窗口 / 弹窗里 → 先 `switch_window`
   - 控件还没创建（在未激活的 Tab 页、需要滚动才加载） → 前面加 `wait_exists` 或先切 Tab
   - 控件是自绘的，UIA 看不到内部 → 用 `click_offset` 点父容器
5. 整棵树都是空的或只有一个 Pane → `QT_ACCESSIBILITY` 没生效，见 [spike_report.md](./spike_report.md)

### 第五步：偶发失败怎么排

- 报告里有 `FOCUS_LOST` → 运行环境被干扰，不是缺陷
- 没有 `FOCUS_LOST`，失败步骤每次都不一样 → 大概率是用了 `sleep` 或没等异步加载完，
  改成 `wait_text` / `wait_exists` / `wait_gone`
- 慢机器上偶发超时 → `--timeout-scale=1.5` 全局放宽，**不要逐个改 JSON 里的 timeout**
- 怀疑是控件树太深导致搜索慢 → 加 `parents` 收窄，或设 `run.search_depth`

### --validate 抓得到什么、抓不到什么

`--validate` 不启动应用，跑一次不到一秒，**写完 JSON 先跑它**。它会一次性报出文件里的全部问题（不是改一条报一条），
每条都带字段路径：

```
[ERROR]   ...\tmp_bad.json: Additional properties are not allowed ('unknown_top_key' was unexpected)
[ERROR]   ...\tmp_bad.json -> steps[0].target: Additional properties are not allowed ('automation_id' was unexpected)
[ERROR]   ...\tmp_bad.json -> steps[1].target.index: 0 is less than the minimum of 1
[ERROR]   ...\tmp_bad.json -> steps[0].action: 未注册的动作 'clickk'，是否想写 'click'？
[ERROR]   ...\tmp_bad.json -> steps[4].steps: repeat 动作必须提供 steps
[WARNING] ...\tmp_bad.json -> steps[2].flow: include 引用的子流程找不到: no_such_flow.json
[WARNING] ...\tmp_bad.json -> steps[5].id: 步骤 id 重复: 'b5'
```

| 抓得到 | 抓不到 |
|---|---|
| JSON 语法错误、非 UTF-8 编码 | `auto_id` 是否对应真实控件 |
| 顶层 / 步骤 / `target` 的未知字段（这两个是封闭集） | **动作专属参数拼错**（如 `dx` 写成 `dxx`）—— 这部分故意不做封闭约束，否则新增动作就得同步改 schema |
| 枚举值、类型、`minimum` 之类的取值约束 | 必填的动作参数缺失（如 `assert_text` 没写 `expect`）—— 运行时才报 |
| 未注册 / 拼错的 `action`（带候选建议） | 变量名拼错（`${devie_name}`）—— 运行时报「变量未定义」 |
| `include` 引用的子流程是否存在 | 用例能否真的跑通 |
| `repeat` 缺 `steps`、`if` 缺 `cond`、步骤 `id` 重复 | |
| 未显式声明 `id`、套件引用无法解析、流程没有任何步骤 | |
| 用了 `target.image`（本期未实现） | |

退出码：0 = 全部通过（可能有警告），2 = 有错误。把它接进 pre-commit 或 CI 很合适。

> 校验只扫 `test_flow_file/` 下的文件，且**跳过 `_` 开头的草稿**。也可以指定范围：
> `python auto_test.py --validate tc002_main_ui.json`。

---

## 11. 常见坑

按踩到的概率排序。

| 现象 | 原因 | 解法 |
|---|---|---|
| 所有控件都找不到，控件树只有一个空 Pane | Qt 的 UIA 桥未激活 | 确认 `app.env.QT_ACCESSIBILITY: "1"` 已注入且未被外部覆盖 |
| 点击「成功了」但界面没反应 | 用了 `invoke`，Qt 的 UIA Invoke Pattern 支持不完整 | `by: "mouse"`（已是全局默认） |
| 用例时好时坏，失败步骤不固定 | 用了 `sleep` 等异步加载 | 换 `wait_text` / `wait_exists` / `wait_gone` |
| 文本断言明明看着一样却失败 | 存在不可见字符差异 | 框架已统一行尾并 strip 首尾；仍失败时用 `assert_text_contains` 或 `assert_property` 看原始值 |
| 单步耗时好几秒甚至超时 | 控件树太深触发全树深搜 | 加 `parents` 收窄；设 `run.search_depth`；提高 `timeout_scale` |
| 动作参数写了没生效 | 参数名拼错。schema 对动作专属参数不做封闭约束 | 对照 [§6](#6-动作清单) 核对；`--validate` 抓不到这类错误 |
| `target` 字段写了没生效 | `target` 字段集是封闭的，拼错会被 `--validate` 报错 | 用 `auto_id` 不是 `automation_id`；用 `type` 不是 `control_type` |
| 子流程被跑了两遍 | 既写进了套件的 `flows`，又被 `include` | 从 `flows` 里移除。`--list` 的「可复用子流程」一节列的就是这类文件 |
| `include` 报找不到子流程 | 相对路径基准是**调用方目录**，不是项目根 | 检查文件位置；或写相对 `flow_dir` 的路径 |
| `repeat` 里 `${item}` 报变量未定义 | 不会发生（子步骤延迟绑定）。若真报，检查 `item_var` 拼写 | 默认名是 `item` / `index` |
| 重跑用例状态不一致 | 用了 `toggle` 而不是 `check` / `uncheck` | 换成幂等的 `check` / `uncheck` |
| 输入框里的 `+` `(` `^` 变成了组合键 | `type_keys` 把这些当语法符号 | 加 `literal: true`；或改用 `set_text` |
| `--attach` 能跑、正常模式跑不了 | 主窗口识别条件不对 | 用 `windows.txt` 里的真实标题收窄 `app.main_window.title_re`。**不要依赖 `class_name`**，Qt 窗口类名随版本变化 |
| 控制台刷一堆 `--- Logging error ---` | 控制台编码编不出某些字符 | 框架已把 stdout/stderr 的错误策略放宽为 `replace`；若仍出现，检查是否替换过 `sys.stdout` |

---

## 12. 编写检查清单

提交一条新用例前过一遍：

- [ ] `id` 唯一且符合 `TC_<模块>_<序号>` 形式；`name` / `desc` 用中文写清楚了意图
- [ ] 有 `tags`（至少一个分级标签）和 `priority`
- [ ] 有用例级 `timeout`（默认 15s/步，整条用例没有兜底超时容易挂死）
- [ ] 每个步骤都有 `id` 和 `desc` —— 决定报告与留证文件的可读性
- [ ] 定位优先用 `auto_id`；用文本定位时改用 `name_re`
- [ ] 深层控件加了 `parents` 收窄搜索范围
- [ ] 没有裸 `sleep`；等待异步用 `wait_*`
- [ ] 点击没有显式写 `by: "invoke"`
- [ ] 勾选类用 `check` / `uncheck` 而不是 `toggle`
- [ ] `teardown` 里把打开的弹窗 / 改过的状态还原（即使框架会重启应用，也便于 `--attach` 调试）
- [ ] 密码等敏感值走 `${env:NAME}`，没有硬编码
- [ ] 首尾各放一个 `assert_process_alive` 作为崩溃看门狗
- [ ] `--validate` 通过，0 错误 0 警告
- [ ] 连续跑 3 次都通过（`--repeat=3`），没有偶发失败
- [ ] 如果依赖尚未补充的 objectName，已在 [objectname_requirements.md](./objectname_requirements.md) 登记

---

## 附：新增一个动作

框架的扩展点是「零侵入」的 —— 在 `app_at/actions.py` 加一个函数即可，
不需要改注册表、schema 或其他任何文件：

```python
@action("my_action", needs_target=True, category="control",
        required_params=("foo",), description="一句话说明，会出现在 --list-actions 里")
def _my_action(ctx: RunContext, step: Step, element: Element | None) -> ActionResult:
    el = _require(step, element, "target")
    foo = str(_param(step, "foo"))          # 必填参数，缺失时抛带写法的 ActionError
    bar = _opt(step, "bar", "默认值")        # 可选参数
    el.some_operation(foo, bar)
    return ActionResult(message="写进报告的一句话")
```

注意事项：

- 签名固定为 `fn(ctx, step, element) -> ActionResult`。
- **动作参数如果与 Step 的具名字段同名**（`timeout` / `count` / `flow` / `cond` / `value` /
  `expect` / `vars` / `steps` / `then` / `else` / `over` / `item_var` / `index_var`），
  它会被 `Step.parse` 抽成具名字段而**不进 `params`** —— 此时 `_opt` / `_param` 读不到，
  必须两处都看。`actions.py` 里 `_scroll` 的 `count`、`_include` 的 `flow` 就是这种情况，
  照着写即可。
- 抛框架异常（`ActionError` / `AssertFailed` / `ElementNotFoundError`）而不是裸 `Exception`，
  这样失败信息才会带上 `detail` 与 `expected` / `actual`，报告里才有可读的差异。
- 断言类动作请抛 `AssertFailed`，它属于不可重试异常。
- 加完跑一次 `--list-actions` 确认注册成功。
