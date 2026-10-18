@echo off
IF "%~1"=="" (
  echo Usage: push_to_github.bat https://github.com/YOUR-USERNAME/YOUR-REPO.git
  exit /b 1
)
git init -b main
git add .
git commit -m "Initial commit: OrderBridge WhatsApp Order Capture Suite"
git branch -M main
git remote remove origin 2>nul
git remote add origin %1
git push -u origin main
