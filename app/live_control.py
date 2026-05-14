"""Live-model JSON control contract and IR execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .serial_ir import SamsungSerialIrService

LIVE_TV_CONTROL_SYSTEM_INSTRUCTION = """
You are a live TV-control agent.

The user may ask you to inspect the TV view and decide which IR remote actions are needed.
Always respond with exactly one JSON object and no markdown.

JSON shape:
{
  "mode": "tv_control",
  "summary": "short summary",
  "done": true_or_false,
  "task_status": "IN_PROGRESS|COMPLETED|BLOCKED|NEEDS_CONFIRMATION",
  "requires_confirmation": true_or_false,
  "visual_state": "what you see on screen",
  "commands": [
    {
      "action": "PRESS_HOME|PRESS_BACK|PRESS_MENU|PRESS_INPUT|PRESS_POWER|PRESS_MUTE|PRESS_UP|PRESS_DOWN|PRESS_LEFT|PRESS_RIGHT|PRESS_OK|PRESS_VOLUME_UP|PRESS_VOLUME_DOWN|PRESS_CHANNEL_UP|PRESS_CHANNEL_DOWN|CUSTOM",
      "key": "optional explicit samsung key",
      "repeats": 1,
      "reason": "why this key is needed"
    }
  ],
  "next_prompt": "optional next question for the user"
}

Rules:
- Return valid JSON only.
- Use commands only when the scene supports the action.
- This is a looped controller. Emit the next best remote step based on the current screen state and previous progress.
- Behave like an autonomous agent. Keep making progress until the user task is visibly complete.
- After each IR action, re-check the visible UI before deciding the next action.
- If the UI did not change after an action, do not mindlessly repeat the same key forever. Try a recovery action or mark the task blocked.
- If the TV looks off or the screen is black with no UI, prefer PRESS_POWER first.
- If a screensaver, idle screen, or app overlay blocks progress, prefer PRESS_BACK or PRESS_HOME to recover.
- Keep moving toward the user's task until it is completed.
- Set done=true only when the requested task is clearly completed on screen.
- If the request is ambiguous or risky, set requires_confirmation=true.
- If a direct setting like brightness 45 percent cannot be achieved in one safe step, emit only the next navigation-safe keys and explain in summary.
- Prefer PRESS_* actions. Use CUSTOM only when a specific Samsung key name is required.
- Keep responses compact.
""".strip()


@dataclass
class LiveControlExecutor:
    config: AppConfig

    def __post_init__(self) -> None:
        self.ir_service = SamsungSerialIrService(
            dataset_path=self.config.ir_dataset_path,
            serial_port=self.config.ir_serial_port or "",
            baudrate=self.config.ir_serial_baudrate,
            sender_channel=self.config.ir_sender_channel,
        )

    def _extract_json_text(self, text: str) -> str | None:
        raw = str(text or "").strip()
        if not raw:
            return None
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        return raw[start : end + 1]

    def parse_response(self, text: str) -> dict[str, Any] | None:
        json_text = self._extract_json_text(text)
        if not json_text:
            return None
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def build_initial_task_prompt(self, user_text: str) -> str:
        return (
            "User task: "
            f"{user_text.strip()}\n"
            "Inspect the current TV state from the live video and return the next JSON control step."
            "\nStay in agent mode. Do not ask the user to press Enter again. Keep working until the task is completed on screen."
        )

    def build_followup_prompt(
        self,
        original_task: str,
        parsed: dict[str, Any],
        execution: dict[str, Any] | None,
        *,
        step_index: int,
        max_steps: int,
        last_error: str | None = None,
    ) -> str:
        summary = str(parsed.get("summary") or "").strip()
        visual_state = str(parsed.get("visual_state") or "").strip()
        task_status = str(parsed.get("task_status") or "IN_PROGRESS").strip()
        executed_keys: list[str] = []
        if execution and isinstance(execution.get("command_results"), list):
            for item in execution["command_results"]:
                keys = item.get("keys")
                if isinstance(keys, list):
                    executed_keys.extend(str(key) for key in keys)
        next_prompt = str(parsed.get("next_prompt") or "").strip()
        lines = [
            f"Continue the same user task: {original_task}",
            f"Agent step: {step_index} of {max_steps}",
            f"Last model summary: {summary}",
            f"Last visual state: {visual_state}",
            f"Task status: {task_status}",
        ]
        if executed_keys:
            lines.append("Executed IR keys: " + ", ".join(executed_keys))
        if last_error:
            lines.append("Last execution or parsing issue: " + last_error)
        lines.append("Observe the updated live video and verify whether the last IR action changed the UI before choosing the next step.")
        if next_prompt:
            lines.append("Additional hint from previous step: " + next_prompt)
        lines.append("If the current screen is blocked, recover with HOME or BACK before continuing.")
        return "\n".join(lines)

    def build_validation_prompt(
        self,
        original_task: str,
        parsed: dict[str, Any],
        execution: dict[str, Any],
        *,
        step_index: int,
        max_steps: int,
    ) -> str:
        executed_keys: list[str] = []
        if isinstance(execution.get("command_results"), list):
            for item in execution["command_results"]:
                keys = item.get("keys")
                if isinstance(keys, list):
                    executed_keys.extend(str(key) for key in keys)
        summary = str(parsed.get("summary") or "").strip()
        visual_state = str(parsed.get("visual_state") or "").strip()
        return "\n".join(
            [
                f"Continue the same user task: {original_task}",
                f"Agent step: {step_index} of {max_steps}",
                f"Previous visual state: {visual_state}",
                f"Previous model summary: {summary}",
                "The last IR action has already been sent.",
                "First validate the current live UI and determine whether that IR action worked.",
                "Only after validating the current visible result should you choose the next JSON control step.",
                "If the UI did not change, avoid repeating the same action unless there is clear evidence it is required.",
                "Executed IR keys: " + (", ".join(executed_keys) if executed_keys else "none"),
            ]
        )

    def build_repair_prompt(
        self,
        original_task: str,
        *,
        step_index: int,
        max_steps: int,
        problem: str,
    ) -> str:
        return "\n".join(
            [
                f"Continue the same user task: {original_task}",
                f"Agent step: {step_index} of {max_steps}",
                "Your previous response could not be executed.",
                f"Problem: {problem}",
                "Inspect the live TV state again and return exactly one valid JSON object.",
                "Validate the current UI first instead of repeating the same failed action without evidence.",
                "Do not explain outside JSON.",
            ]
        )

    def task_completed(self, parsed: dict[str, Any]) -> bool:
        if bool(parsed.get("done")):
            return True
        return str(parsed.get("task_status") or "").strip().upper() == "COMPLETED"

    def needs_confirmation(self, parsed: dict[str, Any]) -> bool:
        if bool(parsed.get("requires_confirmation")):
            return True
        return str(parsed.get("task_status") or "").strip().upper() == "NEEDS_CONFIRMATION"

    def _command_to_keys(self, command: dict[str, Any]) -> list[str] | None:
        action = str(command.get("action") or "").strip().upper()
        repeats = command.get("repeats", 1)
        try:
            repeats_int = max(1, min(20, int(repeats)))
        except Exception:
            repeats_int = 1

        mapping = {
            "PRESS_HOME": "HOME",
            "PRESS_BACK": "RETURN",
            "PRESS_MENU": "MENU",
            "PRESS_INPUT": "SOURCE",
            "PRESS_POWER": "POWER",
            "PRESS_MUTE": "MUTE",
            "PRESS_UP": "UP",
            "PRESS_DOWN": "DOWN",
            "PRESS_LEFT": "LEFT",
            "PRESS_RIGHT": "RIGHT",
            "PRESS_OK": "ENTER",
            "PRESS_VOLUME_UP": "VOL_UP",
            "PRESS_VOLUME_DOWN": "VOL_DOWN",
            "PRESS_CHANNEL_UP": "CH_UP",
            "PRESS_CHANNEL_DOWN": "CH_DOWN",
        }
        if action == "CUSTOM":
            key = str(command.get("key") or "").strip().upper()
            return [key] * repeats_int if key else None
        mapped = mapping.get(action)
        return [mapped] * repeats_int if mapped else None

    def execute_from_response(self, parsed: dict[str, Any]) -> dict[str, Any] | None:
        if not self.config.execute_ir:
            return None
        if not isinstance(parsed, dict):
            return None
        if bool(parsed.get("requires_confirmation")):
            return {
                "ok": False,
                "detail": "Model requested confirmation before execution.",
                "serial_port": self.ir_service.port,
            }
        commands = parsed.get("commands")
        if not isinstance(commands, list) or not commands:
            return None
        if not self.ir_service.configured:
            return {
                "ok": False,
                "detail": "IR serial port is not configured. Use --ir-serial-port /dev/ttyUSB0.",
                "serial_port": None,
            }

        results = []
        for command in commands:
            if not isinstance(command, dict):
                return {"ok": False, "detail": "Invalid command payload from live model."}
            keys = self._command_to_keys(command)
            if not keys:
                return {"ok": False, "detail": f"Unsupported live action: {command}"}
            result = self.ir_service.send_key_sequence(
                device_id=self.config.ir_device_id,
                keys=keys,
            )
            results.append({"command": command, "keys": keys, "result": result})
            if not bool(result.get("success")):
                return {
                    "ok": False,
                    "detail": str(result.get("error") or "IR command sequence failed"),
                    "serial_port": self.ir_service.port,
                    "command_results": results,
                }
        return {
            "ok": True,
            "detail": "IR commands sent over serial.",
            "serial_port": self.ir_service.port,
            "command_results": results,
        }
