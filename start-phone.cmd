@echo off
echo Connect your phone to the same Wi-Fi as this computer.
echo Open http://YOUR_COMPUTER_WIFI_IP:8001 on your phone.
echo Keep this window open while using ParkFlow.
call "%~dp0start-demo.cmd" --host 0.0.0.0 --port 8001
