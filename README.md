# Gemini Live TV Control

Python app for streaming camera or screen frames into Gemini Live, with optional
audio and serial IR control for TV navigation.

## Features

- Camera, screen, or no-video input modes.
- Optional microphone and speaker audio.
- Gemini Live system instructions for TV control workflows.
- Optional NodeMCU serial IR sender support.
- Starter Samsung and LG IR datasets in `artifacts/`.

## Requirements

- Python 3.12 or newer.
- A Gemini API key.
- Camera/screen permissions when using video modes.
- Optional NodeMCU IR blaster connected over USB serial for real IR execution.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="your-api-key"
```

You can also use `GOOGLE_API_KEY` instead of `GEMINI_API_KEY`.

## Run

Camera mode with audio:

```bash
python main.py --mode camera --audio on
```

Screen mode without audio:

```bash
python main.py --mode screen --audio off
```

Run without video:

```bash
python main.py --mode none --audio on
```

## IR Control

By default, IR commands are planned but not sent. To send commands to a connected
NodeMCU IR blaster, pass `--execute-ir` and provide the serial port:

```bash
python main.py \
  --mode camera \
  --audio on \
  --execute-ir \
  --ir-serial-port /dev/ttyUSB0 \
  --ir-device-id samsung_tv_default
```

Useful IR options:

- `--ir-dataset-path artifacts/ir_dataset.json`
- `--ir-device-id samsung_tv_default`
- `--ir-device-id lg_tv_default`
- `--ir-sender-channel D2`
- `--ir-serial-baudrate 115200`

## Git Push

After reviewing the changes, commit and push with:

```bash
git status
git add .gitignore README.md app artifacts main.py requirements.txt
git commit -m "Add project docs and gitignore"
git branch -M main
git remote add origin <your-repository-url>
git push -u origin main
```

If `origin` already exists, use this instead of `git remote add origin ...`:

```bash
git remote set-url origin <your-repository-url>
```
