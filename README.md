# Repo Manager 1.2.7.4

Веб-менеджер локальных зеркал deb и rpm для закрытого контура. Ставится на Oracle Linux 10, отдаёт пакеты через nginx и умеет публиковать свои пакеты.

Интерфейс на порту 8000 (порт в сеть не смотрит), nginx проксирует на 80.

## 1.2.7.4

- Планировщик снова запускает sync по cron.
- У нового DEB-зеркала архитектура по умолчанию `amd64`, у RPM — `x86_64`.
- Размер Debian считается по своему каталогу `public/Имя`, а не по общему пулу aptly.
- История на странице зеркала — 50 записей. После sync лампочка снова проверяет nginx.
- Фильтр «Идёт» показывает и очередь. Версия в меню берётся из файла `VERSION`.

## 1.2.6
- При синхронизации была высока нагрузка на дисковую подсистему в связи с подщетом места , баг исправлен 
## 1.2.5

- Проверка после 1.2.4. Подсчёт размера больше не блокирует интерфейс во время sync.
- Планировщик использует ту же очередь, что и «Sync все», и не запускает лишнее сверх лимита.
- Нельзя удалить зеркало, пока оно в очереди.

## 1.2.4

- RPM sync отключает системные репозитории и не использует fastestmirror. Это чинит падение `reposync` на метаданных EPEL и OL, когда `repomd.xml` открывается, а `primary.xml` нет.
- В лог зеркала пишется короткая причина, без traceback на весь экран.

## 1.2.3

- «Sync все» больше не отбрасывает лишние зеркала. По лимиту из настроек стартуют первые, остальные встают в очередь и запускаются по мере освобождения слота.
- На карточке статус «в очереди», фиолетовая рамка. Стоп снимает зеркало с очереди.
- После перезапуска службы очередь восстанавливается.

## Что умеет

- Зеркала RPM через `dnf reposync` и DEB через `aptly`.
- Свои локальные репозитории: rpm, deb и простой каталог файлов.
- Кэш по запросу: пакет скачивается, когда клиент первый раз его просит.
- Расписание sync в cron, повтор через час после ошибки, защита от второго запуска.
- Стоп синхронизации, порог свободного места, прокси для исходящих загрузок.
- Очистка кэша по расписанию.
- Импорт и экспорт списка зеркал.
- Логи sync в `/var/lib/repo-manager/logs`, в интерфейсе только история: время и результат.
- Проверка, что nginx реально отдаёт `repomd.xml` или `Release`.

## Требования

- Oracle Linux 10, x86_64.
- PostgreSQL, nginx, Python 3.12.
- Для RPM: `dnf-plugins-core`, `createrepo_c`.
- Для DEB: бинарник `aptly` в архиве установки.
- Отдельный диск под `/var/lib/repo-manager`. Полный Debian или Ubuntu занимает сотни гигабайт.

## Установка без интернета

На машине с интернетом один раз собирается архив (`scripts/build-offline.sh`). На сервере:

```bash
tar xzf RM-offline.tar.gz
cd repo-manager
sudo bash scripts/install-offline.sh
```

Скрипт ставит пакеты из репозитория OL, создаёт пользователя `repoman`, базу `repo_manager`, venv и systemd-unit `repo-manager`. Пароль базы печатается в конце. В `pg_hba.conf` для localhost нужен `scram-sha-256`, не `ident`.

```bash
systemctl status repo-manager
curl -s http://127.0.0.1:8000/health
```

Веб-интерфейс: `http://IP:8000`. Клиентские репозитории: `http://IP/repo/...` через nginx.

## Каталоги

| Путь | Назначение |
|---|---|
| `/opt/repo-manager` | код и venv |
| `/var/lib/repo-manager/mirrors` | зеркала |
| `/var/lib/repo-manager/public` | публикация aptly |
| `/var/lib/repo-manager/local` | свои пакеты и файлы |
| `/var/lib/repo-manager/cache` | кэш по запросу |
| `/var/lib/repo-manager/logs` | файлы sync |
| `/var/lib/repo-manager/proxy.json` | прокси, порог диска, очистка кэша |

## Как завести зеркало

RPM: URL должен открывать каталог с `repodata/repomd.xml`.

```text
http://mirror.yandex.ru/oracle/OL9/baseos/latest/x86_64/
```

DEB: URL — корень архива, distribution — имя из `dists`.

```text
URL: http://mirror.yandex.ru/debian/
distribution: bookworm
components: main
architectures: amd64
```

Для `trixie-security` компонент в aptly — `updates/main`, не `main`. Security Ubuntu лежит на том же зеркале, что и релиз: `jammy-security`.

Импорт — текстовый файл, кнопка «Импорт»:

```text
OL9_BaseOS|rpm|http://mirror.yandex.ru/oracle/OL9/baseos/latest/x86_64/||||x86_64|0 3 * * *
Ubuntu22_jammy|deb|http://mirror.yandex.ru/ubuntu/|jammy|main,restricted,universe|amd64|0 2 * * *
```

Импорт создаёт запись. Для deb ещё создаёт зеркало aptly. Пакеты качаются только после Sync.

Строка клиента:

```text
baseurl=http://IP/repo/rpm/OL9_BaseOS
deb http://IP/repo/deb/Ubuntu22_jammy jammy main
```

## Меню

- Прокси — исходящий `http://user:pass@proxy:3128` и исключения.
- Место — минимум свободных ГБ. Пустое поле снимает ограничение, sync не стартует ниже порога.
- Задачи — cron очистки кэша и срок в днях. Пример: `0 4 * * *` и `14`.

Полоска диска в шапке: до 70% зелёная, до 90% жёлтая, от 91% красная.

## Обновление

```bash
systemctl stop repo-manager
cp -a /opt/repo-manager /opt/repo-manager.bak
# скопировать новые app/ и static/
chown -R repoman:repoman /opt/repo-manager
systemctl start repo-manager
```

Рестарт службы обрывает текущий sync. Данные в `/var/lib/repo-manager` и база не затрагиваются.

## Чего нет

Авторизации в интерфейсе нет.
Подпись репозиториев и уведомления не включены.
