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
        "--ir-repeat-delay",
        type=float,
        default=AppConfig.ir_repeat_delay_seconds,
        help="delay in seconds between repeated IR keys",
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
        default=AppConfig.agent_step_delay_seconds,
        help="delay in seconds between automatic control steps",
    )
    parser.add_argument(
        "--agent-ui-settle",
        type=float,
        default=AppConfig.agent_ui_settle_seconds,
        help="time in seconds to wait after an IR action before validating UI changes",
    )
    parser.add_argument(
        "--agent-observe",
        type=float,
        default=AppConfig.agent_observe_seconds,
        help="time in seconds to let fresh video arrive before asking the model to decide",
    )
    parser.add_argument(
        "--agent-min-visual-confidence",
        type=float,
        default=AppConfig.agent_min_visual_confidence,
        help="minimum model-reported UI confidence needed for app launch actions",
    )
    parser.add_argument(
        "--visual-brightness",
        type=float,
        default=AppConfig.visual_brightness,
        help="preview and live-frame brightness multiplier",
    )
    parser.add_argument(
        "--visual-contrast",
        type=float,
        default=AppConfig.visual_contrast,
        help="preview and live-frame contrast multiplier",
    )
    parser.add_argument(
        "--video-width",
        type=int,
        default=AppConfig.video_width,
        help="requested camera capture width",
    )
    parser.add_argument(
        "--video-height",
        type=int,
        default=AppConfig.video_height,
        help="requested camera capture height",
    )
    parser.add_argument(
        "--video-frame-max-size",
        type=int,
        default=AppConfig.video_frame_max_size,
        help="max encoded frame side for Gemini and web stream",
    )
    parser.add_argument(
        "--video-jpeg-quality",
        type=int,
        default=AppConfig.video_jpeg_quality,
        help="JPEG quality for Gemini and web stream frames",
    )
    parser.add_argument(
        "--video-send-interval",
        type=float,
        default=AppConfig.video_send_interval_seconds,
        help="minimum seconds between background video frames sent to Gemini",
    )
    parser.add_argument(
        "--local-preview",
        type=str,
        default="on",
        choices=["on", "off"],
        help="show the local OpenCV preview window",
    )
    parser.add_argument(
        "--web-stream",
        action="store_true",
        help="serve a Raspberry Pi friendly web page with the live preview stream",
    )
    parser.add_argument(
        "--web-host",
        type=str,
        default="0.0.0.0",
        help="host/interface for the web stream server",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="port for the web stream server",
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
        ir_repeat_delay_seconds=max(0.05, args.ir_repeat_delay),
        ir_device_id=args.ir_device_id,
        ir_dataset_path=args.ir_dataset_path,
        agent_max_steps=max(1, args.agent_max_steps),
        agent_step_delay_seconds=max(0.0, args.agent_step_delay),
        agent_ui_settle_seconds=max(0.0, args.agent_ui_settle),
        agent_observe_seconds=max(0.0, args.agent_observe),
        agent_min_visual_confidence=max(0.0, min(1.0, args.agent_min_visual_confidence)),
        visual_brightness=max(0.1, args.visual_brightness),
        visual_contrast=max(0.1, args.visual_contrast),
        video_width=max(160, args.video_width),
        video_height=max(120, args.video_height),
        video_frame_max_size=max(320, args.video_frame_max_size),
        video_jpeg_quality=max(40, min(95, args.video_jpeg_quality)),
        video_send_interval_seconds=max(0.5, args.video_send_interval),
        local_preview_enabled=args.local_preview == "on",
        web_stream_enabled=args.web_stream,
        web_stream_host=args.web_host,
        web_stream_port=max(1, min(65535, args.web_port)),
    )
