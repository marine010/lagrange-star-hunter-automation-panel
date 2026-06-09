拉格朗日自动化面板 - 星际猎人配置版

推荐环境:
- Windows 10/11
- Python 3.12 或更新版本
- 游戏窗口分辨率/布局按 star_hunter_1920.json 的 1920x1080 配置使用

源码包使用方法:
1. 解压整个文件夹。
2. 第一次运行双击 INSTALL_AND_RUN.bat，自动安装依赖并打开 GUI。
3. 之后可以双击 RUN_GUI.bat 直接打开。
4. 在 GUI 中选择游戏窗口，开始连续识别/执行。

可执行版使用方法:
1. 解压整个文件夹。
2. 双击 RUN_EXE.bat 或 dist\LagBotGUI\LagBotGUI.exe。
3. configs 和 templates 必须和 exe 文件夹一起保留，不能只单独发送 exe。

注意:
- 这个包会控制鼠标点击，请只在你授权和确认的测试环境中使用。
- 当前版本包含技能目标刷新 fallback 修复: 刷新确认 066 失败时，如果配置/决策已有兜底目标点，会继续释放技能并在日志里记录 target_confirmation_unverified。
- 日志会写入 logs\gui_sessions，复盘问题时优先看最新 session。
