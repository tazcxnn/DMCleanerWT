@echo off
REM DMCleanerWT - Windows .exe uretimi (PyInstaller)
REM Eski exe'nin ustune YAZMAZ: yeni surum DMCleanerWT_v3.exe adiyla uretilir (varsa eski surum yedeklenir).
REM Temiz bir venv icinde calistir (normal "discord.py" ile "discord.py-self" CAKISIR):
REM   py -m venv .venv ^&^& .venv\Scripts\activate.bat   (PowerShell yerine cmd kullan)
REM   pip install -r requirements.txt

if exist dist\DMCleanerWT.exe copy /Y dist\DMCleanerWT.exe dist\DMCleanerWT_YEDEK.exe

REM Testler (gercek hesap/mesaj kullanmaz). Basarisizsa build iptal.
python -m unittest discover -s tests || (echo Testler basarisiz, build iptal. & exit /b 1)

REM assets klasoru (arka plan gorseli) exe icine gomulur; kod sys._MEIPASS uzerinden okur.
REM customtkinter tema/asset dosyalari --collect-all ile pakete girmeli.
pyinstaller --noconsole --onefile --clean --name DMCleanerWT_v3 ^
  --add-data "assets;assets" ^
  --collect-all customtkinter ^
  --collect-submodules discord ^
  main.py

echo.
echo Bitti: dist\DMCleanerWT_v3.exe
echo Ilk calistirmada app.log dosyasini (exe'nin yaninda) kontrol et.
