# Запуск всей системы одной командой: .\start.ps1
# Supervisor сам запускает Portals, Tonnel, MRKT и closer, перезапускает
# упавшие, пишет логи в папку logs и шлёт владельцу оповещения в бот.
# Остановить: Ctrl+C в этом окне.
Set-Location $PSScriptRoot
. .\env.ps1
python -m gift_sniper.supervisor
