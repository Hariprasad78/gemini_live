"""Command-line interface."""

from __future__ import annotations

import argparse

from .config import AppConfig, DEFAULT_MODE


def parse_args() -> AppConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        type=str,
        default=DEFAULT_MODE,
        help="pixels to stream from",
        choices=["camera", "screen", "none"],
    )
    parser.add_argument(
        "--audio",
        type=str,
        default="on",
        choices=["on", "off"],
        help="enable microphone input and speaker output",
    )
    parser.add_argument(
        "--ir-profile",
        type=str,
        default="default",
        help="IR profile name understood by the NodeMCU firmware",
    )
    parser.add_argument(
        "--execute-ir",
        action="store_true",
        help="actually send planned IR commands to the NodeMCU transport",
    )
    parser.add_argument(
        "--ir-serial-port",
        type=str,
        default=None,
        help="USB serial port for NodeMCU IR blaster, e.g. /dev/ttyUSB0",
    )
    parser.add_argument(
        "--ir-serial-baudrate",
        type=int,
        default=115200,
        help="USB serial baudrate for NodeMCU IR blaster",
    )
    parser.add_argument(
        "--ir-sender-channel",
        type=str,
        default="D2",
        help="sender channel expected by the NodeMCU firmware",
    )
    parser.add_argument(
        "--ir-device-id",
        type=str,
        default="samsung_tv_default",
        help="logical IR dataset device id",
    )
    parser.add_argument(
        "--ir-dataset-path",
        type=str,
        default="artifacts/ir_dataset.json",
        help="path to learned IR dataset JSON",
    )
    parser.add_argument(
        "--agent-max-steps",
        type=int,
        default=30,
        help="maximum automatic live-control steps before the agent stops itself",
    )
    parser.add_argument(
        "--agent-step-delay",
        type=float,
        default=1.0,
        help="delay in seconds between automatic control steps",
    )
    parser.add_argument(
        "--agent-ui-settle",
        type=float,
        default=1.5,
        help="time in seconds to wait after an IR action before validating UI changes",
    )
    args = parser.parse_args()
    return AppConfig(
        video_mode=args.mode,
        audio_enabled=args.audio == "on",
        ir_profile=args.ir_profile,
        execute_ir=args.execute_ir,
        ir_serial_port=args.ir_serial_port,
        ir_serial_baudrate=args.ir_serial_baudrate,
        ir_sender_channel=args.ir_sender_channel,
        ir_device_id=args.ir_device_id,
        ir_dataset_path=args.ir_dataset_path,
        agent_max_steps=max(1, args.agent_max_steps),
        agent_step_delay_seconds=max(0.0, args.agent_step_delay),
        agent_ui_settle_seconds=max(0.0, args.agent_ui_settle),
    )
