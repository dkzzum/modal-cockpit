# modal-cockpit

`modal-cockpit` — локальный CLI-инструмент для безопасного управления несколькими аккаунтами [Modal](https://modal.com/) и переключения между ними в одно действие. Он использует штатную multi-profile модель Modal и добавляет зашифрованные снапшоты аккаунтов, backup перед изменениями, блокировку от параллельных операций, атомарную запись, изолированный запуск команд под разными аккаунтами и copy-first перенос Modal Volumes между аккаунтами.

Инструмент работает с авторизацией **Modal CLI/API**, то есть с профилями в `~/.modal.toml`. Он не управляет cookie/browser-сессиями веб-интерфейса modal.com.

English documentation: [README.md](README.md).

## Инструкция для coding agents

В [AGENTS.md](AGENTS.md) лежит готовая инструкция для агентов: запускать аккаунтные команды Modal через `modal-cockpit exec` вместо bare `modal`, не управлять секретами вручную и соблюдать строгие правила безопасности при переносе volumes. Скопируйте этот файл в репозитории, где агенты должны работать с Modal через `modal-cockpit`, или перенесите нужные разделы в существующий `AGENTS.md` такого репозитория.

## Почему не просто `modal profile activate`

У Modal уже есть штатные профили и команда `modal profile activate`. `modal-cockpit` не заменяет эту модель, а делает её безопаснее и удобнее:

- encrypted snapshot каждого управляемого профиля в `~/.modal-cockpit`;
- encrypted backup всего `~/.modal.toml` перед каждым изменением;
- восстановление профиля, если его секция была удалена из конфигурации;
- синхронизация rotated/изменённых токенов из live-конфигурации перед переключением;
- атомарная запись через temporary-file + rename вместо прямой перезаписи;
- права `0600` на файлы с секретами и `0700` на каталоги хранилища;
- проверка, что файл не изменился внешним процессом во время операции;
- `exec`/`env` для параллельной работы с разными аккаунтами без глобального переключения;
- `doctor` для диагностики битых TOML, нескольких активных профилей, override через environment и небезопасных прав.

Поведение по умолчанию: **live `~/.modal.toml` считается новее снапшота**. Поэтому ротация токена не будет потеряна при переключении. Если профиль повреждён и нужно вернуть сохранённое состояние, используйте `switch --force-snapshot`.

## Установка

Из исходного кода:

```bash
git clone https://github.com/dkzzum/modal-cockpit.git
cd modal-cockpit
python3 -m venv .venv
.venv/bin/pip install -e .
```

После этого команда доступна как:

```bash
.venv/bin/modal-cockpit --help
```

Для удобства можно сделать symlink в каталог, который есть в `PATH`:

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/modal-cockpit" ~/.local/bin/modal-cockpit
```

Требуется Python 3.11+. Сам Modal CLI нужен только для `login` и `switch --verify`; базовые `capture/add/switch/exec` работают и без установленного `modal`.

## Быстрый старт

### 1. Сохранить текущий аккаунт

Если уже выполнен `modal token new` или `modal token set`:

```bash
modal-cockpit capture personal
```

Если не передать alias, используется имя текущего Modal profile:

```bash
modal-cockpit capture
```

Можно явно сохранить неактивный профиль:

```bash
modal-cockpit capture work --profile my-workspace
```

### 2. Добавить аккаунт через browser-login Modal

```bash
modal-cockpit login work --activate
```

Команда запускает `modal token new --no-activate` во временной изолированной копии конфигурации, а затем атомарно сливает полученную секцию в настоящий `~/.modal.toml`. Это защищает основной файл от прямой неатомичной записи во время интерактивного входа.

Можно передать TTL токена:

```bash
modal-cockpit login work --expires-in 7d --activate
```

### 3. Добавить token вручную

Безопасный ввод без отображения секрета:

```bash
modal-cockpit add work
```

Или через stdin:

```bash
printf '%s\n%s\n' "$MODAL_TOKEN_ID" "$MODAL_TOKEN_SECRET" | modal-cockpit add work --token-id - --token-secret -
```

Передача `--token-id/--token-secret` прямо в командной строке поддерживается, но не рекомендуется: значения могут попасть в shell history и список процессов.

### 4. Переключение в одно действие

```bash
modal-cockpit switch work
# или короче/привычнее:
modal-cockpit use work
```

Что происходит внутри:

1. открывается cross-process lock;
2. зашифрованные снапшоты синхронизируются с актуальными live-секциями;
3. создаётся encrypted backup всего текущего `~/.modal.toml`;
4. если целевой профиль пропал, он восстанавливается из encrypted snapshot;
5. целевой профиль становится единственным активным;
6. файл проверяется на внешнее изменение и атомарно заменяется;
7. обновляются метаданные последнего переключения.

Проверить результат:

```bash
modal-cockpit current
modal-cockpit list
```

Для сетевой проверки токена:

```bash
modal-cockpit switch work --verify
```

### 5. Параллельная работа без глобального переключения

`exec` выставляет `MODAL_PROFILE` только для одной команды:

```bash
modal-cockpit exec personal -- modal app list
modal-cockpit exec work -- modal volume ls
```

Так можно одновременно запускать команды в разных workspace/аккаунтах, не меняя глобальный активный профиль.

Для текущего shell:

```bash
eval "$(modal-cockpit env work)"
```

Вернуться к файловому активному профилю:

```bash
unset MODAL_PROFILE
```

Если `MODAL_PROFILE` уже установлен в shell, он имеет приоритет над `active = true` в файле. `modal-cockpit current` и `doctor` явно показывают такой override.

## Копирование volumes между аккаунтами

Команда `transfer` копирует весь Modal Volume из одного управляемого аккаунта в другой через временное локальное хранилище. Она нужна для миграции и аварийной эвакуации данных — например, если на исходном аккаунте заканчиваются деньги.

Скопировать один volume:

```bash
modal-cockpit transfer personal work my-volume --yes
```

Скопировать все volumes, видимые в исходном аккаунте:

```bash
modal-cockpit transfer personal work --all-volumes --yes
```

Полезные опции:

```bash
modal-cockpit transfer personal work my-volume --dest-volume rescued-volume --yes
modal-cockpit transfer personal work my-volume --staging-dir /path/to/large/disk --yes
modal-cockpit transfer personal work my-volume --source-env main --dest-env main --yes
modal-cockpit transfer personal work my-volume --verify --yes
```

Важные свойства и ограничения:

- это copy-first операция: исходные volumes не изменяются и не удаляются;
- целевой volume создаётся автоматически, если его нет;
- без `--force` непустой целевой volume останавливает перенос ещё на preflight, до скачивания; `--force` означает merge/overwrite, а не mirror — лишние файлы в destination остаются;
- все данные временно записываются на локальный диск, поэтому нужно достаточно свободного места. Через `--staging-dir` можно выбрать большой диск;
- во время переноса данные локально лежат в plaintext; временная директория удаляется после нормального завершения, но может остаться после SIGKILL/отключения питания;
- symlink и special file types, найденные в staging, останавливают transfer до upload; пустые директории, timestamps и executable bits не гарантированно сохраняются;
- копирование живого volume не является snapshot-consistent; сначала остановите писателей. При ошибке upload целевой volume может остаться частично скопированным;
- `--verify` повторно скачивает destination и сравнивает SHA-256 скопированных файлов. Это увеличивает bandwidth/время и не откатывает failed copy;
- transfer не обходит биллинговые или access restrictions Modal: исходный аккаунт всё ещё должен иметь возможность читать volume;
- metadata/backend version volume не копируются; автоматически созданные volumes используют настройки Modal по умолчанию. Проверено с Modal CLI 1.6.4;
- legacy `modal.NetworkFileSystem` этой командой не поддерживается.

## Восстановление и backups

Ручной backup:

```bash
modal-cockpit backup --reason before-rotation
```

Список:

```bash
modal-cockpit backups
```

Восстановление всего `~/.modal.toml` из backup:

```bash
modal-cockpit restore <backup-id> --yes
```

Перед restore автоматически создаётся ещё один backup текущего состояния.

Важно: backups хранят состояние Modal config. Encrypted account snapshots в `accounts/` специально не удаляются и не откатываются, чтобы не потерять более новые credentials, которые могли быть сохранены после создания backup.

Если секция профиля была удалена, обычный `switch` восстановит её из снапшота. Если секция существует, но повреждена/устарела и нужно вернуть именно сохранённую версию:

```bash
modal-cockpit switch work --force-snapshot
```

Это деструктивно по отношению к текущей live-секции, поэтому перед записью всегда создаётся backup.

## Диагностика

```bash
modal-cockpit doctor
```

Строгий режим с ненулевым кодом возврата при warnings/errors:

```bash
modal-cockpit doctor --strict
```

Исправить права основного конфига до `0600`:

```bash
modal-cockpit doctor --fix-permissions
```

`doctor` проверяет:

- наличие `modal` в `PATH`;
- читаемость и валидность TOML;
- ровно один активный профиль;
- права конфигурации;
- читаемость encrypted snapshots;
- `MODAL_PROFILE` и credential env overrides.

## Команды

| Команда | Назначение |
|---|---|
| `init` | создать каталоги хранилища и показать пути |
| `capture [alias]` | сохранить существующий профиль из `~/.modal.toml` |
| `login <alias>` | browser-login через `modal token new` с последующим capture/merge |
| `add <alias>` | добавить token manually без plaintext-хранилища |
| `switch/use <alias>` | переключить активный профиль в одно действие |
| `sync` | обновить snapshots из live-конфигурации |
| `list` | список аккаунтов без секретов |
| `current` | активный профиль с учётом `MODAL_PROFILE` |
| `exec <alias> -- cmd ...` | запустить команду под аккаунтом без глобального переключения |
| `env <alias>` | вывести shell-команды для `eval` |
| `transfer` | копирование одного или всех Modal Volumes между аккаунтами через локальное временное хранилище |
| `backup` / `backups` / `restore` | encrypted backups всей конфигурации |
| `remove` | удалить account snapshot; `--delete-profile` также удаляет неактивную секцию |
| `doctor` | диагностика и исправление прав |

Глобальные опции ставятся перед командой:

```bash
modal-cockpit --home ~/.modal-cockpit --config-path ~/.modal.toml list
```

Пути можно также переопределять environment:

- `MODAL_COCKPIT_HOME` — хранилище modal-cockpit;
- `MODAL_CONFIG_PATH` — путь к Modal config, как у самого Modal.

## Модель хранения и безопасность

По умолчанию используется:

```text
~/.modal-cockpit/
├── accounts/              # encrypted snapshots профилей
├── backups/               # encrypted backups всего .modal.toml
├── accounts-index.json    # только metadata, без токенов
├── encryption.key         # локальный AES key, mode 0600
└── modal-cockpit.lock
```

Формат шифрования: AES-256-GCM envelope. В account snapshot попадает вся секция профиля, включая `token_id/token_secret` или OAuth-поля и не-secret настройки вроде `server_url`, `loglevel`, `logs_timeout`.

Ограничения модели:

- Modal сам требует plaintext `~/.modal.toml`, поэтому полное шифрование рабочего файла невозможно;
- `encryption.key` лежит локально рядом с хранилищем; это защищает от случайного копирования/backups наружу, но не от вредоносного процесса с правами того же пользователя;
- CLI никогда не печатает токены в `list/current/switch`, но при ручной передаче секретов аргументами shell и process table могут их видеть;
- lock сериализует операции `modal-cockpit`, но не может запретить самому Modal CLI или другому процессу писать файл; поэтому перед записью дополнительно выполняется проверка внешних изменений.

## Разработка

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check modal_cockpit tests
```

Тесты не используют реальный `~/.modal.toml` и реальные токены. Интеграция `login` проверяется через fake `modal` executable.

## Лицензия

Проект распространяется под [PolyForm Noncommercial License 1.0.0](LICENSE).

Исходный код доступен, его можно использовать, изменять и распространять в некоммерческих целях. Из-за ограничения коммерческого использования это source-available noncommercial-лицензия, а не OSI-approved open-source лицензия.
