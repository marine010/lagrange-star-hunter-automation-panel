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
- 当前版本包含技能目标刷新 fallback 修复：
  当实时释放技能前无法确认足够数量的 CAS066 标签，但本次动作已经有配置或决策给出的兜底目标点时，程序会继续释放技能，并在日志中记录 `target_confirmation_unverified`。

## 安装

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

启动后，在 GUI 中选择游戏窗口，再进行识别、预览和实时执行。

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
- `RUN_GUI.bat` / `INSTALL_AND_RUN.bat`：Windows 启动脚本

公开仓库不包含：

- GUI session 日志
- 原始截图和训练样本
- 打包产物、发布 zip、PyInstaller 构建目录
- 本地编辑器配置、Python 缓存和历史快照

分享或部署时，请保持 `lagrange_bot/`、`configs/` 和 `templates/` 目录在同一个项目根目录下。
