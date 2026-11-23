# Разработка серверной версии

Работайте в отдельной ветке и описывайте конечное поведение и проверку в pull request.
Поддерживаемая поставка — Linux Docker и CLI. Desktop UI, установщики и автообновление оконного приложения в серверный репозиторий не входят.

## Проверка изменений

```bash
python -m pip install -r requirements.txt
python -m playwright install --with-deps --only-shell chromium
python -m compileall -q hh_auto scripts
python -m unittest discover -s tests
python tests/e2e_server_mode.py
python tests/e2e_application_retry.py
```

Макеты полностью изолированы от реального hh и API модели. Для проверки пользовательских сценариев не подставляйте личную сессию, ключ или настоящее резюме в fixtures.

- Изменение фильтра проверяйте до первого возможного клика отправки.
- После неоднозначного outbound нельзя автоматически повторять действие без проверки статуса.
- В журнале различайте черновик, содержимое поля и подтверждённый интерфейсом текст отправки.
- Новое поле документируйте в примерах конфигурации и `docs/CONFIGURATION.md`.
- Не добавляйте реальные профили, карты компетенций, cookies, логи, `.env` и каталоги данных в Git.
- Перед публикацией из чистого репозитория: `python scripts/audit_repository.py . --git-history`. Рабочий каталог с личными `.env` и `hh-data/` для этой проверки не подходит.

Исходный проект: [BlazeStudio/HH-Auto-Response](https://github.com/BlazeStudio/HH-Auto-Response).
Файл [LICENSE.md](LICENSE.md) и исходные уведомления об авторстве сохраняются без изменения.
