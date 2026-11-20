AICF CAD Agent - installation on a Windows PC
=============================================
What it is: a local AI agent (Qwen via your AICF gateway) that drives SolidWorks through the solidworks-mcp COM server and
can run unattended jobs with machine-checked acceptance criteria. Design data stays on this PC; nothing is sent to any cloud.

Requirements
  * Windows 10/11, PowerShell 5.1+, SolidWorks (any recent version) installed, the same Windows user logged in.
  * Network access to the AICF gateway (LAN proxy) and an API key (tenant) from the gateway administrator.
  * Internet access to pypi.org during installation (github.com too if the PC has no Python 3.11+).
  * No administrator rights needed (per-user install).

Install
  1. Unpack the zip anywhere.
  2. Open PowerShell in the unpacked folder:
       powershell -ExecutionPolicy Bypass -File installer\install.ps1 -GatewayUrl http://GATEWAY_HOST:18080
     (you are asked for the API key; or pass -ApiKey KEY)
  3. The installer ends with a self-check. Every line must be PASS (SolidWorks "running" may be WARN until you start it).

Use
  * Chat in Chrome (recommended): %LOCALAPPDATA%\AICF-CAD\app\aicf-cad-ui.cmd
      A window opens and Chrome shows the chat: streamed replies, every SolidWorks step as a card, screenshots, and
      Allow/Deny buttons for save/export/delete. Keep the black window open while you work; close it to stop.
  * Text console chat: %LOCALAPPDATA%\AICF-CAD\app\aicf-cad-chat.cmd
  * Unattended:   %LOCALAPPDATA%\AICF-CAD\app\aicf-cad-job.cmd job.json
      - SolidWorks must be OPEN and responsive first; the Windows session must stay logged in (disconnecting RDP is fine,
        signing out is not). Job outputs, audit logs and REPORT.md go to %LOCALAPPDATA%\AICF-CAD\data.
  * Re-check:     aicf-cad-doctor.cmd

Uninstall:  powershell -ExecutionPolicy Bypass -File installer\uninstall.ps1 [-Purge]

Security notes
  * The API key is stored in app\api.key readable only by the installing user. Anyone who can log in as that user can read it.
  * The agent only offers the model a fixed list of modelling/inspection tools; writes are limited to the job folder.
  * Do not enable the external advisor (advisor != "off") without reading the data-leak limits in the project plan.
