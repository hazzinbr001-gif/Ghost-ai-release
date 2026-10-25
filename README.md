# GhostAI — Release Binaries

**This repository stores built applications only. No source code lives here.**

| OS | File | Download (always latest) |
|---|---|---|
| Windows 10/11 (x64) | `windows/GhostAI-Windows.exe` | https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/latest/download/GhostAI-Windows.exe |
| macOS (Apple Silicon) | `MacOs/GhostAI-macOS.zip` | https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/latest/download/GhostAI-macOS.zip |
| Linux (x86_64) | `linux/GhostAI-linux` | https://github.com/hazzinbr001-gif/Ghost-ai-release/releases/latest/download/GhostAI-linux |

The `releases/latest/download/<name>` links are stable: they always resolve to
the newest published release. Publish a new release under a new `vX.Y.Z` tag
and these links pick it up with no website change.

## Repository layout

```
windows/GhostAI-Windows.exe    Windows build (Nuitka/PyInstaller, self-contained)
MacOs/GhostAI-macOS.zip       macOS .app bundle, zipped
linux/GhostAI-linux           Linux single-file binary
linux/config.json             Runtime config template shipped with the Linux build
SHA256SUMS                    Checksums for every binary above
```

## What belongs here

- Compiled executables, zipped app bundles, installers.
- Runtime config templates that ship next to a binary.
- Checksums and this README.

## What does **not** belong here

- `main.py`, patches, build scripts, CI workflows → `GhostAI-private`
- Website/frontend code → `Ghost-ai-website`
- Backend/API code → `Ghosty`

## Verifying a download

```bash
sha256sum -c SHA256SUMS
```

## Releasing a new build

Builds are produced from `GhostAI-private` and published here by that repo's
GitHub Actions workflow (`.github/workflows/build.yml`). Manual equivalent:

```powershell
gh release create v1.1.0 `
  "windows/GhostAI-Windows.exe" `
  "MacOs/GhostAI-macOS.zip" `
  "linux/GhostAI-linux" `
  --title "GhostAI v1.1.0" --notes "Release notes here"
```

## Notes

- The desktop app contains no API keys. All AI calls, license checks and
  payment handling happen on the backend (`https://ghosty.fly.dev`).
- macOS builds are unsigned; Gatekeeper may require right-click → Open.
- Linux builds need a graphical desktop (`DISPLAY`) and Tesseract for OCR.
