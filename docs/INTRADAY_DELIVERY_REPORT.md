# APX / INTRADAY — состояние сдачи на 25 сентября 2026

Работа не принята полностью: live acceptance и final freshness pass заблокированы HTTP 403 Bybit. Подключение напрямую и через существующий системный прокси возвращает сообщение CloudFront о блокировке страны. Интеграция в основной каталог отложена до успешного acceptance; устаревший staging не опубликован как current.

## Реализация и Git

- Основной проект: `C:\Python\MarketDataCSVBuilder`, ветка `main`, commit `264eae744a255a2318b148b3990e8d9d09722d26`, рабочее дерево чистое на момент проверки.
- Разработка: `C:\Python\MarketDataCSVBuilder_intraday_work`, ветка `feature/intraday-profile`.
- Реализация профиля: `5a12cdc805c93a937b912c6ebd70943b193f21d5`.
- Process-pool feature calculation: `df1049e`.
- Возобновление из проверенного staging: `24ff21d`.
- Этот отчёт и утилита offline completion сохранены следующим коммитом этой же ветки; его SHA выводится в итоговом сообщении и `git log -1`.
- Реализованы отдельные cache/output, public Bybit discovery без TOP-N, пять TF, 1200 закрытых баров и доступный текущий бар, общие 29 features, ATR(14), returns, funding/tickers, ограничение запросов и повторов, финальное обновление, проверка snapshot и публикация с rollback.
- Общий APX feature engine, APX pipeline, export и адаптеры источников не изменены. Новые зависимости не добавлены; использованы существующие pandas/pytest и стандартная библиотека. `.venv` создана только в worktree.

## Сохранённые данные и проверки

Возобновление не начиналось с нуля. Существующий cache сохранён; 298 валидных CSV исходного staging сохранены без перезаписи, 201 недостающая серия рассчитана из закрытого cache за 153,1 секунды. Теперь в staging 499 CSV, все прошли `load_reusable_series`; это проверка отдельных серий, а не завершённого snapshot.

Staging: `output/intraday/.staging/20260925T114325.790924Z_5cb852b5` внутри worktree. Cache: `data/cache/intraday/v1/bybit/linear_perpetual`.

| Метрика | Результат |
|---|---|
| Eligible instruments в сохранённом дневном run | 100 при turnover24h >= 10M USDT |
| Ожидалось серий | 500 |
| Валидных staging CSV | 499 |
| Отсутствует | BCHUSDT / 1D |
| Причина | Историческая свеча Bybit 2020-12-14: open=0, low=0; строгая проверка отклонила её |
| Успешно опубликованных INTRADAY snapshot | 0 |
| Размер 499 CSV | 388 641 224 байта |
| Размер cache | 74 060 983 байта |
| Полный runtime acceptance | Недоступен: run прерван, продолжение заблокировано |
| История по TF в возобновляемом дневном run | 1D 106,0; 4H 58,6; 1H 50,3; 15m 20,9; 5m 38,0 с |
| API до feature stage этого run | 657 попыток, 17 повторов, 1 событие Bybit 10006; это не финальные счётчики |
| Вечерняя попытка resume | 2,814 с; 2 запроса, 1 повтор, 0 rate-limit events; HTTP 403 |
| Offline suite | 96 passed, 3 deselected, 35,31 с |
| Отдельные APX live checks | Bybit PASS, Hyperliquid PASS; MOEX — TLS timeout после 3 попыток |

Проверены BTCUSDT, ETHUSDT, SUIUSDT на всех пяти TF: 15 проверок прошли; независимый Wilder ATR совпал с экспортом с допуском 1e-10. В каждом примере 1200 закрытых баров; старые 1D/4H/1H имеют также provisional-бар, восстановленные 15m/5m содержат только закрытые бары. Последние закрытые 15m/5m начинаются соответственно 11:30/11:40 UTC 25 сентября — это устаревшие данные, непригодные для заявления о текущей свежести. Проверки свежих funding/bid/ask и согласованности финальных screening-файлов ожидают live run.

Feature engine общий, алгоритмы не переписаны. Полный расчёт и расчёт суффикса проверены тестами; rolling floating-point расчёты могут различаться в пределах rtol=1e-10. Структурные признаки проверяются отдельно.

APX сохранён: текущие 401 файл совпадают по SHA-256 с вечерним baseline; 389 файлов previous совпадают с утренним baseline. За время перерыва появился новый обычный APX snapshot `20260925T183109Z`, а старый `20260924T162059Z` переместился в previous. Изменение current не является потерей файлов при этой доработке.

Машиночитаемые доказательства в worktree:

- `output/intraday_staging_acceptance.json` — 499 серий, нет невалидных, 15 проверок BTC/ETH/SUI.
- `output/apx_preservation_check.json` — совпадение current и previous с соответствующими baseline.
- `output/intraday/.staging/20260925T114325.790924Z_5cb852b5/offline_resume_progress.json` — сохранённые и восстановленные серии.
- `output/intraday/.staging/20260925T191037.422287Z_59df2683/failure.json` — вечерний HTTP 403.

## Команды и продолжение

Обычный APX в основном проекте:

```powershell
Set-Location C:\Python\MarketDataCSVBuilder
.\run.bat
```

После интеграции явные команды профилей: `.\run_apx.bat` и `.\run_intraday.bat`. Сейчас INTRADAY доступен в worktree. Для продолжения после восстановления разрешённого Bybit подключения:

```powershell
Set-Location C:\Python\MarketDataCSVBuilder_intraday_work
.\.venv\Scripts\python.exe main.py --profile intraday --resume-staging output\intraday\.staging\20260925T114325.790924Z_5cb852b5
.\.venv\Scripts\python.exe scripts\validate_intraday.py
```

Resume использует заполненный cache и валидные префиксы features, получает свежий universe и недостающие хвосты, затем выполняет final freshness pass и проверку публикации. Не использовать `--no-cache` или `--refresh-cache` для этого продолжения.

Конфигурация: `C:\Python\MarketDataCSVBuilder_intraday_work\config.toml`, после интеграции — `C:\Python\MarketDataCSVBuilder\config.toml`. Для 10M → 30M изменить только `[intraday] min_turnover24h_usdt = 30_000_000`. APX threshold остаётся отдельным параметром.

После успешного live acceptance остаётся: записать финальные runtime/requests/rate events/counts, проверить samples и screening, выполнить fast-forward в чистый main, перенести только INTRADAY cache и проверенный output, повторно сверить APX hashes и проверить запуск из основного каталога. Worktree пока сохранён целиком для продолжения.

## Документация и Google Drive

Инструкция уже существует в worktree: `docs/APX_INTRADAY_MARKETDATA_CONSUMER.md`; детали реализации — `docs/INTRADAY_IMPLEMENTATION.md` и `README.md`.

Пути сдачи после интеграции и публикации (пока не созданы):

- `C:\Python\MarketDataCSVBuilder\output\intraday\current\README_INTRADAY_MARKET_DATA.md`
- `C:\Python\MarketDataCSVBuilder\docs\APX_INTRADAY_MARKETDATA_CONSUMER.md`

После успешного acceptance вручную скопировать целиком `C:\Python\MarketDataCSVBuilder\output\intraday\current\` в `Apx Markets/01_INPUTS/marketdata/MarketDataCSVBuilder/intraday/current/` с заменой предыдущего snapshot. Скопировать `C:\Python\MarketDataCSVBuilder\docs\APX_INTRADAY_MARKETDATA_CONSUMER.md` в `Apx Markets/00_CONTROL/ROUTE_SPECS/APX_INTRADAY_MARKETDATA_CONSUMER.md`. Текущий незавершённый staging не загружать как current. Google Drive в ходе работы не изменялся.
