# APX / INTRADAY — окончательный delivery report

## Итог после восстановления подключения и интеграции

Принят snapshot `20260925T193403.619097Z_da5ae49f`. Код интегрирован в существующий `C:\Python\MarketDataCSVBuilder` через fast-forward чистого main с `264eae7` на ветку `feature/intraday-profile`. Повторная полная загрузка при интеграции не выполнялась. Версия кода snapshot — `16d4a62e43048087f82dee92721ef6d63691ab5a`; последующий commit документации указан в сообщении сдачи и `git log -1`. Push не выполнялся.

| Acceptance metric | Результат |
|---|---:|
| Discovered / eligible | 885 / 98 |
| Expected / successful / failed series | 490 / 489 / 1 |
| READY / PARTIAL / FAILED symbols | 97 / 1 / 0 |
| Partial series | 0 |
| Reused feature series | 449 |
| API requests / estimated weight | 1292 / 1292 |
| Retries / rate-limit events / HTTP429 / Bybit10006 | 0 / 0 / 0 / 0 |
| Total runtime | 534,947 с |
| Published snapshot | 495 файлов, 388 258 961 байт |
| Перенесённый cache | 540 файлов, 75 316 558 байт |

Единственная failed series — BCHUSDT/1D: историческая свеча 2020-12-14 с open=0, low=0. Остальные четыре TF BCHUSDT доступны; symbol PARTIAL. OHLC не подменялись.

Timings: discovery 2,241 с (metadata 0,545; ticker 1,696), history 137,493 с, feature calculation 146,754 с, final refresh 207,693 с, export 75,580 с, validation 8,801 с. История по TF: 1D 39,008; 4H 26,755; 1H 24,992; 15m 23,370; 5m 23,370 с. Метрики этапов перекрываются и не суммируются в total. Rate-limit wait 364,572 с — агрегированное ожидание потоков.

### Freshness и validation

UTC 25 сентября 2026: начало 19:34:03.619097; eligibility 19:34:05.861428; final refresh серий 19:39:22.715482–19:42:48.137294; bulk ticker/funding 19:42:49.663079; завершение 19:42:58.571222. Максимальный возраст series refresh при завершении 215,856 с, bulk ticker/funding — 8,908 с. Проверено для всех успешных серий: refresh после history fetch и до completion; provisional timestamp соответствует границе TF на момент fetch; ticker/funding обновлены после всех серий. Эти значения характеризуют свежесть при создании snapshot; копирование не обновляет рынок.

Валидатор успешно выполнен и в worktree, и в основном каталоге. Проверены manifest, catalog, latest_features, universe, run_report, README, inventory и соответствие screening/raw. BTCUSDT/ETHUSDT/SUIUSDT: все 15 серий READY, по 1200 закрытых баров и одному provisional. Последние closed/current timestamps для всех трёх symbols:

| TF | Closed UTC | Provisional UTC |
|---|---|---|
| 1D | 2026-09-24 00:00 | 2026-09-25 00:00 |
| 4H | 2026-09-25 12:00 | 2026-09-25 16:00 |
| 1H | 2026-09-25 18:00 | 2026-09-25 19:00 |
| 15m | 2026-09-25 19:15 | 2026-09-25 19:30 |
| 5m | 2026-09-25 19:35 | 2026-09-25 19:40 |

### Интеграция и сохранность

В основной проект перенесены только published INTRADAY current, cache и отчёты проверки. SHA-256 каждого из 495 файлов snapshot и 540 файлов cache совпал с worktree. Старые незавершённые staging остались в worktree. После интеграции совпали APX hashes: current 401 файл, previous 389 файлов. Общие APX features/pipeline/export/source adapters не изменены относительно base. Новых dependencies нет; основной launcher использует существующий локальный `venv`.

Оба launcher проверены через `--help`; повторная локальная validation snapshot прошла. Offline suite из основного каталога: **96 passed, 3 live deselected, 57,14 с**. Ранее live smoke Bybit/Hyperliquid прошёл; MOEX был заблокирован TLS timeout, что не считается PASS. Новый APX live run при интеграции не запускался.

Доказательства в основном `output`: `intraday_acceptance.json`, `intraday_delivery_checks.json`, `intraday_transfer_verification.json`, `apx_preservation_check.json` и baseline hashes. Runtime/counts сохранены также в current/run_report.json.

### Рабочие команды и документация

```powershell
Set-Location C:\Python\MarketDataCSVBuilder
.\run_apx.bat
.\run_intraday.bat
```

`.\run.bat` по-прежнему запускает APX. Проверка INTRADAY без сети: `.\venv\Scripts\python.exe scripts\validate_intraday.py`.

Config: `C:\Python\MarketDataCSVBuilder\config.toml`; для 10M → 30M изменить только `[intraday] min_turnover24h_usdt = 30_000_000`.

Созданы и доступны:

- `C:\Python\MarketDataCSVBuilder\output\intraday\current\README_INTRADAY_MARKET_DATA.md`
- `C:\Python\MarketDataCSVBuilder\docs\APX_INTRADAY_MARKETDATA_CONSUMER.md`
- `C:\Python\MarketDataCSVBuilder\docs\INTRADAY_IMPLEMENTATION.md`

### Очистка и Google Drive

Временный `C:\Python\MarketDataCSVBuilder_intraday_work` больше не нужен для запуска: после сдачи его можно целиком удалить через `git worktree remove --force C:\Python\MarketDataCSVBuilder_intraday_work` из основного проекта. Код закоммичен, current/cache перенесены и проверены. Удалятся временная `.venv`, старые staging и диагностика worktree. Автоматическое удаление не выполнялось. Можно также удалить только старые `output/intraday/.staging` внутри временного worktree.

Сохранять основной проект, его `venv`, APX current/previous, `output/intraday/current` и `data/cache/intraday`: cache нужен для инкрементальных запусков.

Вручную скопировать целиком `C:\Python\MarketDataCSVBuilder\output\intraday\current\` в `Apx Markets/01_INPUTS/marketdata/MarketDataCSVBuilder/intraday/current/` с заменой предыдущего snapshot. Скопировать `C:\Python\MarketDataCSVBuilder\docs\APX_INTRADAY_MARKETDATA_CONSUMER.md` в `Apx Markets/00_CONTROL/ROUTE_SPECS/APX_INTRADAY_MARKETDATA_CONSUMER.md`. Google Drive не изменялся.

## Историческое состояние до восстановления подключения

Ниже сохранена история диагностики. Указанные здесь блокировка, отсутствие публикации и ожидание интеграции устранены; актуальный результат приведён выше.

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
