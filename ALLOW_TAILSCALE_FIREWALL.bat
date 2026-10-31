@echo off
REM Right-click this file and choose "Run as administrator".
REM Lets ONLY devices on your Tailscale network (100.64.0.0/10) reach Day Desk on port 8790.
netsh advfirewall firewall add rule name="Day Desk (Tailscale only)" dir=in action=allow protocol=TCP localport=8790 remoteip=100.64.0.0/10
pause
