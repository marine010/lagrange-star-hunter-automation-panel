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

## 推荐安装方式

普通用户推荐下载安装器，不需要自己安装 Python，也不需要运行 pip。

1. 打开 GitHub 页面右侧的 `Releases`。
2. 下载最新版本里的 `LagrangeStarHunterSetup-版本号.exe`。
3. 双击安装器。
4. 按提示一路“下一步”安装。
5. 安装完成后，从桌面快捷方式或开始菜单启动“拉格朗日星际猎人自动化面板”。

这种方式最接近普通软件安装流程。安装器会把运行所需的 Python 环境、依赖、配置和模板一起打包进去。

如果没有看到 Release 安装器，或者你想从源码运行，再看下面的“源码版安装方式”。

## 源码版安装方式

源码版适合开发者或需要自己改配置的人使用。电脑上必须先安装 Python，然后再用项目里的 `.bat` 脚本启动 GUI。

项目根目录里几个常用文件的作用：

- `INSTALL_AND_RUN.bat`：第一次使用时双击它。它会安装依赖，然后尝试打开主 GUI。
- `RUN_GUI.bat`：依赖装好后，平时双击它打开主 GUI。
- `RUN_DATA_GUI.bat`：数据采集界面，普通使用不用点它。
- `requirements.txt`：Python 依赖列表，不需要手动打开。
- `configs/`、`templates/`、`lagrange_bot/`：程序运行需要的文件夹，不要移动到别的地方。

## 源码版第一次安装和打开

### 1. 安装 Python

先安装 Python 3.12 或更新版本。安装时一定要勾选：

```text
Add python.exe to PATH
```

安装好以后，重新打开一个 PowerShell，输入：

```powershell
python --version
```

如果能看到类似 `Python 3.12.x` 的版本号，就说明 Python 可以用了。

### 2. 下载并解压项目

在 GitHub 页面点击 `Code` -> `Download ZIP` 下载源码包。

下载后请注意：

1. 右键 zip 文件，选择“全部解压”。
2. 进入解压出来的文件夹，例如 `lagrange-star-hunter-automation-panel-main`。
3. 确认能看到 `INSTALL_AND_RUN.bat`、`RUN_GUI.bat`、`configs`、`lagrange_bot`、`templates` 这些文件和文件夹。
4. 不要在压缩包预览窗口里直接双击 `.bat`，必须先完整解压。

### 3. 第一次启动

第一次使用时，双击：

```text
INSTALL_AND_RUN.bat
```

它会做三件事：

1. 检查电脑上是否能找到 Python。
2. 自动安装 `requirements.txt` 里的依赖。
3. 安装完成后打开主 GUI。

第一次安装依赖可能比较慢，黑色命令行窗口里会刷很多英文日志，这是正常的。正常启动成功后，会弹出标题为“拉格朗日自动识别”的图形化窗口。

## 源码版以后怎么打开 GUI

以后依赖已经装好时，直接双击：

```text
RUN_GUI.bat
```

正常情况会出现“拉格朗日自动识别”窗口。如果只出现黑色命令行窗口，没有出现图形化窗口，请看下面“常见问题”里的黑窗口排查。

也可以用命令行启动。打开项目文件夹，在空白处按住 `Shift` 后右键，选择“在此处打开 PowerShell”，然后运行：

```powershell
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
```

这个方式适合排查问题，因为报错会直接显示在 PowerShell 里。

## 打开 GUI 后怎么用

启动成功后，主窗口标题是“拉格朗日自动识别”。

基本流程：

1. 先打开游戏，并让游戏窗口保持可见。
2. 回到 GUI 顶部的窗口下拉框，选择游戏窗口。
3. 在“战前卡组”区域勾选本局使用的手牌；不确定时可以先点“全选”。
4. 点击右上角“开始识别”。
5. 开始后按钮会变成“停止识别”，界面会显示时间、费用、手牌、技能和最近动作。
6. 想停止时，再点一次“停止识别”。

注意事项：

- 开始识别后，“战前卡组”区域会自动收起，这是正常现象。
- 当前配置按 1920x1080 布局调试，游戏窗口大小和布局差太多时，识别可能不准。
- 程序默认会启用自动放置手牌和技能释放逻辑。使用前请确认只在授权测试环境中运行。
- 每次运行的日志会写到 `logs/gui_sessions/`，排查问题时优先看最新的 session 文件夹。

## 手动安装方式

如果不想用 `INSTALL_AND_RUN.bat`，也可以手动安装依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
```

如果你熟悉 Python 虚拟环境，也可以自己创建 `.venv` 后再安装：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
```

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

### 双击后窗口一闪而过

通常也是启动时报错了，只是窗口关闭太快看不到。请不要直接双击排查，改用 PowerShell。

最简单的方法：在项目文件夹空白处按住 `Shift` 后右键，选择“在此处打开 PowerShell”，再运行：

```powershell
.\RUN_GUI.bat
```

### 提示 Python was not found

说明系统没有找到 Python。请安装 Python 3.12 或更新版本，并在安装时勾选 `Add python.exe to PATH`。安装完成后重新打开 PowerShell，运行：

```powershell
python --version
```

能看到版本号后，再运行 `INSTALL_AND_RUN.bat`。

### 提示 No module named xxx

说明依赖没有装完整。请在项目文件夹里打开 PowerShell，重新安装依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

安装完成后再运行：

```powershell
.\RUN_GUI.bat
```

### pip 安装很慢或失败

可以换国内镜像安装：

```powershell
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

装完后再运行 `RUN_GUI.bat`。

### GUI 打开了，但是窗口下拉框里没有游戏

先确认游戏已经打开，并且不是最小化状态。然后点击窗口下拉框，它会刷新可见窗口列表。仍然找不到时，重启 GUI 和游戏再试。

### GUI 打开了，但是识别不准

当前公开配置主要按 `configs/star_hunter_1920.json` 的 1920x1080 布局调试。请尽量让游戏窗口使用相同布局，并先只观察识别结果，不要急着让它自动点击。日志和截图会保存在 `logs/gui_sessions/`，可以把最新 session 发给维护者排查。

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

## 打包和发布

维护者可以用下面的命令在本地生成便携版 exe 文件夹：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_windows_app.ps1
```

生成结果在：

```text
dist\LagrangeStarHunter\LagrangeStarHunter.exe
```

GitHub Actions 会在推送 `v*` 标签时自动构建 Windows 安装器，并把 `LagrangeStarHunterSetup-版本号.exe` 上传到 GitHub Release。发版示例：

```powershell
git tag v0.1.0
git push origin v0.1.0
```

## 仓库内容

公开仓库包含：

- `lagrange_bot/`：核心识别、决策、截图和 GUI 代码
- `configs/`：示例配置和星际猎人 1920x1080 配置
- `templates/`：裁剪后的识别模板
- `packaging/`：PyInstaller 和 Inno Setup 打包配置
- `tools/`：本地构建脚本
- `.github/workflows/`：自动构建 Windows 安装器的 GitHub Actions
- `tests/`：单元测试
- `RUN_GUI.bat` / `RUN_DATA_GUI.bat` / `INSTALL_AND_RUN.bat`：Windows 启动脚本

公开仓库不包含：

- GUI session 日志
- 原始截图和训练样本
- 打包产物、发布 zip、PyInstaller 构建目录
- 本地编辑器配置、Python 缓存和历史快照

分享或部署时，请保持 `lagrange_bot/`、`configs/` 和 `templates/` 目录在同一个项目根目录下。
