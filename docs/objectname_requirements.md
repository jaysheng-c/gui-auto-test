# app objectName 补充需求清单

> **面向对象：app 开发人员**
> **性质：测试契约。** objectName 一旦用于自动化用例，改动需同步通知测试侧。

---

## 1. 为什么需要 objectName

自动化测试通过 Microsoft UI Automation（UIA）识别控件。Qt 会把控件的 `objectName` 暴露为 UIA 的
`AutomationId` 属性 —— 这是**唯一稳定的定位主键**：

| 定位方式 | 稳定性 | 问题 |
|---|---|---|
| **`objectName` → `AutomationId`** | 高 | 需要开发显式设置 |
| 可见文本（`Name`） | 低 | 改文案、改多语言、改快捷键标注（如 `复制(&C)`）就全部失效 |
| 窗口类名（`class_name`） | 不可用 | Qt 窗口类名形如 `Qt643QWindowIcon`，**随 Qt 小版本变化** |
| 控件序号（`found_index`） | 低 | 界面增删一个控件，后面全部错位 |
| 屏幕坐标 | 不可用 | 分辨率、DPI、窗口位置一变就失效 |

没有 objectName，测试侧只能退化成「按文本 + 序号 + 祖先链」猜控件，用例既难写又极易随机失败，
维护成本会随界面迭代线性上升。

**一句话：补 objectName 是一次性成本，不补是持续成本。**

---

## 2. 命名规范

```
格式： <模块>_<类型缩写>_<语义>
示例： login_btnSubmit / login_leUserName / main_tblDeviceList / main_menuFile
```

### 约束

1. **全局唯一，不复用。** 不同界面上功能相同的两个按钮，也要用各自的模块前缀区分。
2. **不含易变文本。** 不要把计数器、时间戳、当前选中项之类会变的值写进名字。
3. **所有可交互控件 + 所有断言目标控件必须设置。** 只读的状态文本（如「设备名称」标签的值）也算断言目标。
4. **动态列表项用稳定业务 ID**，不要用下标：
   ```cpp
   item->setObjectName(QStringLiteral("devItem_%1").arg(device.id()));
   ```
5. **纯 ASCII。** 用英文语义命名，避免中文/特殊字符在 UIA 与日志编码之间来回转换时出问题。
6. **一旦发布不改名。** 确实要改，走 §6 的变更流程。

### 类型缩写表

| 缩写 | Qt 控件 | 缩写 | Qt 控件 |
|---|---|---|---|
| `btn` | QPushButton / QToolButton | `lbl` | QLabel |
| `le` | QLineEdit | `tbl` | QTableView / QTableWidget |
| `te` | QTextEdit / QPlainTextEdit | `tree` | QTreeView / QTreeWidget |
| `cb` | QComboBox | `lst` | QListView / QListWidget |
| `chk` | QCheckBox | `tab` | QTabWidget / QTabBar |
| `rb` | QRadioButton | `menu` | QMenu / QMenuBar |
| `sb` | QSpinBox / QDoubleSpinBox | `act` | QAction |
| `sld` | QSlider | `bar` | QToolBar / QStatusBar |
| `pnl` | QWidget / QFrame（容器） | `dlg` | QDialog |
| `grp` | QGroupBox | `prog` | QProgressBar |
| `chart` | 自绘图表 / QGraphicsView | `splitter` | QSplitter |

### 模块前缀

按 app 的实际界面划分，建议：`login` / `main` / `device` / `alarm` / `report` / `config` / `about` …
前缀取界面而非功能域，这样「这个控件在哪个界面上」一眼可辨。

---

## 3. 覆盖范围（优先级）

按下面的顺序推进，**P0 补齐即可让核心冒烟用例跑起来**：

| 优先级 | 范围 |
|---|---|
| **P0** | 登录界面全部输入框与按钮；主窗口的菜单栏、工具栏；主界面的核心表格 / 列表 / 详情面板 |
| **P1** | 各业务弹窗（新增 / 编辑 / 删除确认 / 导出）的输入项与确定取消按钮；筛选条件区；分页控件 |
| **P2** | 设置界面的全部配置项；只读状态标签（版本号、连接状态、当前用户） |
| **P3** | 自绘区域的**父容器**（图表、画布）—— 内部结构 UIA 看不到，但父容器必须有 objectName，测试侧才能按它的矩形做相对坐标点击 |

> P3 特别说明：自绘控件（如 QCustomPlot、QGraphicsView 画布）在 UIA 树里通常只表现为一个不透明 Pane。
> 测试侧的兜底手段是 `click_offset`：拿到父容器的 `rectangle`，按相对左上角的偏移点击。
> **父容器没有 objectName 时，这个兜底也用不了**，那块区域就成了自动化盲区。

---

## 4. 代码写法

```cpp
// 1) 直接在构造处设置
ui->btnSubmit->setObjectName(QStringLiteral("login_btnSubmit"));

// 2) 在 Qt Designer 里填 objectName 属性（推荐，改动最小、review 最直观）

// 3) 动态创建的控件
auto *btn = new QPushButton(tr("删除"), this);
btn->setObjectName(QStringLiteral("device_btnDelete_%1").arg(id));

// 4) 动态列表项
for (const auto &dev : devices) {
    auto *item = new QListWidgetItem(dev.name());
    item->setData(Qt::UserRole, dev.id());
    item->setText(dev.name());
    list->addItem(item);
    // QListWidgetItem 不是 QObject，没有 objectName ——
    // 这种情况请改用自定义 itemWidget，或在测试侧按文本 + 业务 ID 定位
}

// 5) 表格单元格：QTableWidgetItem 同样不是 QObject。
//    若单元格需要被自动化定位，请用 setCellWidget() 放一个带 objectName 的真实控件
```

**注意事项**

- `objectName` 要在控件**显示之前**设置。运行中改名会让已缓存的定位条件失效。
- 不要为了测试去改控件的可见文本或结构 —— 只加 `objectName`，对界面零影响。
- `QAction` 加到工具栏后，工具栏上生成的是 `QToolButton`；给 QAction 设 objectName 通常能传递下去，
  但请在设置后用 `--inspect` 确认一次（见 §5）。

---

## 5. 如何自查覆盖情况

补完一批之后，测试侧（或开发自己）跑一次探测：

```powershell
.venv\Scripts\python.exe auto_test.py --inspect --execute=E:/app/app.exe --wait=30 --keep-open
```

产物在 `reports/inspect/<时间戳>/`，其中两个文件直接对应本文档：

### `summary.txt` —— 覆盖率与门禁判定

```
控件总数:                26
带 AutomationId:         19
可交互控件数:            23
可交互且带 AutomationId:  17
可交互控件覆盖率:        73.9%

# 门禁判定
  >= 70%  通过：可按 auto_id 为主键编写用例
  30~70%  部分通过：核心控件可用，自绘区域需 click_offset 兜底
  <  30%  不通过：先排查 QT_ACCESSIBILITY，仍无改善则需评估图像方案
```

（上面是一份真实探测输出的样例，被测对象是 Windows 字符映射表，用于说明格式。）

### `automation_ids.txt` —— 已覆盖 / 未覆盖清单

文件分两段，**第二段就是待补清单**，可直接拷进下面的 §6 表格：

```
# 已设置 AutomationId（Qt objectName）的控件清单
# 格式: <control_type>  <automation_id>  |  name  |  rect
## Button（6 个）
  102          | 复制(C)   | (L410, T405, R485, B428)
  ...

# 缺少 AutomationId 的可交互控件（建议推动开发补充 objectName）
## Button（3 个缺 auto_id）
  name='最小化'  class=  (L359, T66, R406, B96)
  ...
```

每换一个界面就跑一次 `--inspect`（`--wait` 期间手工导航到目标界面），逐界面收集。

### `control_tree.txt` —— 完整控件树

树里每个控件后面都附带一条可直接抄用的 `child_window(...)` 配方，用于确认某个 objectName 是否真的被 UIA 看到了。

---

## 6. 待补清单

> 拿到真实 `app.exe` 的 `--inspect` 产物后填写。每行一个控件，建议按界面分节。

### 登录界面

| 控件（可见文本 / 用途） | UIA 类型 | 建议 objectName | 优先级 | 状态 |
|---|---|---|---|---|
| 用户名输入框 | Edit | `login_leUserName` | P0 | ⬜ |
| 密码输入框 | Edit | `login_lePassword` | P0 | ⬜ |
| 登录按钮 | Button | `login_btnSubmit` | P0 | ⬜ |
| 登录失败提示 | Text | `login_lblError` | P0 | ⬜ |
| 记住密码 | CheckBox | `login_chkRemember` | P2 | ⬜ |

### 主界面

| 控件（可见文本 / 用途） | UIA 类型 | 建议 objectName | 优先级 | 状态 |
|---|---|---|---|---|
| 文件菜单 | MenuItem | `main_menuFile` | P0 | ⬜ |
| 设备列表表格 | Table | `main_tblDeviceList` | P0 | ⬜ |
| 左侧面板（容器） | Pane | `main_pnlLeft` | P0 | ⬜ |
| 设备名称标签 | Text | `main_lblDeviceName` | P0 | ⬜ |
| 刷新按钮 | Button | `main_btnRefresh` | P1 | ⬜ |
| 导出按钮 | Button | `main_btnExport` | P1 | ⬜ |
| 状态栏 | StatusBar | `main_barStatus` | P2 | ⬜ |
| 版本号标签 | Text | `main_lblVersion` | P2 | ⬜ |
| 图表区域（自绘，父容器） | Pane | `main_chartOverview` | P3 | ⬜ |

> 上表中的 objectName 是**建议值**，也是仓库里 `test_flow_file/*.json` 当前使用的占位值。
> 开发侧若采用不同命名，请回填本表并同步修改 JSON —— 两边必须一致，否则用例会全部定位失败。

### 其他界面

（按 §5 的方法逐界面补充）

---

## 7. 变更流程

objectName 属于**测试契约**，请遵守：

1. **新增**：随功能提交即可，无需通知（测试侧会在下一轮 `--inspect` 中发现）。
2. **改名 / 删除**：**必须提前通知测试侧**，并在提交说明里写明旧名 → 新名。
   测试侧会同步修改 `test_flow_file/*.json`。未通知的改名会导致用例批量失败，
   且失败现象是「控件找不到」，排查成本远高于一次沟通。
3. **重构界面结构**（控件换父容器）：即使 objectName 不变也要通知 ——
   JSON 里的 `parents` 祖先链会失效。
4. 建议把「是否影响自动化定位」作为 UI 相关 MR 的一项 checklist。

---

## 8. 常见问题

**Q：设了 objectName，`--inspect` 里还是看不到 AutomationId？**
A：依次排查 ——
1. `QT_ACCESSIBILITY` 环境变量是否为 `1`（框架已在 `config/default.yaml` 的 `app.env` 中注入，
   但要确认没有被外部设为 `0` 覆盖）；
2. objectName 是否在控件显示**之后**才设置；
3. 控件是否被 `setVisible(false)` 或在未加载的 Tab 页里 —— UIA 看不到未创建的控件；
4. 是不是 `QTableWidgetItem` / `QListWidgetItem` 这类**非 QObject** 的项，它们没有 objectName（见 §4 第 5 条）。

**Q：objectName 会影响性能或界面显示吗？**
A：不会。它只是一个字符串属性，不参与绘制与布局。

**Q：可以用中文 objectName 吗？**
A：技术上可以，但请避免。跨 GBK/UTF-8 编码环境（控制台、日志、CI）时容易出问题，且不利于检索。

**Q：一定要全补吗？工作量太大。**
A：不用。按 §3 的优先级来，**P0 补齐就能让核心冒烟用例跑起来**。
未覆盖的控件测试侧会用文本 + 祖先链降级定位，但那部分用例的维护成本会明显更高、更容易随机失败。
