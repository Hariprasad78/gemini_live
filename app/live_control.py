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
  "goal_state": {
    "user_goal": "the original user goal in concrete terms",
    "success_condition": "what must be visibly true before done=true",
    "current_subgoal": "small next objective before any key press"
  },
  "subplan": {
    "observe": "what you verified on the current frame",
    "validate": "why the current UI state supports or does not support action",
    "next_action_rationale": "why the planned key is the best single next action",
    "fallback_if_wrong": "what to try if this action does not visibly work"
  },
  "visual_state": "what you see on screen",
  "ui_state": {
    "screen": "current screen or section name",
    "focused_item": "exact focused item label/icon, or unknown",
    "focused_evidence": "visible focus cue: highlight ring, brighter tile, underline, cursor rectangle, enlarged tile, selected tab, or empty if not visible",
    "focus_location": "row/column/region of the focused item, or unknown",
    "visible_items": ["labels/icons that are actually visible"],
    "target_name": "requested target app/item if any",
    "target_visible": true_or_false,
    "target_location": "focused|left|right|up|down|visible_not_focused|not_visible|unknown",
    "target_distance_steps": 0,
    "target_evidence": "exact visible label/icon evidence for the target, or empty string",
    "observation_confidence": 0.0_to_1.0,
    "action_basis": "VISIBLE_TARGET|VISIBLE_SEARCH_OR_APPS|RECOVERY|NO_VISIBLE_PATH|UNKNOWN"
  },
  "device_profile": {
    "tv_brand": "Samsung|LG|Sony|TCL|Hisense|Mi|OnePlus|unknown",
    "os_type": "Tizen|webOS|Google TV|Android TV|Roku TV|Fire TV|VIDAA|PatchWall|unknown",
    "confidence": 0.0_to_1.0,
    "evidence": "short visual clues, such as logo, launcher layout, app row, settings style, remote hints"
  },
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
- Think in this order every time: observe the current frame, validate it against the goal, then choose at most one safe action. Do not skip the validate step.
- Always include goal_state and subplan. The subplan must be specific to the current frame, not generic.
- The success_condition must be visual. Example: for "open YouTube", success is the YouTube app screen or YouTube splash/home visibly open, not merely moving through Smart Hub.
- current_subgoal must be a small visible objective, such as "focus the visible YouTube tile" or "open the visible Search entry"; it must not be "move around to find YouTube".
- next_action_rationale must name the visible evidence that justifies the key. If there is no visible evidence, commands must be empty and task_status should be IN_PROGRESS or BLOCKED with a reason.
- Always include ui_state. Before choosing commands, explicitly answer: what screen am I on, what item is focused, what items are visible, and is the requested target visible?
- A cursor/focus guess is not enough. Identify the exact visible focus cue before any navigation: highlight outline, brighter tile, underline, selected tab, enlarged card, cursor rectangle, or focused text color. Put this in ui_state.focused_evidence.
- focused_item must be the item with the visible focus cue, not merely the first visible item or the item you expect from previous steps. If the focus cue is unclear, set focused_item="unknown", focused_evidence="", focus_location="unknown", observation_confidence <= 0.60, and commands=[] unless using HOME/BACK recovery.
- focus_location must describe where the focused item is in the current frame, for example "top tab row second item", "bottom app row first tile", "center content card", or "unknown".
- In subplan.observe, name both the visible focus cue and the focused item. In subplan.validate, explain why that focus cue supports the planned key.
- Set observation_confidence based on how clearly you can see the focused item, visible labels/icons, and target evidence. Use low confidence when text is blurry, cropped, glare-covered, or inferred.
- If observation_confidence is below 0.75, do not send directional or OK actions for app launch. Re-observe, recover to a clearer screen, or mark blocked.
- target_visible is true only when the target's label or recognizable icon is actually visible. Put the exact visual proof in target_evidence and visible_items.
- For app-launch tasks like opening YouTube, do not use directional keys unless the target app is visible or a visible Search/Apps entry is visible and you are moving toward that visible entry. If neither target nor Search/Apps is visible, use recovery such as HOME/BACK or mark blocked; do not guess.
- If the requested app is already focused, press OK.
- If the requested app is visible but not focused, you may send multiple directional repeats only when target_location is left/right/up/down and target_distance_steps is the exact number of key presses needed. Example: if YouTube is visibly five tiles to the right, emit PRESS_RIGHT with repeats=5.
- After multi-step directional navigation, do not include PRESS_OK in the same response. The next model turn must re-check that the target is focused before pressing OK.
- If you cannot determine the exact direction and count, do not move.
- For app launch, commands may be empty if you need one more observation; never send a key just to explore random UI.
- Always include device_profile. Infer the TV brand and OS from visible UI clues when possible; use unknown with low confidence if the screen is black or unclear.
- Use device_profile to choose efficient navigation patterns. Examples: Samsung/Tizen uses Home hub tiles, LG/webOS uses bottom launcher cards, Google TV/Android TV often has top tabs and app rows, Roku TV uses a simple tile grid.
- Use commands only when the scene supports the action.
- This is a looped controller. Emit the next best remote step based on the current screen state and previous progress.
- Behave like an autonomous agent. Keep making progress until the user task is visibly complete.
- After each IR action, re-check the visible UI before deciding the next action.
- If the UI did not change after an action, do not mindlessly repeat the same key forever. Try a recovery action or mark the task blocked.
- Do not scan endlessly through app rows or menus. After three moves in the same direction, stop and reassess the actual focused item. If the target is not visible, choose a different strategy such as opening Search, pressing HOME/BACK to recover, or marking the task blocked.
- Directional repeats greater than 1 are allowed only for a visually grounded target with an exact target_distance_steps count. Never use repeats to scan unknown UI.
- Do not claim focus moved to a numbered app unless the focused app label or icon is visible enough to identify. Prefer describing the focused item by name or visual evidence.
- Never say "focus moved to the second/third/fourth item" unless you can name the item or describe its focused icon and visible focus cue. Number-only focus tracking is unreliable.
- If focus appears to jump to a tab like Live or Guide, update focused_item to that tab and change strategy. Do not keep navigating as if the app row still has focus.
- For app-launch tasks such as opening YouTube, prefer targeted app discovery over blind navigation. On Samsung/Tizen, if the visible launcher path moves focus to Live/Guide or content panels instead of the app icon row, do not repeat that route. Use the visible Search/Apps path, open the app list, or mark blocked if no search/apps entry is reachable.
- If a sequence of actions returns to the same visible states, treat it as a loop. Do not replay the sequence; choose a new strategy.
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
            "Inspect the current TV state from the live video, infer the TV brand and OS type from visual UI clues, "
            "identify the focused UI item by its visible cursor/highlight evidence and whether the requested target is visible, "
            "define the visual success condition and current subgoal, validate the current frame, "
            "and return the next JSON control step."
            "\nStay in agent mode. Do not ask the user to press Enter again. Keep working until the task is completed on screen."
        )

    @staticmethod
    def extract_goal_state(parsed: dict[str, Any]) -> dict[str, Any]:
        state = parsed.get("goal_state")
        if not isinstance(state, dict):
            return {}
        return {
            "user_goal": str(state.get("user_goal") or "").strip(),
            "success_condition": str(state.get("success_condition") or "").strip(),
            "current_subgoal": str(state.get("current_subgoal") or "").strip(),
        }

    @staticmethod
    def extract_subplan(parsed: dict[str, Any]) -> dict[str, Any]:
        plan = parsed.get("subplan")
        if not isinstance(plan, dict):
            return {}
        return {
            "observe": str(plan.get("observe") or "").strip(),
            "validate": str(plan.get("validate") or "").strip(),
            "next_action_rationale": str(plan.get("next_action_rationale") or "").strip(),
            "fallback_if_wrong": str(plan.get("fallback_if_wrong") or "").strip(),
        }

    @staticmethod
    def format_goal_state(state: dict[str, Any] | None) -> str:
        if not state:
            return "unknown"
        subgoal = str(state.get("current_subgoal") or "").strip() or "unknown"
        success = str(state.get("success_condition") or "").strip()
        if success:
            return f"subgoal={subgoal}; success={success}"
        return f"subgoal={subgoal}"

    @staticmethod
    def format_subplan(plan: dict[str, Any] | None) -> str:
        if not plan:
            return "unknown"
        observe = str(plan.get("observe") or "").strip()
        validate = str(plan.get("validate") or "").strip()
        rationale = str(plan.get("next_action_rationale") or "").strip()
        parts = []
        if observe:
            parts.append("observe=" + observe)
        if validate:
            parts.append("validate=" + validate)
        if rationale:
            parts.append("action=" + rationale)
        return "; ".join(parts) if parts else "unknown"

    @staticmethod
    def extract_device_profile(parsed: dict[str, Any]) -> dict[str, Any]:
        profile = parsed.get("device_profile")
        if not isinstance(profile, dict):
            return {}
        confidence_raw = profile.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except Exception:
            confidence = 0.0
        return {
            "tv_brand": str(profile.get("tv_brand") or "unknown").strip() or "unknown",
            "os_type": str(profile.get("os_type") or "unknown").strip() or "unknown",
            "confidence": confidence,
            "evidence": str(profile.get("evidence") or "").strip(),
        }

    @staticmethod
    def extract_ui_state(parsed: dict[str, Any]) -> dict[str, Any]:
        state = parsed.get("ui_state")
        if not isinstance(state, dict):
            return {}
        visible_raw = state.get("visible_items")
        visible_items: list[str] = []
        if isinstance(visible_raw, list):
            visible_items = [str(item).strip() for item in visible_raw if str(item).strip()]
        return {
            "screen": str(state.get("screen") or "unknown").strip() or "unknown",
            "focused_item": str(state.get("focused_item") or "unknown").strip() or "unknown",
            "focused_evidence": str(state.get("focused_evidence") or "").strip(),
            "focus_location": str(state.get("focus_location") or "unknown").strip() or "unknown",
            "visible_items": visible_items,
            "target_name": str(state.get("target_name") or "").strip(),
            "target_visible": bool(state.get("target_visible")),
            "target_location": str(state.get("target_location") or "unknown").strip() or "unknown",
            "target_distance_steps": LiveControlExecutor._coerce_int(state.get("target_distance_steps")),
            "target_evidence": str(state.get("target_evidence") or "").strip(),
            "observation_confidence": LiveControlExecutor._coerce_confidence(state.get("observation_confidence")),
            "action_basis": str(state.get("action_basis") or "UNKNOWN").strip().upper() or "UNKNOWN",
        }

    @staticmethod
    def _coerce_confidence(value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 0.0

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return max(0, int(value))
        except Exception:
            return 0

    @staticmethod
    def format_ui_state(state: dict[str, Any] | None) -> str:
        if not state:
            return "unknown"
        focused = str(state.get("focused_item") or "unknown").strip() or "unknown"
        focused_evidence = str(state.get("focused_evidence") or "").strip()
        focus_location = str(state.get("focus_location") or "").strip()
        target = str(state.get("target_name") or "").strip()
        target_visible = bool(state.get("target_visible"))
        target_location = str(state.get("target_location") or "unknown").strip() or "unknown"
        target_distance = state.get("target_distance_steps", 0)
        target_evidence = str(state.get("target_evidence") or "").strip()
        confidence = state.get("observation_confidence", 0.0)
        try:
            confidence_text = f"{float(confidence):.2f}"
        except Exception:
            confidence_text = "0.00"
        visible_items = state.get("visible_items")
        visible_text = ""
        if isinstance(visible_items, list) and visible_items:
            visible_text = " visible=" + ", ".join(str(item) for item in visible_items[:8])
        target_text = (
            f" target={target} visible={target_visible} location={target_location} steps={target_distance}"
            if target
            else ""
        )
        evidence_text = f" evidence={target_evidence}" if target_evidence else ""
        focus_text = f" focus_evidence={focused_evidence}" if focused_evidence else ""
        location_text = f" focus_location={focus_location}" if focus_location else ""
        return f"focused={focused}{location_text}{focus_text}{target_text} confidence={confidence_text}{evidence_text}{visible_text}"

    @staticmethod
    def format_device_profile(profile: dict[str, Any] | None) -> str:
        if not profile:
            return "unknown"
        brand = str(profile.get("tv_brand") or "unknown").strip() or "unknown"
        os_type = str(profile.get("os_type") or "unknown").strip() or "unknown"
        confidence = profile.get("confidence", 0.0)
        try:
            confidence_text = f"{float(confidence):.2f}"
        except Exception:
            confidence_text = "0.00"
        evidence = str(profile.get("evidence") or "").strip()
        text = f"{brand} / {os_type} confidence={confidence_text}"
        if evidence:
            text += f" evidence={evidence}"
        return text

    @staticmethod
    def inferred_device_id(profile: dict[str, Any] | None) -> str | None:
        if not profile:
            return None
        try:
            confidence = float(profile.get("confidence") or 0.0)
        except Exception:
            confidence = 0.0
        if confidence < 0.65:
            return None
        brand = str(profile.get("tv_brand") or "").strip().lower()
        os_type = str(profile.get("os_type") or "").strip().lower()
        if "lg" in brand or "webos" in os_type:
            return "lg_tv_default"
        if "samsung" in brand or "tizen" in os_type:
            return "samsung_tv_default"
        return None

    def build_followup_prompt(
        self,
        original_task: str,
        parsed: dict[str, Any],
        execution: dict[str, Any] | None,
        *,
        step_index: int,
        max_steps: int,
        last_error: str | None = None,
        device_profile: dict[str, Any] | None = None,
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
            "Last goal state: " + self.format_goal_state(self.extract_goal_state(parsed)),
            "Last subplan: " + self.format_subplan(self.extract_subplan(parsed)),
            "Last UI state: " + self.format_ui_state(self.extract_ui_state(parsed)),
            "Current inferred TV profile: " + self.format_device_profile(device_profile),
        ]
        if executed_keys:
            lines.append("Executed IR keys: " + ", ".join(executed_keys))
        if last_error:
            lines.append("Last execution or parsing issue: " + last_error)
        lines.extend(self._strategy_hints(original_task, device_profile))
        lines.append(
            "Before any command, update goal_state and subplan from the current frame: observe the focus cue, "
            "validate the focused item and target location, then act."
        )
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
        device_profile: dict[str, Any] | None = None,
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
                "Previous goal state: " + self.format_goal_state(self.extract_goal_state(parsed)),
                "Previous subplan: " + self.format_subplan(self.extract_subplan(parsed)),
                "Previous UI state: " + self.format_ui_state(self.extract_ui_state(parsed)),
                f"Previous model summary: {summary}",
                "Current inferred TV profile: " + self.format_device_profile(device_profile),
                "The last IR action has already been sent.",
                *self._strategy_hints(original_task, device_profile),
                "Before any command, update goal_state and subplan from the current frame: observe the focus cue, validate the focused item and target location, then act.",
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
        device_profile: dict[str, Any] | None = None,
    ) -> str:
        return "\n".join(
            [
                f"Continue the same user task: {original_task}",
                f"Agent step: {step_index} of {max_steps}",
                "Your previous response could not be executed.",
                f"Problem: {problem}",
                "Current inferred TV profile: " + self.format_device_profile(device_profile),
                *self._strategy_hints(original_task, device_profile),
                "Before any command, update goal_state and subplan from the current frame: observe the focus cue, validate the focused item and target location, then act.",
                "Inspect the live TV state again and return exactly one valid JSON object.",
                "Validate the current UI first instead of repeating the same failed action without evidence.",
                "Do not explain outside JSON.",
            ]
        )

    def task_completed(self, parsed: dict[str, Any]) -> bool:
        if bool(parsed.get("done")):
            return True
        return str(parsed.get("task_status") or "").strip().upper() == "COMPLETED"

    @staticmethod
    def _strategy_hints(original_task: str, device_profile: dict[str, Any] | None) -> list[str]:
        task = str(original_task or "").lower()
        brand = str((device_profile or {}).get("tv_brand") or "").lower()
        os_type = str((device_profile or {}).get("os_type") or "").lower()
        hints: list[str] = []
        if any(name in task for name in ("youtube", "netflix", "prime video", "hotstar", "app")):
            hints.append(
                "App-launch strategy: identify a visible app tile by name/icon before pressing OK. "
                "If the requested app is visible, set target_visible=true and target_evidence to the exact label/icon. "
                "Before moving, set focused_evidence to the visible highlight/cursor cue and focus_location to where that cue is. "
                "If focus is unclear, return no command and re-observe; if the target is not visible, use Search or Apps instead of scanning blindly."
            )
        if "samsung" in brand or "tizen" in os_type:
            hints.append(
                "Samsung/Tizen hint: Smart Hub can move focus between tabs, Guide, content panels, and the app row. "
                "If RIGHT from Samsung TV Plus lands on Live, that path is wrong; navigate to Search/Apps or a visible app-list entry."
            )
        return hints

    @staticmethod
    def target_app_name(user_request: str) -> str | None:
        text = str(user_request or "").lower()
        targets = {
            "youtube": "youtube",
            "netflix": "netflix",
            "prime video": "prime video",
            "amazon prime": "prime video",
            "hotstar": "hotstar",
            "disney": "disney",
        }
        for needle, target in targets.items():
            if needle in text:
                return target
        return None

    @staticmethod
    def _text_contains_any(text: str, needles: list[str]) -> bool:
        lowered = str(text or "").lower()
        return any(needle in lowered for needle in needles)

    @staticmethod
    def _target_visual_terms(target: str) -> list[str]:
        target = str(target or "").lower()
        terms = [target]
        if target == "youtube":
            terms.extend(["you tube", "red play", "play icon", "play button"])
        elif target == "prime video":
            terms.extend(["prime", "amazon prime"])
        elif target == "hotstar":
            terms.extend(["disney hotstar", "disney+ hotstar"])
        return terms

    @staticmethod
    def _search_or_apps_terms() -> list[str]:
        return ["search", "apps", "app store"]

    @staticmethod
    def _visible_index(items: list[str], terms: list[str]) -> int | None:
        for index, item in enumerate(items):
            if LiveControlExecutor._text_contains_any(item, terms):
                return index
        return None

    @staticmethod
    def _direction_for_action(action: str) -> str | None:
        return {
            "PRESS_LEFT": "left",
            "PRESS_RIGHT": "right",
            "PRESS_UP": "up",
            "PRESS_DOWN": "down",
        }.get(str(action or "").strip().upper())

    def validate_app_launch_plan(
        self,
        user_request: str,
        parsed: dict[str, Any],
        command_signature: str,
    ) -> str | None:
        target = self.target_app_name(user_request)
        if not target:
            return None
        commands = parsed.get("commands")
        if not isinstance(commands, list) or not commands:
            return None
        valid_commands = [item for item in commands if isinstance(item, dict)]
        has_ok = any(str(item.get("action") or "").strip().upper() == "PRESS_OK" for item in valid_commands)
        has_direction = any(
            str(item.get("action") or "").strip().upper()
            in {"PRESS_UP", "PRESS_DOWN", "PRESS_LEFT", "PRESS_RIGHT"}
            for item in valid_commands
        )
        if has_ok and has_direction:
            return (
                "Do not combine directional navigation and OK in one app-launch response. "
                "Move first, then re-check the focused item before pressing OK."
            )
        first_command = next((item for item in commands if isinstance(item, dict)), None)
        if not first_command:
            return None
        action = str(first_command.get("action") or "").strip().upper()
        try:
            repeats = max(1, int(first_command.get("repeats") or 1))
        except Exception:
            repeats = 1
        ui_state = self.extract_ui_state(parsed)
        visible_items = [str(item).lower() for item in ui_state.get("visible_items") or []]
        focused_item = str(ui_state.get("focused_item") or "").lower()
        focused_evidence = str(ui_state.get("focused_evidence") or "").strip().lower()
        focus_location = str(ui_state.get("focus_location") or "").strip().lower()
        action_basis = str(ui_state.get("action_basis") or "").upper()
        observation_confidence = float(ui_state.get("observation_confidence") or 0.0)
        target_location = str(ui_state.get("target_location") or "unknown").strip().lower()
        target_distance_steps = int(ui_state.get("target_distance_steps") or 0)
        target_evidence = str(ui_state.get("target_evidence") or "").lower()
        ui_target_name = str(ui_state.get("target_name") or "").strip().lower()
        target_terms = self._target_visual_terms(target)
        search_or_apps_terms = self._search_or_apps_terms()
        visual_text = " ".join([focused_item, target_evidence, *visible_items])
        target_evidence_present = self._text_contains_any(visual_text, target_terms)
        intermediate_target_present = self._text_contains_any(
            " ".join([ui_target_name, target_evidence, *visible_items]),
            search_or_apps_terms,
        )
        target_visible_claimed = bool(ui_state.get("target_visible"))
        search_or_apps_visible = any(
            self._text_contains_any(item, search_or_apps_terms)
            for item in visible_items
        )

        if action in {"PRESS_OK", "PRESS_UP", "PRESS_DOWN", "PRESS_LEFT", "PRESS_RIGHT"}:
            if not focused_evidence or focused_evidence in {"unknown", "unclear", "none", "not visible"}:
                return (
                    "Focused cursor/highlight evidence is missing. Before pressing navigation or OK, identify the "
                    "visible focus cue such as highlight ring, brighter tile, underline, selected tab, or cursor rectangle."
                )
            if not focused_item or focused_item == "unknown":
                return (
                    "Focused item is unknown. Do not navigate by memory; re-observe the current frame and name the item "
                    "that has the visible focus cue."
                )
            if not focus_location or focus_location == "unknown":
                return (
                    "Focus location is unknown. Report where the focused item is on screen before choosing a directional key."
                )

        if action == "PRESS_OK":
            if observation_confidence < self.config.agent_min_visual_confidence:
                return (
                    f"Observation confidence {observation_confidence:.2f} is too low for pressing OK. "
                    "Re-observe until the focused item is clearly identified."
                )
            if self._text_contains_any(focused_item, target_terms) or self._text_contains_any(focused_item, search_or_apps_terms):
                return None
            return (
                f"Target app '{target}' is not focused. Do not press OK until the focused item is the target "
                "or a visible Search/Apps entry."
            )

        if self.is_directional_command_signature(command_signature):
            if observation_confidence < self.config.agent_min_visual_confidence:
                return (
                    f"Observation confidence {observation_confidence:.2f} is too low for directional app navigation. "
                    "Wait for a clearer frame and list the visible labels/icons before moving."
                )
            if target_visible_claimed and not target_evidence_present and not intermediate_target_present:
                return (
                    f"Target app '{target}' was claimed visible, but no visible item/evidence names the target. "
                    "Re-scan the current frame and list the exact visible app labels/icons before moving."
                )
            if target_evidence_present:
                expected_direction = self._direction_for_action(action)
                if target_location == "focused":
                    return f"Target app '{target}' is focused; press OK instead of moving."
                if expected_direction and target_location == expected_direction:
                    focused_index = self._visible_index(visible_items, [focused_item]) if focused_item else None
                    target_index = self._visible_index(visible_items, target_terms)
                    if focused_index is not None and target_index is not None:
                        visible_delta = abs(target_index - focused_index)
                        if repeats > 1 and visible_delta and repeats != visible_delta:
                            return (
                                f"{action} repeats={repeats} conflicts with visible_items order, which shows "
                                f"'{target}' about {visible_delta} step(s) from '{focused_item}'. Recount from the live frame."
                            )
                    if repeats > 1 and target_distance_steps != repeats:
                        return (
                            f"{action} repeats={repeats} is not grounded by target_distance_steps="
                            f"{target_distance_steps}. Use the exact visible distance or move one step."
                        )
                    return None
                return (
                    f"Target app '{target}' is visible, but target_location='{target_location}' does not justify {action}. "
                    "Report the exact relative direction and move only one step toward it."
                )
            if target_visible_claimed and not intermediate_target_present:
                return (
                    f"Target app '{target}' visibility is not grounded by target_evidence or visible_items. "
                    "Do not navigate until the visual evidence is explicit."
                )
            if search_or_apps_visible and (action_basis == "VISIBLE_SEARCH_OR_APPS" or intermediate_target_present):
                expected_direction = self._direction_for_action(action)
                if expected_direction and target_location not in {expected_direction, "visible_not_focused"}:
                    return (
                        f"Search/Apps is visible, but target_location='{target_location}' does not justify {action}. "
                        "Report the exact relative direction to the visible Search/Apps item."
                    )
                if repeats > 1 and target_distance_steps and repeats != target_distance_steps:
                    return (
                        f"{action} repeats={repeats} is not grounded by Search/Apps target_distance_steps="
                        f"{target_distance_steps}."
                    )
                return None
            return (
                f"Blind app navigation blocked: '{target}' is not reported visible and no visible Search/Apps "
                "path is being targeted. First inspect visible_items/current focus, then move only toward a visible target."
            )
        return None

    def validate_goal_subplan(self, parsed: dict[str, Any]) -> str | None:
        if not self.has_commands(parsed):
            return None
        goal_state = self.extract_goal_state(parsed)
        subplan = self.extract_subplan(parsed)
        missing: list[str] = []
        if not goal_state.get("success_condition"):
            missing.append("goal_state.success_condition")
        if not goal_state.get("current_subgoal"):
            missing.append("goal_state.current_subgoal")
        if not subplan.get("observe"):
            missing.append("subplan.observe")
        if not subplan.get("validate"):
            missing.append("subplan.validate")
        if not subplan.get("next_action_rationale"):
            missing.append("subplan.next_action_rationale")
        if missing:
            return (
                "Planner validation missing before action: "
                + ", ".join(missing)
                + ". Observe the current frame, validate against the goal, then choose an action."
            )
        return None

    def has_commands(self, parsed: dict[str, Any]) -> bool:
        commands = parsed.get("commands")
        return isinstance(commands, list) and any(isinstance(item, dict) for item in commands)

    def probe_ir(self) -> dict[str, Any]:
        return self.ir_service.probe()

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
        if action == "PRESS_OK":
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

    def is_directional_command_signature(self, signature: str) -> bool:
        first = str(signature or "").split("|", 1)[0]
        action = first.split(":", 1)[0]
        return action in {"PRESS_UP", "PRESS_DOWN", "PRESS_LEFT", "PRESS_RIGHT"}

    def execute_from_response(
        self,
        parsed: dict[str, Any],
        *,
        device_id_override: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(parsed, dict):
            return None
        if not self.config.execute_ir:
            if self.has_commands(parsed):
                return {
                    "ok": False,
                    "dry_run": True,
                    "detail": "IR execution is disabled. Restart with --execute-ir and --ir-serial-port /dev/ttyUSB0 to control the TV.",
                    "serial_port": self.ir_service.port or None,
                }
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
        device_id = device_id_override or self.config.ir_device_id
        for command in commands:
            if not isinstance(command, dict):
                return {"ok": False, "detail": "Invalid command payload from live model."}
            keys = self._command_to_keys(command)
            if not keys:
                return {"ok": False, "detail": f"Unsupported live action: {command}"}
            result = self.ir_service.send_key_sequence(
                device_id=device_id,
                keys=keys,
                delay_seconds=self.config.ir_repeat_delay_seconds,
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
            "device_id": device_id,
            "command_results": results,
        }
