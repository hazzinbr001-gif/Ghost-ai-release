# GhostAI Linux release

This folder contains the hardened Linux x86_64 release compiled with Nuitka.

Run:

```bash
chmod +x RuntimeBroker
./RuntimeBroker
```

The application requires a graphical Linux desktop (`DISPLAY`) and Tesseract OCR for OCR features. `config.json` is included as the runtime configuration template.

The executable is stripped and does not ship Python source files. Client-side executables can still be reverse engineered; keep sensitive credentials and licensing decisions on the backend.
