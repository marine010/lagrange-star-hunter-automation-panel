# Lagrange Star Hunter Automation Panel

Windows GUI automation helper for a Lagrange private/test environment. It captures a selected game window, reads cards, cost, timer, skill status, and battlefield labels from image templates, then chooses and optionally executes mouse actions from a configurable policy.

This repository contains the Star Hunter 1920x1080 profile and cropped image templates used by the current GUI workflow.

## Safety Notice

- Use only in environments where you are authorized to automate input.
- The GUI can move and click the mouse when live execution is enabled.
- Test detection first, then enable live actions only after the preview and logs look correct.
- Runtime logs are written under `logs/gui_sessions/` and are intentionally ignored by Git.

## Current Profile

- Config: `configs/star_hunter_1920.json`
- Capture backend: Windows Graphics Capture (`wgc`)
- Target resolution/layout: 1920x1080
- Includes the skill-target refresh fallback fix: when live target refresh cannot confirm enough CAS066 labels but the selected action already has a fallback target point, the GUI still casts the skill and logs `target_confirmation_unverified`.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run

```powershell
python -m lagrange_bot.gui --config configs\star_hunter_1920.json
```

Or double-click:

```text
INSTALL_AND_RUN.bat
```

After dependencies are installed, `RUN_GUI.bat` starts the GUI directly.

## Test

```powershell
python -m compileall lagrange_bot tests
python -m unittest discover -s tests
```

Some vision tests use optional live sample images and skip themselves when those private samples are not present.

## Repository Hygiene

This public repository excludes:

- GUI session logs
- Screenshots and training samples
- Packaged release zips and PyInstaller build output
- Local editor settings and Python caches

Keep `configs/`, `templates/`, and `lagrange_bot/` together when sharing or deploying.
