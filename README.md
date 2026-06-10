# 拉格朗日星际猎人自动化面板

这是一个 Windows 图形界面自动化工具，用于拉格朗日私服/测试环境的星际猎人 1920x1080 配置。它会捕获指定游戏窗口，通过图片模板识别手牌、费用、计时器、技能状态和战场目标标签，再按配置策略选择动作，并在开启实时执行后控制鼠标点击。

## 安全说明

- 只在你有授权的测试环境中使用。
- 开启实时执行后，程序会移动并点击鼠标。
- 建议先只做识别预览，确认识别框、目标点和日志都正确后，再开启实时点击。
- 运行日志会写入 `logs/gui_sessions/`，该目录已被 Git 忽略，不会进入公开仓库。

## 当前配置

- 主配置文件：`configs/star_hunter_1920.json`
- 截图后端：Windows Graphics Capture，配置值为 `wgc`
- 目标分辨率/布局：1920x1080
- 当前配置包含 18 张手牌标题模板。
- 其中 10 张手牌已经配置自动出牌策略：
  `FG300装甲型`、`卡利莱恩级`、`CAS066综合型`、`阋神星级`、`雨海突击型`、`雷里亚特级`、`苔原支援型`、`刺水母级`、`云海级`、`M470攻城型`。
- 另外 8 张手牌目前只做识别模板，默认不会自动打出：
  `AC72载机型`、`AC72通用型`、`AC72离子炮型`、`CV3000级`、`新君士坦丁大帝级`、`艾奥级`、`太阳鲸级`、`奇美拉弹炮型`。
- 当前配置包含 4 个技能条目：
  `伤害提升`、`掩护承伤`、`多目标射击`、`防御情报同步`。其中前三个已配置自动释放策略，`防御情报同步` 当前为未启用/被动条目。
- 客户端 GUI 支持在开始正式识别前手动选择本局卡组；开始后只会用选中卡组的手牌模板进行匹配，以降低运算压力和识别延迟。
- 其他手牌或技能需要先补充采集样本、识别模板和配置策略；未配置前不会可靠识别或自动执行。
- 当前版本包含技能目标刷新 fallback 修复：
  当实时释放技能前无法确认足够数量的 CAS066 标签，但本次动作已经有配置或决策给出的兜底目标点时，程序会继续释放技能，并在日志中记录 `target_confirmation_unverified`。

## 安装

### 新手最快启动

1. 确认电脑已经安装 Python 3.12 或更新版本。
2. 下载 GitHub 源码后，先把 zip 完整解压出来，不要在压缩包预览窗口里直接运行。
3. 第一次运行双击 `INSTALL_AND_RUN.bat`，它会自动安装依赖并尝试打开主 GUI。
4. 依赖装好以后，后续直接双击 `RUN_GUI.bat` 打开主 GUI。

正常启动时会出现图形化窗口。如果只看到黑色命令行窗口，说明 GUI 没有成功启动或已经退出，请看下面的“常见问题”。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

也可以第一次直接双击：

```text
INSTALL_AND_RUN.bat
```

## 启动

```powershell
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
```

依赖安装完成后，也可以双击：

```text
RUN_GUI.bat
```

启动后，在 GUI 中选择游戏窗口，并在“战前卡组”区域勾选本局使用的手牌，再开始识别。开始识别后卡组选择区会自动收起，界面只保留战斗状态信息。

## 常见问题

### 只出现黑色命令行窗口，并显示“请按任意键继续...”

这不是 GUI。这个提示来自 `.bat` 文件最后的 `pause`，表示前面的启动命令已经结束。正常情况下应当弹出图形化窗口；如果只剩这个黑窗口，通常是 Python 没装好、依赖没装完整，或者 GUI 启动时报错后退出了。

处理方法：

1. 先不要按任意键关闭窗口，向上查看黑窗口里是否有报错信息。
2. 如果第一次运行，请先双击 `INSTALL_AND_RUN.bat`，等待依赖安装完成。
3. 如果仍然失败，在项目文件夹空白处按住 `Shift` 后右键，选择“在此处打开 PowerShell”，然后运行：

```powershell
.\RUN_GUI.bat
```

4. 如果想看到更完整的报错，也可以直接运行：

```powershell
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
```

5. 把 PowerShell 中从报错开始到最后一行的内容截图或复制出来，方便定位问题。

### 提示 Python was not found

说明系统没有找到 Python。请安装 Python 3.12 或更新版本，并在安装时勾选 `Add python.exe to PATH`。安装完成后重新打开 PowerShell，运行：

```powershell
python --version
```

能看到版本号后，再运行 `INSTALL_AND_RUN.bat`。

## 数据采集界面

如果要继续补充手牌、技能或 066 战斗目标样本，可以启动专用的数据采集 GUI：

```powershell
python -m lagrange_bot.data_gui --config configs\star_hunter_1920.json
```

也可以双击：

```text
RUN_DATA_GUI.bat
```

采集结果默认写入 `training_samples/`，该目录已被 Git 忽略，不会进入公开仓库。

## 测试

```powershell
python -m compileall lagrange_bot tests
python -m unittest discover -s tests
```

部分视觉测试会引用本地私有采样图片；公开仓库不包含这些采样图，缺失时测试会自动跳过。

## 仓库内容

公开仓库包含：

- `lagrange_bot/`：核心识别、决策、截图和 GUI 代码
- `configs/`：示例配置和星际猎人 1920x1080 配置
- `templates/`：裁剪后的识别模板
- `tests/`：单元测试
- `RUN_GUI.bat` / `RUN_DATA_GUI.bat` / `INSTALL_AND_RUN.bat`：Windows 启动脚本

公开仓库不包含：

- GUI session 日志
- 原始截图和训练样本
- 打包产物、发布 zip、PyInstaller 构建目录
- 本地编辑器配置、Python 缓存和历史快照

分享或部署时，请保持 `lagrange_bot/`、`configs/` 和 `templates/` 目录在同一个项目根目录下。
