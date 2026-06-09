from __future__ import annotations

import time

from .models import Action, ActionType


class ActionExecutor:
    def __init__(self, dry_run: bool = True, click_delay_seconds: float = 0.08):
        self.dry_run = dry_run
        self.click_delay_seconds = click_delay_seconds
        self._pyautogui = None

    def execute(self, action: Action) -> None:
        if action.type == ActionType.WAIT:
            print(f"WAIT {action.wait_seconds:.2f}s: {action.reason}")
            time.sleep(max(0.0, action.wait_seconds))
            return

        if action.click is None:
            print(f"SKIP: {action.reason} has no click point")
            return

        if self.dry_run:
            print(
                f"DRY-RUN {action.type.value}: "
                f"pre_clicks={list(action.pre_clicks)} click={action.click} "
                f"target_click={action.target_click} reason={action.reason}"
            )
            return

        pyautogui = self._load_pyautogui()
        for click in action.pre_clicks:
            pyautogui.click(click[0], click[1])
            time.sleep(self.click_delay_seconds)

        pyautogui.click(action.click[0], action.click[1])
        time.sleep(self.click_delay_seconds)

        if action.target_click:
            pyautogui.click(action.target_click[0], action.target_click[1])
            time.sleep(self.click_delay_seconds)

        print(f"DONE {action.type.value}: {action.reason}")

    def _load_pyautogui(self):
        if self._pyautogui is None:
            try:
                import pyautogui
            except ImportError as exc:
                raise RuntimeError("pyautogui is required for --live mode") from exc
            pyautogui.FAILSAFE = True
            self._pyautogui = pyautogui
        return self._pyautogui
